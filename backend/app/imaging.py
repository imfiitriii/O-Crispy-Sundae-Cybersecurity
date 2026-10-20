"""Frame decoding, resizing and perceptual hashing."""
import base64
import io

import cv2
import imagehash
import numpy as np
from PIL import Image

from . import config


def decode_frame(data_url: str) -> Image.Image | None:
    """Decode a base64 image (with or without a data: URL prefix)."""
    try:
        if "," in data_url and data_url.startswith("data:"):
            data_url = data_url.split(",", 1)[1]
        raw = base64.b64decode(data_url)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        return resize(img)
    except Exception:
        return None


def resize(img: Image.Image, max_side: int = config.FRAME_MAX_SIDE) -> Image.Image:
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1:
        img = img.resize((int(w * scale), int(h * scale)))
    return img


def to_jpeg_bytes(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def phashes(frames: list[Image.Image]) -> list[str]:
    return [str(imagehash.phash(f)) for f in frames]


def phash_distance(a: str, b: str) -> int:
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


try:
    _face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
except AttributeError:  # OpenCV 5 dropped Haar cascades; see requirements.txt
    _face_cascade = None


def largest_face(img: Image.Image, margin: float = 0.25, min_size: int = 48) -> Image.Image | None:
    """Return a crop of the largest face in the image, or None if there is no face."""
    if _face_cascade is None:
        return None
    gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    faces = _face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_size, min_size))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    mx, my = int(w * margin), int(h * margin)
    W, H = img.size
    box = (max(0, x - mx), max(0, y - my), min(W, x + w + mx), min(H, y + h + my))
    return img.crop(box)


def frames_from_video_file(path: str, count: int = 5) -> list[Image.Image]:
    """Take `count` evenly spaced frames (10%..90%) from a video file."""
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    frames: list[Image.Image] = []
    if total <= 0:
        cap.release()
        return frames
    for i in range(count):
        pos = int(total * (0.1 + 0.8 * i / max(1, count - 1)))
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ok, frame = cap.read()
        if ok:
            frames.append(resize(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))))
    cap.release()
    return frames
