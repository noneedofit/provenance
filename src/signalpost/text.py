"""Text normalisation shared by every name/address comparison.

One fold for Norwegian text everywhere: "ø" -> "o", "å" -> "a", "æ" -> "ae", then accents stripped and
case folded. Comparisons on either side must use the same fold: when website verification folded "ø"
away while the legal-name side mapped it to "o", "Trøndelag" never matched "Trondelag".
"""
from __future__ import annotations

import re
import unicodedata

NORWEGIAN_FOLD = str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"})


def fold(text: str | None) -> str:
    """ASCII, case-folded form of `text` with Norwegian letters transliterated."""
    folded = str(text or "").translate(NORWEGIAN_FOLD)
    return unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode().casefold()


_FORMS_UPPER = ("AS", "ASA", "ANS", "DA", "SA", "NUF", "IKS", "BA")
_SENTENCE_END = re.compile(r"(?:^|[.!?:;|•\n\"'«(]\s*)$")
_PREV_WORD = re.compile(r"([A-ZÆØÅ][\w&'-]*)\s+$")


def find_registered_name(text: str, legal_name: str) -> "re.Match[str] | None":
    """Where `text` names the company by its full registered name, legal form included ("Hydema AS").
    The name's words match in any case, but the legal form must be written in capitals (so English "mark
    as read" is not MARK AS), and the hit must not be the tail of a longer name: "Rapp Hydema AS" is a
    different company from HYDEMA AS. A capitalised word right before the name counts as part of a longer
    name unless it starts a sentence. None when the name has no legal form."""
    tokens = re.findall(r"\w+", str(legal_name or ""))
    if len(tokens) < 2 or tokens[-1].upper() not in _FORMS_UPPER:
        return None
    words = r"[^\w\n]{1,3}".join(re.escape(t) for t in tokens[:-1])
    pattern = re.compile(r"(?<!\w)(?i:" + words + r")[^\w\n]{1,3}" + re.escape(tokens[-1].upper()) + r"(?!\w)")
    for m in pattern.finditer(text or ""):
        before = text[: m.start()]
        prev = _PREV_WORD.search(before)
        if prev is not None and not _SENTENCE_END.search(before[: prev.start()]):
            continue  # "Rapp Hydema AS": our name is the end of someone else's
        return m
    return None
