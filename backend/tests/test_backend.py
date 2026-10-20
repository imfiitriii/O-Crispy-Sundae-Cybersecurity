"""Smoke tests that run without API keys or downloaded models.

Run from the backend folder:  python -m pytest -q
"""
import base64
import io
import os

os.environ["LOAD_LOCAL_MODELS"] = "0"
os.environ["GEMINI_API_KEY"] = ""
os.environ["FACTCHECK_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app import config, db, detectors, factcheck, gemini_client, scam_rules, scoring


def frame(seed: int) -> str:
    img = Image.new("RGB", (360, 640), (20 * seed % 255, 80, 140))
    d = ImageDraw.Draw(img)
    for i in range(12):
        d.rectangle([i * 30, (i * 53 * seed) % 600, i * 30 + 25, (i * 53 * seed) % 600 + 40], fill=(255, 255 - i * 20, i * 20))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(db, "_conn", None)
    from app.main import app

    with TestClient(app) as c:
        yield c


GEMINI_SAFE = {
    "face_visible": False, "face_consistent_across_frames": None, "deepfake_signs": [], "deepfake_likelihood": 0.05,
    "ai_visual_signs": [], "ai_label_visible": False, "ai_generated_likelihood": 0.1, "on_screen_text": "my cat",
    "claims": [], "scam_signs": [], "misinformation_risk": "none", "reason_simple": "A normal pet video.",
}


def test_health(client):
    r = client.get("/health").json()
    assert r["ok"] and r["gemini"] is False and r["models"]["deepfake_model"] == "disabled"


def test_no_signals_is_unknown_not_safe(client):
    r = client.post("/analyze", json={"platform": "tiktok", "video_id": "1", "frames": [frame(1)]}).json()
    assert r["status"] == "unknown"
    assert client.get("/verdict/tiktok/1").status_code == 404  # not cached


def test_scam_caption_is_red_even_without_models(client):
    r = client.post("/analyze", json={
        "platform": "tiktok", "video_id": "2", "caption": "Pulangan dijamin 30% sebulan! PM wa.me/60123", "frames": [frame(2)],
    }).json()
    assert r["status"] == "red"


def test_cache_reports_and_reupload(client, monkeypatch):
    monkeypatch.setattr(gemini_client, "available", lambda: True)
    monkeypatch.setattr(gemini_client, "analyze", lambda *a, **k: dict(GEMINI_SAFE))
    frames = [frame(3), frame(4), frame(5)]
    r = client.post("/analyze", json={"platform": "tiktok", "video_id": "100", "frames": frames}).json()
    assert r["status"] == "green" and r["cached"] is False

    assert client.get("/verdict/tiktok/100").json()["cached"] is True

    # Re-upload of the same frames under a new ID reuses the verdict.
    r2 = client.post("/analyze", json={"platform": "tiktok", "video_id": "200", "frames": frames}).json()
    assert r2["cached"] is True and r2["status"] == "green"

    # Three reports raise it to yellow; reports never make it greener.
    for u in ("a", "b", "c"):
        client.post("/report", json={"platform": "tiktok", "video_id": "100", "user_id": u, "label": "scam"})
    assert client.get("/verdict/tiktok/100").json()["status"] == "yellow"
    # Same user reporting twice still counts once.
    client.post("/report", json={"platform": "tiktok", "video_id": "200", "user_id": "a"})
    client.post("/report", json={"platform": "tiktok", "video_id": "200", "user_id": "a"})
    assert client.get("/verdict/tiktok/200").json()["reports"]["count"] == 1


def test_upload_image(client):
    raw = base64.b64decode(frame(7).split(",", 1)[1])
    r = client.post("/check-upload", files={"file": ("x.jpg", raw, "image/jpeg")})
    assert r.status_code == 200 and r.json()["status"] in ("unknown", "green", "yellow", "red")


def _combine(df=None, ai=None, g=None, facts=(), caption="", labels=()):
    return scoring.combine(
        {"faces_found": 1 if df is not None else 0, "score": df, "available": df is not None},
        {"score": ai, "available": ai is not None}, g, list(facts), scam_rules.check(caption), list(labels),
    )


def test_scoring_levels():
    assert _combine(df=0.1, ai=0.1, g=GEMINI_SAFE)["status"] == "green"
    # A confident deepfake model alone is not enough for red: Gemini must see it too.
    assert _combine(df=0.95, ai=0.1, g=GEMINI_SAFE)["status"] == "green"
    g_face = dict(GEMINI_SAFE, face_visible=True, deepfake_signs=["blurry jawline"], deepfake_likelihood=0.7)
    assert _combine(df=0.95, ai=0.1, g=g_face)["status"] == "red"
    # Local models alone (Gemini down) can only give yellow, and only when very sure.
    assert _combine(df=0.95, ai=0.1, g=None)["status"] == "yellow"
    assert _combine(df=0.8, ai=0.8, g=None)["status"] == "green"
    # AI-made alone is yellow at most; with a money claim and risk it's red.
    g_ai = dict(GEMINI_SAFE, ai_generated_likelihood=0.8)
    assert _combine(df=0.1, ai=0.9, g=g_ai)["status"] == "yellow"
    g_money = dict(g_ai, claims=[{"text": "earn 30% monthly", "topic": "money"}], misinformation_risk="medium")
    assert _combine(df=0.1, ai=0.9, g=g_money)["status"] == "red"
    # A news claim with low risk stays green.
    g_news = dict(GEMINI_SAFE, claims=[{"text": "Ship passengers stranded", "topic": "news"}], misinformation_risk="low")
    assert _combine(df=0.1, ai=0.1, g=g_news)["status"] == "green"
    assert _combine(df=0.1, ai=0.1, g=GEMINI_SAFE, labels=["AI-generated label"])["status"] == "yellow"
    fact = {"match": "match", "verdict": "false", "publisher": "AFP", "rating": "False"}
    assert _combine(df=0.1, ai=0.1, g=GEMINI_SAFE, facts=[fact])["status"] == "red"


def test_label_mapping():
    assert detectors.fake_probability([{"label": "Deepfake", "score": 0.8}, {"label": "Realism", "score": 0.2}]) == pytest.approx(0.8)
    assert detectors.fake_probability([{"label": "ai", "score": 0.3}, {"label": "hum", "score": 0.7}]) == pytest.approx(0.3)
    assert detectors.fake_probability([{"label": "LABEL_0", "score": 1.0}]) is None


def test_ratings_and_keywords():
    assert factcheck.classify_rating("Partly false") == "misleading"
    assert factcheck.classify_rating("PALSU") == "false"
    assert factcheck.classify_rating("True") == "other"
    assert scam_rules.check("This secure app")["level"] == "none"
    assert scam_rules.check("Guaranteed return every week")["level"] == "high"


def test_gemini_client_is_reused(monkeypatch):
    # Regression: a fresh client per call was garbage-collected mid-request ("client has been closed").
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini_client, "_client_instance", None)
    assert gemini_client._client() is gemini_client._client()


def test_gemini_can_be_turned_off(client, monkeypatch):
    monkeypatch.setattr(gemini_client, "available", lambda: True)

    def fail(*a, **k):
        raise AssertionError("Gemini should not be called")

    monkeypatch.setattr(gemini_client, "analyze", fail)
    r = client.post("/analyze", json={"platform": "tiktok", "video_id": "300", "caption": "Guaranteed return every week",
                                      "frames": [frame(6)], "use_gemini": False}).json()
    assert r["signals_used"]["gemini"] is False and r["status"] == "red"
    # A local-only verdict isn't cached, so turning the helper back on gets a full check.
    assert client.get("/verdict/tiktok/300").status_code == 404
