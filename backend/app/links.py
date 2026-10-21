"""Check a pasted TikTok / Instagram link: find the video ID and fetch what we can without logging in."""
import asyncio
import html
import io
import json
import logging
import os
import re
import tempfile

import httpx
from PIL import Image

from .imaging import frames_from_video_file, resize

log = logging.getLogger("scrollsafe.links")

# A normal desktop browser; some sites return an empty page to unknown clients.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

TIKTOK_ID = re.compile(r"tiktok\.com/(?:@[^/?#]+/(?:video|photo)|v|embed(?:/v2)?)/(\d{8,})", re.I)
TIKTOK_SHORT = re.compile(r"^https?://(?:(?:vm|vt)\.tiktok\.com/|(?:www\.|m\.)?tiktok\.com/t/)", re.I)
INSTAGRAM_ID = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/([\w-]{5,})", re.I)
# Same hashtags the extension treats as a creator's AI label.
AI_HASHTAG = re.compile(r"#(ai|aiart|aigenerated|aivideo|madewithai)\b", re.I)


class LinkError(Exception):
    """A problem we can explain to the user, in both languages."""

    def __init__(self, en: str, ms: str):
        super().__init__(en)
        self.i18n = {"en": en, "ms": ms}


NOT_SUPPORTED = LinkError(
    "Please paste a TikTok or Instagram Reels link, for example https://www.tiktok.com/@name/video/123…",
    "Sila tampal pautan TikTok atau Instagram Reels, contohnya https://www.tiktok.com/@nama/video/123…",
)


def parse(url: str) -> tuple[str, str] | None:
    """(platform, video_id) for a full link, or None. Uses the same IDs as the extension."""
    if m := TIKTOK_ID.search(url):
        return "tiktok", m.group(1)
    if m := INSTAGRAM_ID.search(url):
        return "instagram", m.group(1)
    return None


async def resolve(url: str, client: httpx.AsyncClient) -> tuple[str, str, str]:
    """(platform, video_id, full_url). Short TikTok links (vm.tiktok.com/…) are followed to the real one."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if found := parse(url):
        return (*found, url)
    if TIKTOK_SHORT.match(url):
        try:
            r = await client.get(url, follow_redirects=True)
            full = str(r.url)
        except httpx.HTTPError as e:
            log.warning("Short link failed: %s", e)
            raise LinkError("We couldn't open this TikTok link. Please check it and try again.",
                            "Kami tidak dapat membuka pautan TikTok ini. Sila semak dan cuba lagi.") from e
        if found := parse(full):
            return (*found, full.split("?")[0])
    raise NOT_SUPPORTED


async def tiktok_meta(url: str, client: httpx.AsyncClient) -> dict:
    """Caption, author, cover image and video address from the public video page; oEmbed as a fallback."""
    try:
        r = await client.get(url, follow_redirects=True, headers={"Accept-Language": "en"})
        m = re.search(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', r.text, re.S)
        detail = json.loads(m.group(1))["__DEFAULT_SCOPE__"]["webapp.video-detail"] if m else {}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("TikTok page failed: %s", e)
        detail = {}
    item = (detail.get("itemInfo") or {}).get("itemStruct") or {}
    if item:
        video = item.get("video") or {}
        return {"caption": item.get("desc") or "", "author": (item.get("author") or {}).get("uniqueId") or "",
                "thumbnail_url": video.get("cover") or video.get("originCover") or "", "video_url": video.get("playAddr") or ""}
    return await tiktok_oembed(url, client)


async def tiktok_oembed(url: str, client: httpx.AsyncClient) -> dict:
    """TikTok's public oEmbed API. It sometimes leaves out the caption and cover."""
    unreadable = LinkError("TikTok didn't let us read this video. It may be private or deleted. You can upload a screen recording instead.",
                           "TikTok tidak membenarkan kami membaca video ini. Ia mungkin peribadi atau telah dipadam. Anda boleh muat naik rakaman skrin.")
    try:
        r = await client.get("https://www.tiktok.com/oembed", params={"url": url})
        r.raise_for_status()
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("TikTok oEmbed failed: %s", e)
        raise unreadable from e
    if not d.get("title") and not d.get("thumbnail_url"):
        raise unreadable  # deleted or private videos still answer, but without any content
    return {"caption": d.get("title") or "", "author": d.get("author_name") or "", "thumbnail_url": d.get("thumbnail_url") or ""}


