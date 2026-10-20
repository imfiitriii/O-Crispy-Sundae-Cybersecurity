"""Combine all signals into one GREEN / YELLOW / RED verdict with plain-language reasons."""
from . import config

# The open-source classifiers are poorly calibrated on real TikTok frames (compression,
# filters, burned-in captions), so on their own they can only give yellow. Red needs a
# second, independent signal: Gemini seeing the same problem, a fact-check, or scam words.
RISKY_TOPICS = {"health", "money", "government", "safety"}

TITLES = {"green": "Looks OK", "yellow": "Be careful", "red": "Warning"}


def _weighted(pairs: list[tuple[float | None, float]]) -> float | None:
    vals = [(v, w) for v, w in pairs if v is not None]
    if not vals:
        return None
    total = sum(w for _, w in vals)
    return sum(v * w for v, w in vals) / total


def _num(x) -> float | None:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return None


def combine(deepfake: dict, ai_image: dict, gemini: dict | None, facts: list[dict], scam: dict,
            platform_labels: list[str]) -> dict:
    g = gemini or {}
    reasons: list[tuple[int, str, str]] = []  # (priority, level, text); lower priority shows first

    have_gemini = gemini is not None

    # 1. Deepfake: dedicated classifier on face crops + Gemini consistency, weighted equally
    g_df = _num(g.get("deepfake_likelihood"))
    if g.get("face_consistent_across_frames") is False:
        g_df = max(g_df or 0.0, 0.7)
    face_seen = deepfake.get("faces_found", 0) > 0 or g.get("face_visible") is True
    deepfake_score = _weighted([(deepfake.get("score"), 0.5), (g_df, 0.5)]) if face_seen else None
    gemini_sees_deepfake = bool(g.get("deepfake_signs")) or g.get("face_consistent_across_frames") is False \
        or (g_df or 0) >= 0.5

    # 2. AI-generated: AI-image classifier + Gemini visual signs, weighted equally; a platform label wins
    ai_score = _weighted([(ai_image.get("score"), 0.5), (_num(g.get("ai_generated_likelihood")), 0.5)])
    ai_labelled = bool(platform_labels) or g.get("ai_label_visible") is True
    if ai_labelled:
        ai_score = 1.0

    # 3. False information and scams
    false_hits = [f for f in facts if f["match"] == "match" and f["verdict"] == "false"]
    misleading_hits = [f for f in facts if f["match"] == "match" and f["verdict"] == "misleading"]
    claims = g.get("claims") or []
    risky_claims = [c for c in claims if c.get("topic") in RISKY_TOPICS]
    gemini_scam = g.get("scam_signs") or []
    risk = g.get("misinformation_risk", "none")

    status = "green"

    def raise_to(level: str) -> None:
        nonlocal status
        order = ["green", "yellow", "red"]
        if order.index(level) > order.index(status):
            status = level

    if false_hits:
        f = false_hits[0]
        raise_to("red")
        reasons.append((0, "red", f"Fact-checkers ({f['publisher'] or 'a fact-checker'}) rated a claim in this video as \"{f['rating']}\"."))
    if scam["level"] == "high":
        raise_to("red")
        reasons.append((1, "red", "This video uses words that are common in scams. Do not send money or personal details."))
    if deepfake_score is not None and deepfake_score >= config.RED_AT and gemini_sees_deepfake:
        raise_to("red")
        reasons.append((2, "red", "The person's face may have been digitally changed (possible deepfake)."))
    if ai_score is not None and ai_score >= config.AI_WITH_RISK_RED_AT and (gemini_scam or risk in ("medium", "high") or risky_claims):
        raise_to("red")
        reasons.append((3, "red", "This video looks AI-made and makes claims about money, health or the government."))
    if risk == "high" and gemini_scam:
        raise_to("red")
        reasons.append((4, "red", "This looks like a scam: " + ", ".join(gemini_scam[:2]) + "."))

    if misleading_hits:
        f = misleading_hits[0]
        raise_to("yellow")
        reasons.append((5, "yellow", f"Fact-checkers ({f['publisher'] or 'a fact-checker'}) rated a similar claim \"{f['rating']}\"."))
    df_yellow_at = config.DEEPFAKE_YELLOW_AT if have_gemini else config.LOCAL_ONLY_YELLOW_AT
    if deepfake_score is not None and status != "red" and deepfake_score >= df_yellow_at:
        raise_to("yellow")
        reasons.append((6, "yellow", "Some signs the face may be edited. Be careful before believing it."))
    ai_yellow_at = config.AI_YELLOW_AT if have_gemini else config.LOCAL_ONLY_YELLOW_AT
    if ai_score is not None and (ai_score >= ai_yellow_at or ai_labelled):
        raise_to("yellow")
        reasons.append((7, "yellow", "This video is likely made or edited with AI." if not ai_labelled
                        else "This video is labelled as AI-generated."))
    if gemini_scam and risk != "high":
        raise_to("yellow")
        reasons.append((8, "yellow", "Possible scam signs: " + ", ".join(gemini_scam[:2]) + "."))
    if scam["level"] == "low":
        raise_to("yellow")
        reasons.append((9, "yellow", "Contains words often used in scams. Be careful with links and WhatsApp contacts."))
    # Ordinary news videos make claims too, so unverified claims only matter when
    # Gemini also rates the misinformation risk as medium or high.
    if risk in ("medium", "high") and not false_hits and not misleading_hits:
        raise_to("yellow")
        reasons.append((10, "yellow", "Makes claims about health, money or the government that we couldn't verify. Check a trusted source."))

    reasons.sort(key=lambda r: r[0])
    simple = g.get("reason_simple")
    if status == "green":
        message = "No warning signs found. Still, stay careful with offers and claims."
    else:
        message = reasons[0][2] if reasons else (simple or "Be careful with this video.")

    return {
        "status": status,
        "title": TITLES[status],
        "message": message,
        "details": [r[2] for r in reasons] + ([f"AI helper says: {simple}"] if simple else []),
        "scores": {
            "deepfake": None if deepfake_score is None else round(deepfake_score, 3),
            "ai_generated": None if ai_score is None else round(ai_score, 3),
            "deepfake_model": deepfake.get("score"),
            "ai_image_model": ai_image.get("score"),
            "faces_found": deepfake.get("faces_found", 0),
        },
        "claims": claims,
        "fact_checks": facts,
        "scam_keywords": scam,
        "signals_used": {
            "deepfake_model": deepfake.get("available", False),
            "ai_image_model": ai_image.get("available", False),
            "gemini": gemini is not None,
        },
        "model_version": config.MODEL_VERSION,
    }


def apply_reports(result: dict, reports: dict) -> dict:
    """Community reports can raise a warning to yellow but never lower it."""
    result = dict(result)
    result["reports"] = reports
    if reports["count"] >= config.REPORT_THRESHOLD and result["status"] == "green":
        result["status"] = "yellow"
        result["title"] = TITLES["yellow"]
        result["message"] = f"{reports['count']} people reported this video. Be careful."
        result["details"] = [result["message"]] + list(result.get("details", []))
    return result
