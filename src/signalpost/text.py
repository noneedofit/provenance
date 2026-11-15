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
_PREV_WORD = re.compile(r"([A-ZÆØÅ][\w&'-]*)[ \t]+$")
# Capitalised words that start a sentence before a company name without being part of it.
_SENTENCE_WORDS = {
    "selskapet", "firmaet", "bedriften", "daglig", "styreleder", "i", "og", "men", "nå", "her", "der", "da", "når",
    "hos", "for", "med", "til", "fra", "av", "om", "på", "etter", "både", "også", "ifølge", "det", "den", "dette",
    "the", "a", "an", "and", "but", "at", "in", "for", "with", "from", "by", "according", "company", "firm",
    "nyheter", "pressemelding", "oppdatert", "publisert",
}


def find_registered_name(text: str, legal_name: str) -> "re.Match[str] | None":
    """Where `text` names the company by its full registered name ("Hydema AS", "Valentinlyst Borettslag").
    The name's words match in any case, but a legal form must be written in capitals (so English "mark as
    read" is not MARK AS), and the hit must not be the tail of a longer name: "Rapp Hydema AS" is another
    company than HYDEMA AS. A capitalised word or a hyphenated prefix right before the name makes it part of
    a longer name, unless that word is a common sentence word ("Selskapet", "The"). Names of one word, or
    one word plus a legal form, are matched only with the form."""
    tokens = re.findall(r"\w+", str(legal_name or ""))
    if len(tokens) < 2:
        return None
    sep = r"[^\w\n]{1,3}"
    if tokens[-1].upper() in _FORMS_UPPER:
        body = r"(?i:" + sep.join(re.escape(t) for t in tokens[:-1]) + r")" + sep + re.escape(tokens[-1].upper())
    else:
        body = r"(?i:" + sep.join(re.escape(t) for t in tokens) + r")"
    for m in re.finditer(r"(?<![\w-])" + body + r"(?!\w)", text or ""):
        before = text[: m.start()]
        if before.endswith("-"):
            continue
        prev = _PREV_WORD.search(before)
        if prev is not None and prev.group(1).casefold() not in _SENTENCE_WORDS:
            continue  # "Rapp Hydema AS": our name is the end of someone else's
        return m
    return None
