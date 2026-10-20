"""One Gemini Flash call per video: visual checks, on-screen text, claims and scam signs."""
import json
import logging

from PIL import Image

from . import config
from .imaging import to_jpeg_bytes

log = logging.getLogger("scrollsafe.gemini")

PROMPT = """You are a careful media-safety assistant helping older adults and first-time internet users.
You are given {n} frames sampled in order from one short social media video, plus its caption and creator name.

Caption: {caption}
Creator: {author}
Labels shown by the platform: {labels}

Analyse the frames and text, then return ONLY a JSON object with exactly these keys:
{{
  "face_visible": true/false,
  "face_consistent_across_frames": true/false/null,   // null if no face. false if face shape, skin tone, glasses, earrings or hairline change between frames
  "deepfake_signs": [string],          // visible signs of face swap or editing, e.g. "blurry jawline", "mouth looks pasted on"; [] if none
  "deepfake_likelihood": number 0-1,   // how likely a real person's face/lips were digitally altered
  "ai_visual_signs": [string],         // warped hands/text, melting objects, impossible physics, plastic skin, AI watermark; [] if none
  "ai_label_visible": true/false,      // an "AI-generated" / "AI info" label or AI tool watermark is visible
  "ai_generated_likelihood": number 0-1,
  "on_screen_text": string,            // main text visible in the frames, short
  "claims": [                          // factual claims about health, money, government, news or safety. Ignore jokes, opinions, product taste.
    {{"text": string, "topic": "health|money|government|news|safety|other", "query_en": string, "query_ms": string}}
  ],                                   // query_en / query_ms: a short search query for the claim in English and Malay. Max 3 claims.
  "scam_signs": [string],              // e.g. "guaranteed returns", "celebrity endorses investment", "asks to message on WhatsApp", "urgent limited offer"; [] if none
  "misinformation_risk": "none|low|medium|high",
  "reason_simple": string              // one short sentence (max 25 words) a grandparent can understand. Do not claim certainty.
}}
Be calibrated: ordinary videos of real people should get low likelihoods. Do not invent claims."""


_client_instance = None


def _client():
    # Keep one client alive. A client created inline gets garbage-collected (and its
    # connection closed) before the request is sent: "client has been closed".
    global _client_instance
    if _client_instance is None:
        from google import genai
        from google.genai import types

        _client_instance = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=int(config.GEMINI_TIMEOUT_S * 1000)),
        )
    return _client_instance


def available() -> bool:
    return bool(config.GEMINI_API_KEY)


def analyze(frames: list[Image.Image], caption: str, author: str, labels: list[str]) -> dict | None:
    """Return Gemini's JSON analysis, or None if Gemini is unavailable or fails."""
    if not available() or not frames:
        return None
    from google.genai import types

    # 3 frames are enough for the consistency check and keep the request small and fast.
    picked = frames if len(frames) <= 3 else [frames[0], frames[len(frames) // 2], frames[-1]]
    parts = [types.Part.from_bytes(data=to_jpeg_bytes(f), mime_type="image/jpeg") for f in picked]
    prompt = PROMPT.format(
        n=len(picked),
        caption=(caption or "(none)")[:1500],
        author=author or "(unknown)",
        labels=", ".join(labels) or "(none)",
    )
    # No tools are used, so automatic function calling is off (this also silences its log line).
    base = dict(response_mime_type="application/json", temperature=0.2,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    configs = []
    try:
        # Low thinking is much faster and plenty for this task. Older SDKs or models
        # may not support it, so fall back to the default config if it's rejected.
        configs.append(types.GenerateContentConfig(**base, thinking_config=types.ThinkingConfig(thinking_level="low")))
    except Exception:  # noqa: BLE001
        pass
    configs.append(types.GenerateContentConfig(**base))

    def ask(model: str) -> dict | None:
        for i, cfg in enumerate(configs):
            try:
                resp = _client().models.generate_content(model=model, contents=[*parts, prompt], config=cfg)
                data = json.loads(resp.text)
                # Some models (e.g. flash-lite) wrap the object in a list: [{...}].
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    data = data[0]
                if not isinstance(data, dict):
                    log.warning("Gemini returned unexpected JSON: %.200s", resp.text)
                    return None
                return data
            except Exception as e:  # noqa: BLE001
                # Only a 400 means the config was rejected; anything else goes to the caller.
                if i == len(configs) - 1 or getattr(e, "code", None) != 400:
                    raise
                log.info("Gemini low-thinking config rejected, retrying with defaults: %s", e)
        return None

    models = [config.GEMINI_MODEL]
    if config.GEMINI_FALLBACK_MODEL and config.GEMINI_FALLBACK_MODEL != config.GEMINI_MODEL:
        models.append(config.GEMINI_FALLBACK_MODEL)
    for j, model in enumerate(models):
        try:
            return ask(model)
        except Exception as e:  # noqa: BLE001
            # Busy (503) or rate-limited (429): try the backup model once. Anything else, or the
            # backup failing too, gives up; the verdict isn't cached, so the next view rechecks.
            if j < len(models) - 1 and _is_busy(e):
                log.info("Gemini model %s is busy, trying backup model %s", model, models[j + 1])
                continue
            log.warning("Gemini call failed: %s", e)
            return None
    return None


def _is_busy(e: Exception) -> bool:
    # A model that is too slow to answer within GEMINI_TIMEOUT_S counts as busy too: either our
    # connection times out, or Google stops at that deadline and returns 504 DEADLINE_EXCEEDED.
    # 500/502 are temporary server errors that the backup model may not have.
    if getattr(e, "code", None) in (429, 500, 502, 503, 504):
        return True
    text = f"{type(e).__name__} {getattr(e, 'status', '')} {e}"
    return any(m in text for m in ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED", "INTERNAL",
                                   "Timeout", "timed out"))
