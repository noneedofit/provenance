"""Text normalisation shared by every name/address comparison.

One fold for Norwegian text everywhere: "ø" -> "o", "å" -> "a", "æ" -> "ae", then accents stripped and
case folded. Comparisons on either side must use the same fold: when website verification folded "ø"
away while the legal-name side mapped it to "o", "Trøndelag" never matched "Trondelag".
"""
from __future__ import annotations

import unicodedata

NORWEGIAN_FOLD = str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"})


def fold(text: str | None) -> str:
    """ASCII, case-folded form of `text` with Norwegian letters transliterated."""
    folded = str(text or "").translate(NORWEGIAN_FOLD)
    return unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode().casefold()
