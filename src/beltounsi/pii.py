"""Mask e-mail addresses and phone numbers in post text before it is stored or published."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Tunisian numbers: 8 digits written together or grouped 2-3-3 / 2-2-2-2 with spaces or dots, optional
# +216 / 00216 prefix. Dashes and line breaks are not separators: they made year ranges ("2010-2011"),
# dates and lists of years match on real posts.
PHONE_RE = re.compile(
    r"(?<![\w.,+-])(?:(?:\+|00)216[ .]?)?"
    r"(?:[2-9]\d{7}|[2-9]\d[ .]\d{3}[ .]\d{3}|[2-9]\d(?:[ .]\d{2}){3})"
    r"(?![\w.,-])"
)


def scrub(text: str) -> tuple[str, int]:
    """Returns (masked text, number of replacements)."""
    text, n_email = EMAIL_RE.subn("<EMAIL>", text)
    text, n_phone = PHONE_RE.subn("<PHONE>", text)
    return text, n_email + n_phone
