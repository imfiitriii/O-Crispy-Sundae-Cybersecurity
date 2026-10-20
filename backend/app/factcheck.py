"""Google Fact Check Tools API lookup for claims extracted by Gemini."""
import logging
import re

import httpx

from . import config

log = logging.getLogger("scrollsafe.factcheck")

URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"

# Checked first, because e.g. "partly false" also contains "false".
MISLEADING_WORDS = (
    "misleading", "mostly false", "partly false", "partially false", "half true", "mixture",
    "missing context", "out of context", "exaggerated", "unproven", "unverified", "mengelirukan",
    "separuh benar", "tidak tepat",
)
FALSE_WORDS = (
    "false", "fake", "hoax", "scam", "incorrect", "fabricated", "pants on fire", "wrong", "not true",
    "palsu", "tidak benar", "penipuan", "berita palsu", "salah",
)
STOPWORDS = set(
    "the a an and or of to in on for is are was were be this that it with by from as at your you "
    "yang dan di ke dari untuk ini itu dengan ada akan boleh anda".split()
)


def classify_rating(rating: str) -> str:
    r = (rating or "").lower()
    if any(w in r for w in MISLEADING_WORDS):
        return "misleading"
    if any(w in r for w in FALSE_WORDS):
        return "false"
    return "other"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()) if w not in STOPWORDS and len(w) > 2}


def overlap(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def available() -> bool:
    return bool(config.FACTCHECK_API_KEY)


async def check_claims(claims: list[dict]) -> list[dict]:
    """Search each claim (English and Malay query). Returns matching fact-checks.

    The API matches loosely, so a result only counts as a "match" when its claim
    text shares enough words with ours; weaker results are kept as "related".
    """
    if not available() or not claims:
        return []
    hits: list[dict] = []
    seen_urls: set[str] = set()
    async with httpx.AsyncClient(timeout=8) as client:
        for claim in claims[:3]:
            queries = {q for q in (claim.get("query_en"), claim.get("query_ms"), claim.get("text")) if q}
            for q in queries:
                try:
                    r = await client.get(URL, params={"query": q, "key": config.FACTCHECK_API_KEY, "pageSize": 5})
                    r.raise_for_status()
                except Exception as e:  # noqa: BLE001
                    log.warning("Fact check query failed: %s", e)
                    continue
                for found in r.json().get("claims", []):
                    for review in found.get("claimReview", []):
                        url = review.get("url", "")
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)
                        score = max(overlap(q, found.get("text", "")), overlap(claim.get("text", ""), found.get("text", "")))
                        hits.append(
                            {
                                "our_claim": claim.get("text", ""),
                                "fact_checked_claim": found.get("text", ""),
                                "rating": review.get("textualRating", ""),
                                "verdict": classify_rating(review.get("textualRating", "")),
                                "publisher": (review.get("publisher") or {}).get("name", ""),
                                "url": url,
                                "match": "match" if score >= 0.4 else "related",
                            }
                        )
    return hits
