"""Exact numeric spellings for the cited-text check, not factual inference."""
from __future__ import annotations

import re


_DIGITS = str.maketrans("零〇一二三四五六七八九", "00123456789")
_TOKEN = re.compile(r"[0-9零〇一二两三四五六七八九十百千万亿]+(?:[.点][0-9零〇一二两三四五六七八九十百千万亿]+)*")
# In these measurement names 千 belongs to the unit, not the preceding
# numeral. Keep the list explicit: 千元/千人 still contain a numeric 千.
_KILO_UNITS = ("千克", "千米", "千瓦", "千伏", "千安", "千欧", "千赫",
               "千焦", "千帕", "千牛", "千升", "千卡")


def _integer_spelling(value: int, *, initial: bool = True) -> str:
    """Canonical explicit place values, including zero in omitted places."""
    if not 0 <= value < 100000000:
        raise ValueError("unsupported_integer")
    if value >= 10000:
        high, low = divmod(value, 10000)
        return (_integer_spelling(high) + "万" +
                (("零" if low < 1000 else "") + _integer_spelling(low, initial=False) if low else ""))
    names, result, zero = "零一二三四五六七八九", "", False
    for divisor, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit, value = divmod(value, divisor)
        if digit:
            result += ("零" if zero else "")
            result += ("" if divisor == 10 and digit == 1 and not result and initial else names[digit]) + unit
            zero = False
        elif result and value:
            zero = True
    return result or "零"


def _integer_key(text: str) -> str | None:
    if re.fullmatch(r"[0-9]+", text):
        return text  # Codes retain leading zeroes.
    if re.fullmatch(r"[零〇一二三四五六七八九]+", text):
        return text.translate(_DIGITS)
    if text == "两":
        return "2"
    if not re.fullmatch(r"[零〇一二两三四五六七八九十百千万]+", text):
        return None
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    spelling = re.sub(r"两(?=[百千万])", "二", text.replace("〇", "零"))
    total = section = number = 0
    last_unit = 10000
    for char in spelling:
        if char in digits:
            number = digits[char]
        elif char in "十百千":
            unit = {"十": 10, "百": 100, "千": 1000}[char]
            if unit >= last_unit:
                return None
            section += (number or 1) * unit
            number, last_unit = 0, unit
        elif char == "万":
            total += (section + number or 1) * 10000
            section = number = 0
            last_unit = 10000
        else:
            return None
    value = total + section + number
    try:
        # Round-trip rejects 一百二, 一二三十, 万一, repeated units, etc.
        return str(value) if _integer_spelling(value) == spelling else None
    except ValueError:
        return None


def _number_key(text: str) -> str | None:
    parts = re.split(r"[.点]", text)
    if len(parts) > 2:
        return None
    integer = _integer_key(parts[0])
    if integer is None:
        return None
    if len(parts) == 1:
        return integer
    fraction = parts[1]
    if re.fullmatch(r"[0-9]+", fraction):
        return integer + "." + fraction
    if re.fullmatch(r"[零〇一二三四五六七八九]+", fraction):
        return integer + "." + fraction.translate(_DIGITS)
    return None


def numeric_tokens(text: str, *, claims: bool = False) -> list[str | None]:
    """Read entire numbers; unsupported spellings never provide evidence.

    Preserve the existing claim scope for single Chinese digits in ordinary
    phrases. Add only unambiguous place-value and decimal readings. This does
    not interpret quantities, units, successful attempts, or uncited text.
    """
    result = []
    for match in _TOKEN.finditer(text):
        token = match.group()
        number_end = match.end()
        if token.endswith("千") and text.startswith(_KILO_UNITS, number_end - 1):
            token, number_end = token[:-1], number_end - 1
            if not token:
                continue
        key = _number_key(token)
        # Never take a suffix/prefix out of a malformed decimal expression.
        after = text[number_end:number_end + 2]
        if ((match.start() and text[match.start() - 1] == ".") or
                (after.startswith(".") and len(after) > 1 and
                 (after[1] == "." or _TOKEN.match(after[1:])))):
            key = None
        if claims:
            if len(token) == 1 and not token.isascii():
                continue
            if key is None and not re.search(r"[0-9]|[零〇一二三四五六七八九]{2,}", token):
                continue  # Ambiguous Chinese phrases are left to semantic review.
            result.append(key)
        elif key is not None:
            result.append(key)
    return result
