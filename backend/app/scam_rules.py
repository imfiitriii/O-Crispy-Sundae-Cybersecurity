"""Keyword-based scam check, based on public MCMC / PDRM / Bank Negara scam warnings."""
import json
import re
from pathlib import Path

_DATA = json.loads((Path(__file__).parent / "scam_keywords.json").read_text(encoding="utf-8"))
STRONG = [p.lower() for p in _DATA["strong"]]
WEAK = [p.lower() for p in _DATA["weak"]]


def _contains(text: str, phrase: str) -> bool:
    # Word boundaries for plain words, so "cure" doesn't match "secure".
    if re.fullmatch(r"[a-z0-9 ]+", phrase):
        return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None
    return phrase in text


def check(*texts: str) -> dict:
    text = " ".join(t for t in texts if t).lower()
    strong = [p for p in STRONG if _contains(text, p)]
    weak = [p for p in WEAK if _contains(text, p)]
    if strong or len(weak) >= 2:
        level = "high"
    elif weak:
        level = "low"
    else:
        level = "none"
    return {"level": level, "strong": strong, "weak": weak}
