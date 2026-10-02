"""Post length as X counts it, which is not the same as len()."""

import re
import unicodedata

MAX_POST_LENGTH = 280
# Every link is shortened to a t.co URL of this length, whatever its real length
_URL_LENGTH = 23
_URL = re.compile(r"https?://\S+")
# Code points X counts as one character; everything else (emoji, CJK, ...) counts as two.
# From the published twitter-text configuration (v3).
_SINGLE_WEIGHT_RANGES = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))


def weighted_length(text: str) -> int:
    """Length of a post the way X measures it against the 280 limit."""
    text = unicodedata.normalize("NFC", text)
    urls = _URL.findall(text)
    remainder = _URL.sub("", text)
    length = len(urls) * _URL_LENGTH
    for char in remainder:
        code = ord(char)
        single = any(low <= code <= high for low, high in _SINGLE_WEIGHT_RANGES)
        length += 1 if single else 2
    return length
