"""process_reply(): buttons, media, LLM (mocked), dedup, autostop, side effects."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from separator.bitrix.models import Bitrix, Connector, Line
from separator.waq import classify, tasks
from separator.waq.models import WaqEvent, WaqLead, WaqPhone, WaqSettings

from .conftest import CUSTOMER, make_lead

pytestmark = pytest.mark.django_db


@pytest.fixture()
def bot(cfg, graph, alerts_sent, monkeypatch, settings):
    """Configured texts, a Bitrix line, recorded Bitrix calls."""
    portal = Bitrix.objects.create(domain="portal.example.test", member_id="test-member")
    connector = Connector.objects.create(service="waba", code="test-connector")
    line = Line.objects.create(line_id="7", connector=connector, portal=portal)
    cfg.phone.line = line
    cfg.phone.save()
    cfg.reply_qual = "QUAL-TEXT"
    cfg.reply_qual_question = "QUESTION-TEXT"
    cfg.reply_bot = "BOT-TEXT"
    cfg.reply_not_me = "NOTME-TEXT"
    cfg.reply_stop = "STOP-TEXT"
    cfg.reply_who = "WHO-TEXT {{1}}"
    cfg.reply_company = "COMPANY-TEXT"
    cfg.qualified_stage_id = "C0:TEST_STAGE"
    cfg.responsible_user_id = 77
    cfg.save()
    settings.ANTHROPIC_API_KEY = ""

    bitrix_calls = []
    open_line = []
    monkeypatch.setattr(tasks, "_app_instance", lambda cfg: SimpleNamespace(id="app-instance-1"))
    from separator.bitrix import crest
    from separator.bitrix import tasks as bitrix_tasks

    monkeypatch.setattr(
        crest, "call_method", lambda app, method, data=None, **kw: bitrix_calls.append((method, data)) or {"result": True}
    )
    monkeypatch.setattr(bitrix_tasks.send_messages, "apply_async", lambda *a, **kw: open_line.append(kw))
    return SimpleNamespace(
        cfg=cfg, graph=graph, alerts=alerts_sent, bitrix=bitrix_calls, open_line=open_line
    )


def comment_payload(lead, text):
    return {"fields": {"ENTITY_ID": lead.deal_id, "ENTITY_TYPE": "deal", "COMMENT": text}}


def waiting_lead(**overrides):
    values = {
        "state": WaqLead.STATE_CONFIRMATION_SENT,
        "confirmation_sent_at": timezone.now() - timedelta(hours=1),
    }
    values.update(overrides)
    return make_lead(**values)


def button(text, message_id="wamid.B1"):
    return {"id": message_id, "type": "button", "text": text, "payload": text}


def text_message(body, message_id="wamid.T1"):
    return {"id": message_id, "type": "text", "text": body}


def sent_texts(graph):
    return [call["payload"]["text"]["body"] for call in graph.calls if call["payload"]["type"] == "text"]


@pytest.fixture()
def llm(monkeypatch):
    """Fake classifier: returns `llm.label`, records what it was given."""
    seen = []

    def fake(text, project):
        seen.append((text, project))
        return fake.label

    fake.label = classify.QUAL
    fake.seen = seen
    monkeypatch.setattr(classify, "classify_text", fake)
    return fake


def test_yes_button_qualifies(bot):
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, button("Yes, it was me")) == "qualified"

    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_QUALIFIED
    assert lead.outcome_source == "button"
    assert lead.replied_at is not None
    assert sent_texts(bot.graph) == ["QUAL-TEXT"]
    assert bot.graph.calls[0]["endpoint"] == "test-phone-id-1/messages"
    # Operator sees the bot reply as a system message, ~20 s later.
    assert bot.open_line[0]["countdown"] == 20
    assert bot.open_line[0]["kwargs"] == {"manager_id": 0}
    assert bot.open_line[0]["args"][1:] == [CUSTOMER, "QUAL-TEXT", "test-connector", "7"]
    methods = [method for method, _ in bot.bitrix]
    assert methods == ["crm.deal.update", "crm.timeline.comment.add"]
    assert bot.bitrix[0][1] == {"id": lead.deal_id, "fields": {"STAGE_ID": "C0:TEST_STAGE", "ASSIGNED_BY_ID": 77}}
    comment = bot.bitrix[1][1]["fields"]
    assert comment["ENTITY_TYPE"] == "deal" and comment["ENTITY_ID"] == lead.deal_id
    assert comment["COMMENT"] == "AI: КВАЛ · подтвердил заявку · Test Project · test_form"


def test_no_button_opts_out_and_records_the_negative_reply(bot):
    lead = waiting_lead(preferred_channel="telegram", lead_created_at=timezone.now() - timedelta(seconds=600))
    assert tasks.process_reply(lead.id, button("No, not me")) == "not_lead"

    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_NOT_LEAD
    assert sent_texts(bot.graph) == ["NOTME-TEXT"]
    phone = WaqPhone.objects.get(phone=CUSTOMER)
    assert phone.opted_out and phone.opt_out_reason == "not_me"
    event = WaqEvent.objects.get(kind=WaqEvent.KIND_NEGATIVE_REPLY)
    assert event.data["deal_id"] == lead.deal_id
    assert event.data["form_type"] == "test_form"
    assert event.data["preferred_channel"] == "telegram"
    assert 590 <= event.data["seconds_from_lead"] <= 700
    # Stage untouched; a timeline comment; the bot reply mirrored to the operator.
    assert bot.bitrix == [("crm.timeline.comment.add", comment_payload(lead, "AI: НЕ ЛИД · нажал «No, not me»"))]
    assert bot.open_line[0]["args"][1:3] == [CUSTOMER, "NOTME-TEXT"]
    assert bot.open_line[0]["countdown"] == 20 and bot.open_line[0]["kwargs"] == {"manager_id": 0}


def test_same_message_id_twice_is_processed_once(bot, llm):
    llm.label = classify.WHO  # WHO keeps the lead waiting, so only the dedup protects
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, text_message("who is this?")) == "who"
    assert tasks.process_reply(lead.id, text_message("who is this?")) == "duplicate"
    assert sent_texts(bot.graph) == ["WHO-TEXT Test Project"]
    assert len(llm.seen) == 1


def test_who_uses_the_fallback_project(bot, llm):
    llm.label = classify.WHO
    lead = waiting_lead(project="")
    tasks.process_reply(lead.id, text_message("who?"))
    assert sent_texts(bot.graph) == ["WHO-TEXT Test Project Fallback"]
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT


def test_company_keeps_waiting(bot, llm):
    llm.label = classify.COMPANY
    lead = waiting_lead()
    tasks.process_reply(lead.id, text_message("which agency is this?"))
    assert sent_texts(bot.graph) == ["COMPANY-TEXT"]
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT


@pytest.mark.parametrize("mtype", ["audio", "image", "video", "document"])
def test_media_qualifies_without_the_llm(bot, llm, mtype):
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, {"id": f"wamid.M-{mtype}", "type": mtype}) == "qualified"
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_QUALIFIED
    assert lead.outcome_source == "media"
    assert llm.seen == []
    assert "медиа, не расшифровано" in bot.bitrix[-1][1]["fields"]["COMMENT"]


def test_text_question_qualifies_with_its_own_reply(bot, llm):
    llm.label = classify.QUAL_QUESTION
    lead = waiting_lead()
    tasks.process_reply(lead.id, text_message("what is the payment plan?"))
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_QUALIFIED
    assert lead.outcome_source == "llm"
    assert sent_texts(bot.graph) == ["QUESTION-TEXT"]
    # Only the text and the project reach the model.
    assert llm.seen == [("what is the payment plan?", "Test Project")]


def test_stop_text_opts_out(bot, llm):
    llm.label = classify.STOP
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, text_message("stop writing to me")) == "stopped"
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_STOPPED
    assert WaqPhone.objects.get(phone=CUSTOMER).opt_out_reason == "stop"
    assert WaqEvent.objects.filter(kind=WaqEvent.KIND_STOP).count() == 1
    assert sent_texts(bot.graph) == ["STOP-TEXT"]


def test_llm_unavailable_keeps_waiting_and_alerts(bot):
    # No ANTHROPIC_API_KEY (set to "" by the fixture): the real classifier refuses.
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, text_message("hello")) == "unclassified"
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT
    assert bot.graph.calls == []  # no reply to the customer
    assert WaqEvent.objects.filter(kind=WaqEvent.KIND_LLM_ERROR).count() == 1
    assert bot.bitrix == [
        (
            "crm.timeline.comment.add",
            {"fields": {"ENTITY_ID": lead.deal_id, "ENTITY_TYPE": "deal", "COMMENT": "AI: не классифицировано"}},
        )
    ]
    assert len(bot.alerts) == 1


def test_llm_unsure_is_treated_as_unavailable(bot, llm):
    llm.label = classify.UNSURE
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, text_message("hmm")) == "unclassified"
    assert bot.graph.calls == []


def test_empty_reply_text_still_runs_the_actions(bot):
    bot.cfg.reply_qual = ""
    bot.cfg.save()
    lead = waiting_lead()
    tasks.process_reply(lead.id, button("Yes, it was me"))
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_QUALIFIED
    assert bot.graph.calls == []
    assert bot.open_line == []
    assert [method for method, _ in bot.bitrix] == ["crm.deal.update", "crm.timeline.comment.add"]


def test_manual_stop_blocks_the_reply_but_not_bitrix(bot):
    bot.cfg.enabled = False
    bot.cfg.save()
    lead = waiting_lead()
    tasks.process_reply(lead.id, button("Yes, it was me"))
    assert bot.graph.calls == []  # no outbound WhatsApp at all
    assert bot.open_line == []  # nothing was sent, nothing to mirror
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_QUALIFIED
    assert [method for method, _ in bot.bitrix] == ["crm.deal.update", "crm.timeline.comment.add"]
    assert bot.bitrix[0][1]["fields"] == {"STAGE_ID": "C0:TEST_STAGE", "ASSIGNED_BY_ID": 77}
    assert bot.bitrix[1][1]["fields"]["COMMENT"] == (
        "AI: КВАЛ · подтвердил заявку · Test Project · test_form · ответ клиенту не отправлен: ручной стоп"
    )


def test_manual_stop_note_on_a_negative_reply(bot):
    bot.cfg.enabled = False
    bot.cfg.save()
    lead = waiting_lead()
    tasks.process_reply(lead.id, button("No, not me"))
    assert bot.graph.calls == []
    assert bot.bitrix == [
        (
            "crm.timeline.comment.add",
            comment_payload(lead, "AI: НЕ ЛИД · нажал «No, not me» · ответ клиенту не отправлен: ручной стоп"),
        )
    ]


def test_autostop_blocks_only_templates_not_replies(bot):
    bot.cfg.autostop_active = True
    bot.cfg.autostop_reason = "test"
    bot.cfg.save()
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, button("Yes, it was me")) == "qualified"
    assert sent_texts(bot.graph) == ["QUAL-TEXT"]
    assert len(bot.open_line) == 1
    assert [method for method, _ in bot.bitrix] == ["crm.deal.update", "crm.timeline.comment.add"]
    assert "не отправлен" not in bot.bitrix[1][1]["fields"]["COMMENT"]


@pytest.mark.parametrize(
    "label, reply, comment",
    [
        (classify.STOP, "STOP-TEXT", "AI: СТОП"),
        (classify.WHO, "WHO-TEXT Test Project", "AI: спросил „кто это“"),
        (classify.COMPANY, "COMPANY-TEXT", "AI: спросил „какая компания“"),
        (classify.NOT_LEAD, "NOTME-TEXT", "AI: НЕ ЛИД · по тексту ответа: не оставлял заявку / ошибся номером / спам / не по делу"),
        (classify.BOT, "BOT-TEXT", "AI: КВАЛ · спросил, не бот ли · Test Project · test_form"),
        (classify.QUAL_QUESTION, "QUESTION-TEXT", "AI: КВАЛ · задал вопрос по объекту · Test Project · test_form"),
    ],
)
def test_every_bot_reply_is_mirrored_and_commented(bot, llm, label, reply, comment):
    llm.label = label
    lead = waiting_lead()
    tasks.process_reply(lead.id, text_message("some text"))
    assert sent_texts(bot.graph) == [reply]
    assert len(bot.open_line) == 1
    assert bot.open_line[0]["args"][1:3] == [CUSTOMER, reply]
    assert bot.open_line[0]["countdown"] == 20
    comments = [data["fields"]["COMMENT"] for method, data in bot.bitrix if method == "crm.timeline.comment.add"]
    assert comments == [comment]
    updates = [method for method, _ in bot.bitrix if method == "crm.deal.update"]
    # The stage changes only for a qualified lead, never for NOT_LEAD / STOP / WHO / COMPANY.
    assert len(updates) == (1 if label in (classify.BOT, classify.QUAL_QUESTION) else 0)


def test_failed_reply_is_noted_and_not_mirrored(bot):
    bot.graph.error = "outside the 24h window"
    lead = waiting_lead()
    tasks.process_reply(lead.id, button("Yes, it was me"))
    assert bot.open_line == []
    assert bot.bitrix[-1][1]["fields"]["COMMENT"].endswith("ответ клиенту не отправлен: ошибка отправки")


def test_bitrix_failure_does_not_cancel_the_rest(bot, monkeypatch):
    from separator.bitrix import crest

    def broken(app, method, data=None, **kw):
        raise Exception("bitrix is down")

    monkeypatch.setattr(crest, "call_method", broken)
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, button("Yes, it was me")) == "qualified"
    assert sent_texts(bot.graph) == ["QUAL-TEXT"]
    assert WaqEvent.objects.filter(kind=WaqEvent.KIND_BITRIX_ERROR).count() == 2


def test_already_qualified_lead_is_left_alone(bot):
    lead = waiting_lead(state=WaqLead.STATE_QUALIFIED)
    assert tasks.process_reply(lead.id, button("No, not me")) == "not waiting"
    assert bot.graph.calls == []


def test_stickers_and_reactions_are_ignored(bot, llm):
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, {"id": "wamid.S1", "type": "sticker"}) == "ignored"
    assert tasks.process_reply(lead.id, {"id": "wamid.R1", "type": "reaction"}) == "ignored"
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT
    assert bot.graph.calls == [] and llm.seen == []


# ------------------------------------------------------------------- autostop


def _negatives(count):
    for _ in range(count):
        WaqEvent.objects.create(kind=WaqEvent.KIND_NEGATIVE_REPLY, data={})


def test_third_negative_reply_in_24h_triggers_autostop(bot, django_capture_on_commit_callbacks):
    _negatives(2)
    lead = waiting_lead()
    with django_capture_on_commit_callbacks(execute=True):
        tasks.process_reply(lead.id, button("No, not me"))
    settings_row = WaqSettings.load()
    assert settings_row.autostop_active is True
    assert "3 negative replies" in settings_row.autostop_reason
    assert WaqEvent.objects.filter(kind=WaqEvent.KIND_AUTOSTOP).count() == 1
    assert any("AUTOSTOP" in text for text in bot.alerts)


def test_old_negative_replies_do_not_count(bot):
    _negatives(2)
    WaqEvent.objects.update(created_at=timezone.now() - timedelta(hours=25))
    lead = waiting_lead()
    tasks.process_reply(lead.id, button("No, not me"))
    assert WaqSettings.load().autostop_active is False


def test_negative_share_above_30_percent_triggers_autostop(bot):
    # 5 confirmations in 24h (the lead itself is one of them), 2 negatives = 40%.
    for i in range(4):
        make_lead(phone=f"+1555010{2000 + i}", state=WaqLead.STATE_REMINDER_SENT, confirmation_sent_at=timezone.now())
    _negatives(1)
    lead = waiting_lead()
    tasks.process_reply(lead.id, button("No, not me"))
    settings_row = WaqSettings.load()
    assert settings_row.autostop_active is True
    assert "2 negative replies to 5 confirmations" in settings_row.autostop_reason


def test_negative_share_at_20_percent_does_not_trigger(bot):
    for i in range(9):
        make_lead(phone=f"+1555010{3000 + i}", state=WaqLead.STATE_REMINDER_SENT, confirmation_sent_at=timezone.now())
    _negatives(1)
    lead = waiting_lead()
    tasks.process_reply(lead.id, button("No, not me"))
    assert WaqSettings.load().autostop_active is False


# --------------------------------------------------------- the Claude request


class _FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.requests.append(kwargs)
        if self.owner.error:
            raise self.owner.error
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=json.dumps({"class": self.owner.answer})),
            ],
        )


class _FakeAnthropic:
    requests = []
    clients = []
    answer = "QUAL"
    error = None

    def __init__(self, **kwargs):
        _FakeAnthropic.clients.append(kwargs)
        self.messages = _FakeMessages(_FakeAnthropic)


@pytest.fixture()
def fake_sdk(monkeypatch, settings):
    import anthropic

    settings.ANTHROPIC_API_KEY = "test-key-not-real"
    _FakeAnthropic.requests = []
    _FakeAnthropic.clients = []
    _FakeAnthropic.answer = "QUAL"
    _FakeAnthropic.error = None
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    return _FakeAnthropic


def test_claude_request_shape(fake_sdk):
    fake_sdk.answer = "BOT"
    assert classify.classify_text("are you a bot?", "Test Project") == "BOT"
    client = fake_sdk.clients[0]
    assert client["timeout"] == 30.0 and client["max_retries"] <= 1
    request = fake_sdk.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["format"]["schema"]["properties"]["class"]["enum"] == classify.CLASSES
    assert "thinking" not in request  # never disabled on this model
    content = request["messages"][0]["content"]
    assert "are you a bot?" in content and "Test Project" in content


def test_claude_request_never_contains_phone_or_name(bot, fake_sdk):
    lead = waiting_lead()
    tasks.process_reply(lead.id, text_message("yes that was my enquiry"))
    sent = json.dumps(fake_sdk.requests, ensure_ascii=False)
    assert CUSTOMER not in sent and CUSTOMER.lstrip("+") not in sent
    assert "Test Person" not in sent
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_QUALIFIED


def test_claude_error_becomes_unavailable(fake_sdk):
    fake_sdk.error = RuntimeError("overloaded")
    with pytest.raises(classify.ClassifierUnavailable):
        classify.classify_text("hello", "Test Project")


def test_claude_garbage_answer_becomes_unavailable(fake_sdk):
    fake_sdk.answer = "MAYBE"
    with pytest.raises(classify.ClassifierUnavailable):
        classify.classify_text("hello", "Test Project")


# ------------------------------------------- Anthropic API errors (all -> unavailable)


def _api_error(status, message):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = httpx2.Response(status, request=request, json=body)
    error_class = {
        400: anthropic.BadRequestError,
        401: anthropic.AuthenticationError,
        429: anthropic.RateLimitError,
        500: anthropic.InternalServerError,
    }[status]
    return error_class(f"Error code: {status} - {body}", response=response, body=body)


CREDIT_MESSAGE = "Your credit balance is too low to access the Anthropic API."


def test_credit_balance_error_goes_to_the_unavailable_branch(bot, fake_sdk):
    fake_sdk.error = _api_error(400, CREDIT_MESSAGE)
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, text_message("yes it was me")) == "unclassified"

    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT  # keeps waiting
    assert bot.graph.calls == []  # nothing is sent to the customer
    assert [method for method, _ in bot.bitrix] == ["crm.timeline.comment.add"]
    assert bot.bitrix[0][1]["fields"]["COMMENT"] == "AI: не классифицировано"
    assert len(bot.alerts) == 1 and "credit_balance_too_low" in bot.alerts[0]
    event = WaqEvent.objects.get(kind=WaqEvent.KIND_LLM_ERROR)
    assert event.data["error_kind"] == "credit_balance_too_low"


@pytest.mark.parametrize(
    "make_error, kind",
    [
        (lambda: _api_error(401, "invalid x-api-key"), "auth_401"),
        (lambda: _api_error(429, "rate limited"), "rate_limit_429"),
        (lambda: _api_error(500, "overloaded"), "server_500"),
        (lambda: __import__("anthropic").APITimeoutError(request=__import__("httpx2").Request("POST", "https://api.anthropic.com/v1/messages")), "timeout"),
        (lambda: RuntimeError("unexpected"), "RuntimeError"),
    ],
)
def test_every_api_error_is_unavailable_with_its_kind(bot, fake_sdk, make_error, kind):
    fake_sdk.error = make_error()
    lead = waiting_lead()
    assert tasks.process_reply(lead.id, text_message("hello")) == "unclassified"
    assert bot.graph.calls == []
    assert f"[{kind}]" in bot.alerts[0]


def test_buttons_work_without_the_llm(bot, fake_sdk):
    fake_sdk.error = _api_error(400, CREDIT_MESSAGE)
    yes_lead = waiting_lead()
    no_lead = waiting_lead(phone="+15550100003")
    assert tasks.process_reply(yes_lead.id, button("Yes, it was me")) == "qualified"
    assert tasks.process_reply(no_lead.id, button("No, not me", message_id="wamid.B2")) == "not_lead"
    assert fake_sdk.requests == []  # Claude was never called
    assert bot.alerts == []


def test_model_and_effort_come_from_settings(fake_sdk, settings):
    settings.WAQ_ANTHROPIC_MODEL = "claude-sonnet-5-5"
    settings.WAQ_ANTHROPIC_EFFORT = "medium"
    classify.classify_text("hello", "Test Project")
    request = fake_sdk.requests[0]
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "medium"


def test_model_and_effort_defaults(fake_sdk, settings):
    settings.WAQ_ANTHROPIC_MODEL = ""
    settings.WAQ_ANTHROPIC_EFFORT = ""
    classify.classify_text("hello", "Test Project")
    request = fake_sdk.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["effort"] == "low"


# ------------------------------------------------------------- review fixes


def test_autostop_is_on_before_any_external_call(bot, monkeypatch):
    """The third negative reply: by the time the bot answers, the autostop is
    already committed - and it stays on even if the worker dies mid-reply."""
    _negatives(2)
    seen = []

    class WorkerKilled(BaseException):
        pass

    def reply_post(sender, payload):
        seen.append(
            (
                WaqSettings.objects.get(pk=1).autostop_active,
                WaqEvent.objects.filter(kind=WaqEvent.KIND_NEGATIVE_REPLY).count(),
            )
        )
        raise WorkerKilled()

    monkeypatch.setattr(tasks, "post_message", reply_post)
    lead = waiting_lead()
    with pytest.raises(WorkerKilled):
        tasks.process_reply(lead.id, button("No, not me"))
    assert seen == [(True, 3)]
    assert WaqSettings.load().autostop_active is True
    assert bot.bitrix == []  # Bitrix comes after the reply; never reached here


def test_manual_stop_pressed_just_before_the_reply_wins(bot, monkeypatch):
    real_reply_text = tasks._reply_text

    def text_then_stop(cfg, lead, label):
        WaqSettings.objects.filter(pk=1).update(enabled=False)
        return real_reply_text(cfg, lead, label)

    monkeypatch.setattr(tasks, "_reply_text", text_then_stop)
    lead = waiting_lead()
    tasks.process_reply(lead.id, button("Yes, it was me"))
    assert bot.graph.calls == []
    assert bot.bitrix[-1][1]["fields"]["COMMENT"].endswith("ответ клиенту не отправлен: ручной стоп")


def test_who_reply_is_dropped_if_the_dialogue_closed_meanwhile(bot, llm, monkeypatch):
    llm.label = classify.WHO
    real_reply_text = tasks._reply_text

    def text_after_another_message_closed_it(cfg, lead, label):
        # A "Yes, it was me" processed in parallel has just qualified the lead.
        WaqLead.objects.filter(pk=lead.pk).update(state=WaqLead.STATE_QUALIFIED)
        return real_reply_text(cfg, lead, label)

    monkeypatch.setattr(tasks, "_reply_text", text_after_another_message_closed_it)
    lead = waiting_lead()
    tasks.process_reply(lead.id, text_message("who is this?"))
    assert bot.graph.calls == []
    assert bot.open_line == []
    assert bot.bitrix[-1][1]["fields"]["COMMENT"] == "AI: спросил „кто это“ · ответ клиенту не отправлен: диалог уже закрыт"


def test_who_sets_replied_at(bot, llm):
    llm.label = classify.WHO
    lead = waiting_lead()
    tasks.process_reply(lead.id, text_message("who is this?"))
    lead.refresh_from_db()
    assert lead.replied_at is not None
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT


def test_first_reply_time_is_kept(bot, llm):
    llm.label = classify.WHO
    lead = waiting_lead()
    tasks.process_reply(lead.id, text_message("who is this?", message_id="wamid.T1"))
    lead.refresh_from_db()
    first = lead.replied_at
    tasks.process_reply(lead.id, button("Yes, it was me", message_id="wamid.B9"))
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_QUALIFIED
    assert lead.replied_at == first


def test_waq_tasks_and_beat_use_their_own_queue(settings):
    assert tasks.QUEUE == "waq"
    for task in (tasks.send_confirmation, tasks.run_schedule, tasks.process_reply):
        assert task.queue == "waq"
    assert settings.CELERY_BEAT_SCHEDULE["waq-run-schedule"]["options"]["queue"] == "waq"
    assert "waq" in [queue.name for queue in settings.CELERY_QUEUES]


def test_compose_has_a_waq_worker_and_keeps_the_others():
    from pathlib import Path

    import yaml
    from django.conf import settings as django_settings

    compose = yaml.safe_load(Path(django_settings.BASE_DIR, "docker-compose.photon.yml").read_text())
    services = compose["services"]
    waq = services["worker-waq"]
    assert waq["environment"]["CELERY_QUEUE"] == "waq"
    assert waq["environment"]["CELERY_CONCURRENCY"] == "1"
    assert waq["command"] == "/start-celeryworker"
    assert services["worker-misc"]["environment"]["CELERY_QUEUE"] == "bitrix,default,bitbot,olx,waweb"
    assert services["worker-waba"]["environment"]["CELERY_QUEUE"] == "waba,waba_messages"
