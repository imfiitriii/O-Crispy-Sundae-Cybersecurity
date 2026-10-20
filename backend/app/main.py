"""ScrollSafe backend: checks short videos for deepfakes, AI generation and false information."""
import asyncio
import hashlib
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field

from . import config, db, detectors, factcheck, gemini_client, scam_rules, scoring
from .imaging import decode_frame, frames_from_video_file, phashes, resize

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scrollsafe")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.conn()
    detectors.load()
    log.info("Gemini: %s, Fact Check: %s", gemini_client.available(), factcheck.available())
    yield


app = FastAPI(title="ScrollSafe", version=config.MODEL_VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


class AnalyzeRequest(BaseModel):
    platform: str = Field(..., examples=["tiktok"])
    video_id: str
    caption: str = ""
    author: str = ""
    page_url: str = ""
    platform_labels: list[str] = []
    frames: list[str] = Field(default_factory=list, description="Base64 JPEG/PNG frames (data URLs are fine)")
    use_gemini: bool = True  # the user can turn the slower AI helper off in the extension


class ReportRequest(BaseModel):
    platform: str
    video_id: str
    user_id: str
    label: str = "suspicious"  # deepfake | ai | scam | false_info | suspicious


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model_version": config.MODEL_VERSION,
        "models": detectors.status,
        "gemini": gemini_client.available(),
        "fact_check": factcheck.available(),
    }


def _respond(platform: str, video_id: str, result: dict, cached: bool) -> dict:
    out = scoring.apply_reports(result, db.report_summary(platform, video_id))
    out["cached"] = cached
    out["platform"] = platform
    out["video_id"] = video_id
    return out


@app.get("/verdict/{platform}/{video_id}")
def get_verdict(platform: str, video_id: str) -> dict:
    result = db.get_verdict(platform, video_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not checked yet")
    return _respond(platform, video_id, result, cached=True)


async def _skipped() -> None:
    return None


async def run_checks(frames: list[Image.Image], caption: str, author: str, labels: list[str],
                     use_gemini: bool = True) -> dict:
    """Run all checks. The two local models and Gemini run at the same time."""
    gemini_task = asyncio.wait_for(
        asyncio.to_thread(gemini_client.analyze, frames, caption, author, labels), timeout=config.GEMINI_TIMEOUT_S * 2 + 5  # main model + backup
    ) if use_gemini else _skipped()
    deepfake_task = asyncio.to_thread(detectors.deepfake_score, frames)
    ai_task = asyncio.to_thread(detectors.ai_image_score, frames)
    gemini, deepfake, ai_image = await asyncio.gather(gemini_task, deepfake_task, ai_task, return_exceptions=True)

    if isinstance(gemini, BaseException):
        log.warning("Gemini skipped: %r", gemini)
        gemini = None
    if isinstance(deepfake, BaseException):
        log.warning("Deepfake model failed: %r", deepfake)
        deepfake = {"faces_found": 0, "score": None, "available": False}
    if isinstance(ai_image, BaseException):
        log.warning("AI-image model failed: %r", ai_image)
        ai_image = {"score": None, "available": False}

    claims = (gemini or {}).get("claims") or []
    facts = await factcheck.check_claims(claims)
    scam = scam_rules.check(caption, (gemini or {}).get("on_screen_text", ""))

    result = scoring.combine(deepfake, ai_image, gemini, facts, scam, labels)
    result["signals_used"]["fact_check"] = factcheck.available()
    used = result["signals_used"]
    if not (used["gemini"] or used["deepfake_model"] or used["ai_image_model"]) and result["status"] == "green":
        # Nothing actually looked at the video, so don't pretend it's safe.
        result.update(status="unknown", title="Couldn't check", message="We couldn't check this video right now.")
    # Only cache complete results, so a Gemini rate limit doesn't freeze a weak verdict forever.
    result["complete"] = used["gemini"] or not gemini_client.available()
    return result


def _save(platform: str, video_id: str, result: dict, hashes: list[str]) -> None:
    if result.get("complete") and result["status"] != "unknown":
        db.save_verdict(platform, video_id, result, hashes)


@app.post("/analyze")
async def analyze(req: AnalyzeRequest) -> dict:
    cached = db.get_verdict(req.platform, req.video_id)
    if cached is not None:
        return _respond(req.platform, req.video_id, cached, cached=True)

    frames = [f for f in (decode_frame(x) for x in req.frames[: config.MAX_FRAMES]) if f is not None]
    if not frames and not req.caption:
        raise HTTPException(status_code=400, detail="Send at least one frame or a caption")

    hashes = phashes(frames)
    match = db.find_by_phash(hashes)
    if match is not None:
        # Same video re-uploaded under a new ID: reuse the verdict for this ID too.
        db.save_verdict(req.platform, req.video_id, match, hashes)
        return _respond(req.platform, req.video_id, match, cached=True)

    result = await run_checks(frames, req.caption, req.author, req.platform_labels, req.use_gemini)
    _save(req.platform, req.video_id, result, hashes)
    return _respond(req.platform, req.video_id, result, cached=False)


@app.post("/report")
def report(req: ReportRequest) -> dict:
    db.add_report(req.platform, req.video_id, req.user_id, req.label)
    summary = db.report_summary(req.platform, req.video_id)
    return {"ok": True, "reports": summary}


@app.post("/check-upload")
async def check_upload(file: UploadFile = File(...), caption: str = "") -> dict:
    """Feature 1: check an uploaded image or video with the same pipeline."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    video_id = hashlib.sha256(data).hexdigest()[:32]
    cached = db.get_verdict("upload", video_id)
    if cached is not None:
        return _respond("upload", video_id, cached, cached=True)

    content_type = file.content_type or ""
    frames: list[Image.Image] = []
    if content_type.startswith("image/"):
        import io

        frames = [resize(Image.open(io.BytesIO(data)).convert("RGB"))]
    else:
        suffix = Path(file.filename or "upload.mp4").suffix or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            path = tmp.name
        try:
            frames = frames_from_video_file(path)
        finally:
            os.unlink(path)
    if not frames:
        raise HTTPException(status_code=400, detail="Could not read any frames from this file")

    result = await run_checks(frames, caption, "", [])
    _save("upload", video_id, result, phashes(frames))
    return _respond("upload", video_id, result, cached=False)


@app.get("/")
def index():
    page = WEB_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return {"ok": True, "see": "/docs"}
