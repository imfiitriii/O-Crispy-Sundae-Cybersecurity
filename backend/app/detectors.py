"""Local Hugging Face classifiers: face deepfake detector and AI-vs-human image detector.

Both are optional. If a model can't load (no internet, not installed), its score is
None and the rest of the pipeline still works.
"""
import logging

from PIL import Image

from . import config
from .imaging import largest_face

log = logging.getLogger("scrollsafe.detectors")

_deepfake = None
_ai_image = None
status = {"deepfake_model": "not loaded", "ai_image_model": "not loaded"}

# Words that mark a label as the "fake / AI" class. Model cards use different
# label names (e.g. "Deepfake" vs "Realism", "ai" vs "hum"), so we match loosely.
FAKE_WORDS = ("fake", "deepfake", "ai", "artificial", "generated", "synthetic", "manipulated", "spoof")
REAL_WORDS = ("real", "realism", "human", "hum", "authentic", "genuine", "original", "natural")


def _load(model_id: str):
    from transformers import pipeline

    return pipeline("image-classification", model=model_id, device=-1)


def load() -> None:
    global _deepfake, _ai_image
    if not config.LOAD_LOCAL_MODELS:
        status.update(deepfake_model="disabled", ai_image_model="disabled")
        return
    for name, model_id in (("deepfake_model", config.DEEPFAKE_MODEL), ("ai_image_model", config.AI_IMAGE_MODEL)):
        try:
            pipe = _load(model_id)
            labels = list(pipe.model.config.id2label.values())
            known = [lb for lb in labels if _is_fake_label(lb) is not None]
            status[name] = f"loaded {model_id} labels={labels}" + (
                "" if len(known) >= 2 else " (labels not recognised as real/fake, so this model is ignored)"
            )
            if name == "deepfake_model":
                _deepfake = pipe
            else:
                _ai_image = pipe
            log.info("Loaded %s (%s)", model_id, labels)
        except Exception as e:  # noqa: BLE001
            status[name] = f"failed: {e.__class__.__name__}: {e}"
            log.warning("Could not load %s: %s", model_id, e)


def _is_fake_label(label: str) -> bool | None:
    tokens = label.lower().replace("-", " ").replace("_", " ").split()
    if any(t in REAL_WORDS for t in tokens):
        return False
    if any(t in FAKE_WORDS for t in tokens):
        return True
    return None


def fake_probability(results: list[dict]) -> float | None:
    """Turn a classifier's [{label, score}, ...] into P(fake)."""
    fake = sum(r["score"] for r in results if _is_fake_label(r["label"]) is True)
    real = sum(r["score"] for r in results if _is_fake_label(r["label"]) is False)
    if fake == 0 and real == 0:
        return None
    return fake / (fake + real)


def _score(pipe, images: list[Image.Image]) -> float | None:
    if pipe is None or not images:
        return None
    probs = []
    for img in images:
        p = fake_probability(pipe(img, top_k=None))
        if p is not None:
            probs.append(p)
    return sum(probs) / len(probs) if probs else None


def deepfake_score(frames: list[Image.Image]) -> dict:
    """Run the deepfake classifier on the largest face in each frame."""
    crops = [c for c in (largest_face(f) for f in frames) if c is not None]
    return {
        "faces_found": len(crops),
        "score": _score(_deepfake, crops),
        "available": _deepfake is not None,
    }


def ai_image_score(frames: list[Image.Image]) -> dict:
    return {"score": _score(_ai_image, frames), "available": _ai_image is not None}
