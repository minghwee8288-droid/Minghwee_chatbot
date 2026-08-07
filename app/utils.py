"""Shared helpers: phone normalisation and the NRIC guard."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Singapore NRIC / FIN: S/T/F/G/M + 7 digits + checksum letter.
NRIC_PATTERN = re.compile(r"\b[STFGMstfgm]\d{7}[A-Za-z]\b")
NRIC_REDACTION = "[REDACTED-ID]"

_DIGITS = re.compile(r"\D+")


def contains_nric(text: str | None) -> bool:
    return bool(text) and bool(NRIC_PATTERN.search(text or ""))


def redact_nric(text: str | None, *, context: str = "") -> str:
    """Strip NRIC/FIN numbers before anything reaches the LLM or the KB search."""
    if not text:
        return text or ""
    if not NRIC_PATTERN.search(text):
        return text
    logger.warning("NRIC pattern detected and redacted%s", f" ({context})" if context else "")
    return NRIC_PATTERN.sub(NRIC_REDACTION, text)


_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    """Lowercase, strip punctuation and collapse whitespace, for comparing
    message bodies that may differ only in formatting."""
    lowered = (text or "").lower()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", lowered)).strip()


def digits_only(phone: str | None) -> str:
    return _DIGITS.sub("", phone or "")


def normalize_phone(raw: str | None) -> str:
    """Return an E.164-ish string ('+6591234567') from any Whapi identifier.

    Accepts '6591234567@s.whatsapp.net', '65 9123 4567', '+6591234567', etc.
    Bare 8-digit numbers are assumed Singaporean and prefixed with 65.
    """
    if not raw:
        return ""
    value = raw.split("@", 1)[0]
    digits = digits_only(value)
    if not digits:
        return ""
    if len(digits) == 8 and digits[0] in "3689":
        digits = f"65{digits}"
    return f"+{digits}"


def phone_variants(phone: str | None) -> list[str]:
    """Every spelling of a number we might find in the platform tables.

    Employer/candidate rows were entered by humans over several years, so the
    same number shows up as '+6591234567', '6591234567' and '91234567'.
    """
    digits = digits_only(phone)
    if not digits:
        return []
    variants = {digits, f"+{digits}"}
    if digits.startswith("65") and len(digits) > 8:
        local = digits[2:]
        variants.update({local, f"+{local}"})
    return [v for v in variants if v]