def _meta_tag(page: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']*)', page, re.I) or \
        re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{prop}["\']', page, re.I)
    return html.unescape(m.group(1)) if m else ""


async def instagram_meta(url: str, client: httpx.AsyncClient) -> dict:
    """Instagram has no open API; read the page's preview tags. Often blocked by the login wall."""
    try:
        r = await client.get(url, follow_redirects=True)
        page = r.text if r.status_code == 200 else ""
    except httpx.HTTPError as e:
        log.warning("Instagram page failed: %s", e)
        page = ""
    image = _meta_tag(page, "og:image")
    if not image:
        raise LinkError("Instagram didn't let us read this link. Please upload a screenshot or screen recording instead.",
                        "Instagram tidak membenarkan kami membaca pautan ini. Sila muat naik tangkapan skrin atau rakaman skrin.")
    desc = _meta_tag(page, "og:description") or _meta_tag(page, "og:title")
    author = _meta_tag(page, "og:title").split(" on Instagram")[0]
    return {"caption": desc, "author": author, "thumbnail_url": image}


async def fetch_meta(platform: str, url: str, client: httpx.AsyncClient) -> dict:
    return await (tiktok_meta if platform == "tiktok" else instagram_meta)(url, client)


async def download_image(url: str, client: httpx.AsyncClient) -> Image.Image | None:
    if not url:
        return None
    try:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        return resize(Image.open(io.BytesIO(r.content)).convert("RGB"))
    except Exception as e:  # noqa: BLE001
        log.warning("Cover image download failed: %s", e)
        return None


MAX_VIDEO_BYTES = 80 * 1024 * 1024


async def video_frames(meta: dict, url: str, client: httpx.AsyncClient) -> list[Image.Image]:
    """Sample frames from the full video: TikTok's own video address, else yt-dlp if installed. [] if neither works."""
    if meta.get("video_url"):
        path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                path = tmp.name
                # Same client as the page request, so its cookies come along; TikTok needs them and the Referer.
                async with client.stream("GET", meta["video_url"], headers={"Referer": "https://www.tiktok.com/"},
                                         follow_redirects=True, timeout=httpx.Timeout(30.0, connect=5.0)) as r:
                    r.raise_for_status()
                    size = 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_VIDEO_BYTES:
                            raise ValueError("video too large")
                        tmp.write(chunk)
            frames = await asyncio.to_thread(frames_from_video_file, path)
            if frames:
                return frames
        except Exception as e:  # noqa: BLE001
            log.warning("Video download failed: %s", e)
        finally:
            if path and os.path.exists(path):
                os.unlink(path)
    return await asyncio.to_thread(ytdlp_frames, url)


def ytdlp_frames(url: str) -> list[Image.Image]:
    """Sample frames with yt-dlp, only if it happens to be installed. Otherwise []."""
    try:
        import yt_dlp
    except ImportError:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        opts = {"outtmpl": os.path.join(tmp, "v.%(ext)s"), "format": "mp4/best", "quiet": True, "no_warnings": True,
                "socket_timeout": 15, "max_filesize": 80 * 1024 * 1024}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:  # noqa: BLE001
            log.warning("Video download failed: %s", e)
            return []
        files = [os.path.join(tmp, f) for f in os.listdir(tmp)]
        return frames_from_video_file(files[0]) if files else []


def labels_from_caption(caption: str) -> list[str]:
    return ["creator tagged #ai"] if AI_HASHTAG.search(caption or "") else []
