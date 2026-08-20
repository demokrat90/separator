"""Pure-logic tests: no database, no Django settings needed."""

from separator.attribution.tokens import (
    ALPHABET,
    build_wa_text_with_token,
    extract_token,
    generate_token,
    normalize_ga_client_id,
    normalize_phone,
    text_stats,
)


def test_generated_token_avoids_confusable_characters():
    for _ in range(200):
        token = generate_token()
        assert len(token) == 6
        assert set(token) <= set(ALPHABET)
        assert not set(token) & set("O0I1")


def test_extract_token_from_prefilled_message():
    assert extract_token("Hello Photon! I'm interested in PH-204. #K7QX9M") == "K7QX9M"


def test_extract_token_five_chars_and_trailing_punctuation():
    assert extract_token("hi #ABC23.") == "ABC23"


def test_extract_token_lower_case_is_still_a_code():
    assert extract_token("hi #k7qx9m") == "K7QX9M"


def test_extract_token_returns_none_without_pattern():
    assert extract_token("Hello, do you have 2 bedroom apartments?") is None
    assert extract_token("") is None
    assert extract_token(None) is None
    # Too short / too long to be a code.
    assert extract_token("#AB2") is None
    assert extract_token("#ABCDEFGH") is None


def test_wa_text_falls_back_to_generic_subject():
    assert build_wa_text_with_token("K7QX9M") == (
        "Hello Photon! I'm interested in your properties. #K7QX9M"
    )
    assert build_wa_text_with_token("K7QX9M", "PH-204") == (
        "Hello Photon! I'm interested in PH-204. #K7QX9M"
    )


def test_normalize_ga_client_id_strips_cookie_prefix():
    assert normalize_ga_client_id("GA1.1.1234567.7654321") == "1234567.7654321"
    assert normalize_ga_client_id("1234567.7654321") == "1234567.7654321"
    assert normalize_ga_client_id(None) is None


def test_normalize_phone_produces_e164():
    assert normalize_phone("971521892809") == "+971521892809"
    assert normalize_phone("+971521892809") == "+971521892809"
    assert normalize_phone(None) is None


def test_text_stats_never_returns_text():
    length, digest = text_stats("hello")
    assert length == 5
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert text_stats(None) == (0, None)
