"""Security and entity identifiers with check-digit generation and validation.

* ISIN (ISO 6166): 2-letter country + 9-char NSIN + Luhn check digit over letter-expanded string.
* CUSIP: 8 chars + modulus-10 "double-add-double" check digit.
* SEDOL: 6 chars (no vowels) + weighted (1,3,1,7,3,9) check digit.
* FIGI-shaped: 12 chars, third char 'G', ISIN-style check digit. Our prefix is "MKG" (mock).
* LEI (ISO 17442): 18 chars + 2 ISO 7064 MOD 97-10 check digits.

Fictitious ranges so we never collide with real securities: CUSIP issuer numbers 990000-999999
are reserved for internal use, SEDOLs starting with 9 are user-defined, and LEIs use a "MOCK"
prefix that no LOU issues.
"""

from __future__ import annotations

import re
import string

ALNUM = string.digits + string.ascii_uppercase
SEDOL_CHARS = "0123456789BCDFGHJKLMNPQRSTVWXYZ"  # no vowels
FIGI_CHARS = "0123456789BCDFGHJKLMNPQRSTVWXYZ"


def _char_value(c: str) -> int:
    if c.isdigit():
        return int(c)
    if c.isalpha():
        return ord(c.upper()) - ord("A") + 10
    return {"*": 36, "@": 37, "#": 38}[c]


# -- ISIN ---------------------------------------------------------------------------------
def isin_check_digit(body11: str) -> str:
    digits = "".join(str(_char_value(c)) for c in body11.upper())
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
        total += d // 10 + d % 10
    return str((10 - total % 10) % 10)


def make_isin(country: str, nsin: str) -> str:
    nsin = nsin.upper().rjust(9, "0")
    body = country.upper() + nsin
    return body + isin_check_digit(body)


def is_valid_isin(value: str) -> bool:
    v = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", v):
        return False
    return isin_check_digit(v[:11]) == v[11]


# -- CUSIP --------------------------------------------------------------------------------
def cusip_check_digit(body8: str) -> str:
    total = 0
    for i, c in enumerate(body8.upper()):
        v = _char_value(c)
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return str((10 - total % 10) % 10)


def make_cusip(body8: str) -> str:
    return body8.upper() + cusip_check_digit(body8)


def is_valid_cusip(value: str) -> bool:
    v = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9*@#]{8}[0-9]", v):
        return False
    return cusip_check_digit(v[:8]) == v[8]


# -- SEDOL --------------------------------------------------------------------------------
_SEDOL_WEIGHTS = (1, 3, 1, 7, 3, 9)


def sedol_check_digit(body6: str) -> str:
    total = sum(_char_value(c) * w for c, w in zip(body6.upper(), _SEDOL_WEIGHTS, strict=True))
    return str((10 - total % 10) % 10)


def make_sedol(body6: str) -> str:
    return body6.upper() + sedol_check_digit(body6)


def is_valid_sedol(value: str) -> bool:
    v = value.strip().upper()
    if not re.fullmatch(r"[0-9BCDFGHJKLMNPQRSTVWXYZ]{6}[0-9]", v):
        return False
    return sedol_check_digit(v[:6]) == v[6]


# -- FIGI-shaped --------------------------------------------------------------------------
def make_figi(body8: str, prefix: str = "MKG") -> str:
    body = prefix + body8.upper()
    return body + isin_check_digit(body)


def is_valid_figi(value: str) -> bool:
    v = value.strip().upper()
    if not re.fullmatch(r"[B-DF-HJ-NP-TV-Z0-9]{2}G[B-DF-HJ-NP-TV-Z0-9]{8}[0-9]", v):
        return False
    return isin_check_digit(v[:11]) == v[11]


# -- LEI ----------------------------------------------------------------------------------
def _mod97(s: str) -> int:
    return int("".join(str(_char_value(c)) for c in s)) % 97


def make_lei(body18: str) -> str:
    body = body18.upper()
    check = 98 - _mod97(body + "00")
    return f"{body}{check:02d}"


def is_valid_lei(value: str) -> bool:
    v = value.strip().upper()
    return bool(re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", v)) and _mod97(v) == 1


# -- detection / suggestions --------------------------------------------------------------
def detect(value: str) -> str | None:
    """Best guess of an identifier's type from its shape and check digit."""
    v = value.strip().upper()
    if len(v) == 12 and is_valid_isin(v):
        return "ISIN"
    if len(v) == 12 and is_valid_figi(v):
        return "FIGI"
    if len(v) == 9 and is_valid_cusip(v):
        return "CUSIP"
    if len(v) == 7 and is_valid_sedol(v):
        return "SEDOL"
    if len(v) == 20 and is_valid_lei(v):
        return "LEI"
    return None


def suggest_fix(value: str) -> list[str]:
    """For a typo'd ISIN/CUSIP/SEDOL, propose same-length variants that pass the check digit.

    Tries replacing the check digit, then single-character substitutions and adjacent swaps.
    """
    v = value.strip().upper()
    validators = {12: is_valid_isin, 9: is_valid_cusip, 7: is_valid_sedol}
    check = validators.get(len(v))
    if check is None:
        return []
    out: list[str] = []
    for d in string.digits:
        cand = v[:-1] + d
        if cand != v and check(cand):
            out.append(cand)
    for i in range(len(v) - 2):
        cand = v[:i] + v[i + 1] + v[i] + v[i + 2 :]
        if cand != v and check(cand):
            out.append(cand)
    return out
