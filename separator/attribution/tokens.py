"""Pure helpers: token alphabet, generation, extraction, normalization.

Deliberately free of Django imports so the logic can be unit-tested standalone.
"""

import hashlib
import re
import secrets

# O/0 and I/1 removed: they are the pairs people mistype when copying a code.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_LENGTH = 6

# Spec regex (uppercase only). Kept verbatim for the strict pass.
TOKEN_RE = re.compile(r"#([A-Z2-9]{5,6})\b")
# Second pass: people (and some keyboards) send the code in lower case.
TOKEN_RE_LOOSE = re.compile(r"#([A-Za-z2-9]{5,6})\b")


def generate_token(length: int = TOKEN_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def extract_token(text):
    """Return the code found in a message, upper-cased, or None.

    Strict (spec) pass first; if it finds nothing, a case-insensitive pass runs,
    because a lower-cased code is a found-but-mistyped code, not a missing one.
    """
    if not text:
        return None
    match = TOKEN_RE.search(text)
    if match:
        return match.group(1)
    match = TOKEN_RE_LOOSE.search(text)
    if match:
        return match.group(1).upper()
    return None


def build_wa_text(listing=None, brand="Photon"):
    subject = listing or "your properties"
    return f"Hello {brand}! I'm interested in {subject}."


def build_wa_text_with_token(token, listing=None, brand="Photon"):
    return f"{build_wa_text(listing, brand)} #{token}"


def normalize_ga_client_id(value):
    """`GA1.1.123.456` (raw _ga cookie) -> `123.456`. Anything else is kept as is."""
    if not value:
        return value
    value = str(value).strip()
    parts = value.split(".")
    if len(parts) >= 4 and parts[0].upper().startswith("GA"):
        return ".".join(parts[-2:])
    return value


def text_stats(text):
    """(length, sha256) of a message - the text itself is never persisted."""
    if not text:
        return 0, None
    encoded = str(text).encode("utf-8")
    return len(str(text)), hashlib.sha256(encoded).hexdigest()


def normalize_phone(value):
    """E.164 with a leading '+'. Values are otherwise kept as they arrived."""
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    if value.startswith("+"):
        return value
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    return f"+{digits}"
