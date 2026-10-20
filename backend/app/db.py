"""SQLite storage for the shared verdict cache and community reports.

Only the backend writes verdicts. User reports live in their own table and can
only raise a warning level, never lower it (see scoring.apply_reports).
"""
import hashlib
import json
import sqlite3
import threading
import time

from . import config
from .imaging import phash_distance

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init(_conn)
    return _conn


def _init(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS verdicts (
            platform TEXT NOT NULL,
            video_id TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT NOT NULL,
            phashes TEXT NOT NULL DEFAULT '[]',
            model_version TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (platform, video_id)
        );
        CREATE TABLE IF NOT EXISTS reports (
            platform TEXT NOT NULL,
            video_id TEXT NOT NULL,
            user_hash TEXT NOT NULL,
            label TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (platform, video_id, user_hash)
        );
        """
    )
    c.commit()


def get_verdict(platform: str, video_id: str) -> dict | None:
    row = conn().execute(
        "SELECT result_json, model_version FROM verdicts WHERE platform=? AND video_id=?",
        (platform, video_id),
    ).fetchone()
    if not row or row["model_version"] != config.MODEL_VERSION:
        return None
    return json.loads(row["result_json"])


def find_by_phash(hashes: list[str], limit: int = 5000) -> dict | None:
    """Find a stored verdict whose frames look like these (catches re-uploads)."""
    if not hashes:
        return None
    rows = conn().execute(
        "SELECT result_json, phashes FROM verdicts WHERE model_version=? ORDER BY created_at DESC LIMIT ?",
        (config.MODEL_VERSION, limit),
    ).fetchall()
    for row in rows:
        stored = json.loads(row["phashes"])
        matches = sum(
            1 for h in hashes if any(phash_distance(h, s) <= config.PHASH_MAX_DISTANCE for s in stored)
        )
        # Most frames must match, so one similar frame doesn't link unrelated videos.
        if stored and matches >= max(2, (len(hashes) + 1) // 2):
            return json.loads(row["result_json"])
    return None


def save_verdict(platform: str, video_id: str, result: dict, hashes: list[str]) -> None:
    with _lock:
        conn().execute(
            "INSERT OR REPLACE INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                platform,
                video_id,
                result["status"],
                json.dumps(result),
                json.dumps(hashes),
                config.MODEL_VERSION,
                time.time(),
            ),
        )
        conn().commit()


def hash_user(user_id: str) -> str:
    return hashlib.sha256(("scrollsafe:" + user_id).encode()).hexdigest()


def add_report(platform: str, video_id: str, user_id: str, label: str) -> None:
    with _lock:
        conn().execute(
            "INSERT OR REPLACE INTO reports VALUES (?, ?, ?, ?, ?)",
            (platform, video_id, hash_user(user_id), label, time.time()),
        )
        conn().commit()


def report_summary(platform: str, video_id: str) -> dict:
    rows = conn().execute(
        "SELECT label, COUNT(*) AS n FROM reports WHERE platform=? AND video_id=? GROUP BY label",
        (platform, video_id),
    ).fetchall()
    by_label = {r["label"]: r["n"] for r in rows}
    return {"count": sum(by_label.values()), "by_label": by_label}
