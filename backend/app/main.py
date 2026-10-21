"""ScrollSafe backend: checks short videos for deepfakes, AI generation and false information."""
import asyncio
import base64
import hashlib
import io
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from pydantic import BaseModel, Field

import httpx

from . import config, db, detectors, factcheck, gemini_client, links, scam_rules, scoring
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
    lang: str = "en"  # "en" or "ms"; the verdict text comes back in this language


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


def _respond(platform: str, video_id: str, result: dict, cached: bool, lang: str = "en") -> dict:
    out = scoring.localize(scoring.apply_reports(result, db.report_summary(platform, video_id)), lang)
    out["cached"] = cached
    out["platform"] = platform
    out["video_id"] = video_id
    return out


@app.get("/verdict/{platform}/{video_id}")
def get_verdict(platform: str, video_id: str, lang: str = "en") -> dict:
    result = db.get_verdict(platform, video_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not checked yet")
    return _respond(platform, video_id, result, cached=True, lang=lang)


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
        msg = {"en": "We couldn't check this video right now.", "ms": "Kami tidak dapat menyemak video ini sekarang."}
        i18n = {lang: {"title": scoring.TITLES[lang]["unknown"], "message": msg[lang], "details": []} for lang in scoring.LANGS}
        result.update(status="unknown", i18n=i18n, **i18n["en"])
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
        return _respond(req.platform, req.video_id, cached, cached=True, lang=req.lang)

    frames = [f for f in (decode_frame(x) for x in req.frames[: config.MAX_FRAMES]) if f is not None]
    if not frames and not req.caption:
        raise HTTPException(status_code=400, detail="Send at least one frame or a caption")

    hashes = phashes(frames)
    match = db.find_by_phash(hashes)
    if match is not None:
        # Same video re-uploaded under a new ID: reuse the verdict for this ID too.
        db.save_verdict(req.platform, req.video_id, match, hashes)
        return _respond(req.platform, req.video_id, match, cached=True, lang=req.lang)

    result = await run_checks(frames, req.caption, req.author, req.platform_labels, req.use_gemini)
    _save(req.platform, req.video_id, result, hashes)
    return _respond(req.platform, req.video_id, result, cached=False, lang=req.lang)


@app.post("/report")
def report(req: ReportRequest) -> dict:
    db.add_report(req.platform, req.video_id, req.user_id, req.label)
    summary = db.report_summary(req.platform, req.video_id)
    return {"ok": True, "reports": summary}


@app.post("/check-upload")
async def check_upload(file: UploadFile = File(...), caption: str = "", lang: str = "en") -> dict:
    """Feature 1: check an uploaded image or video with the same pipeline."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    video_id = hashlib.sha256(data).hexdigest()[:32]
    cached = db.get_verdict("upload", video_id)
    if cached is not None:
        return _respond("upload", video_id, cached, cached=True, lang=lang)

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
    return _respond("upload", video_id, result, cached=False, lang=lang)


class LinkRequest(BaseModel):
    url: str
    lang: str = "en"
    use_gemini: bool = True


LINK_ONLY_NOTE = {"en": "Only the cover image and caption were checked, not the whole video.",
                  "ms": "Hanya gambar muka depan dan kapsyen yang disemak, bukan keseluruhan video."}


def _jpeg_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


@app.post("/check-link")
async def check_link(req: LinkRequest):
    """Check a pasted TikTok or Instagram Reels link with the same pipeline as the extension."""
    lang = req.lang if req.lang in scoring.LANGS else "en"
    async with httpx.AsyncClient(timeout=links.TIMEOUT, headers={"User-Agent": links.UA}) as client:
        try:
            platform, video_id, url = await links.resolve(req.url, client)
        except links.LinkError as e:
            return JSONResponse({"detail": e.i18n[lang]}, status_code=400)
        # Already checked by the extension (full video), or by an earlier link check.
        cached = db.get_verdict(platform, video_id) or db.get_verdict("link-" + platform, video_id)
        try:
            meta = await links.fetch_meta(platform, url, client)
        except links.LinkError as e:
            if cached is None:
                return JSONResponse({"detail": e.i18n[lang]}, status_code=400)
            meta = {"caption": "", "author": "", "thumbnail_url": ""}
        cover = await links.download_image(meta["thumbnail_url"], client)
        info = {"platform": platform, "video_id": video_id, "url": url, "author": meta["author"],
                "caption": meta["caption"][:300], "thumbnail": _jpeg_data_url(cover) if cover else None}
        if cached is not None:
            return {**_respond(platform, video_id, cached, cached=True, lang=lang), "link": info}
        frames = await links.video_frames(meta, url, client)

    full_video = bool(frames)
    if not full_video:
        frames = [cover] if cover else []
    if not frames and not meta["caption"]:
        return JSONResponse({"detail": {"en": "We couldn't get the video or its caption from this link.",
                                        "ms": "Kami tidak dapat mendapatkan video atau kapsyennya daripada pautan ini."}[lang]},
                            status_code=400)

    result = await run_checks(frames, meta["caption"], meta["author"], links.labels_from_caption(meta["caption"]), req.use_gemini)
    if full_video:
        _save(platform, video_id, result, phashes(frames))
    else:
        for code, text in result.get("i18n", {}).items():
            text["details"] = list(text.get("details", [])) + [LINK_ONLY_NOTE[code]]
        result["details"] = result["i18n"]["en"]["details"] if "i18n" in result else result.get("details", [])
        # A cover-only check is weaker than the extension's, so keep it apart: the extension still does a full check.
        _save("link-" + platform, video_id, result, [])
    return {**_respond(platform, video_id, result, cached=False, lang=lang), "link": info}


@app.get("/")
def index():
    page = WEB_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return {"ok": True, "see": "/docs"}
