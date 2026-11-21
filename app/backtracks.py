"""Backing-track library: music or ambience that plays under the reading.

Tracks live in DATA_DIR/backtracks/<id>.mp3 with <id>.json beside them.
Every upload is converted to a 44.1 kHz stereo MP3 and loudness-normalised, so the
same volume setting sounds alike from one track to the next.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from .config import DATA_DIR

TRACKS_DIR = DATA_DIR / "backtracks"
TRACKS_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS = DATA_DIR / "settings.json"
DEFAULT_VOLUME = 0.3        # music
DEFAULT_VOICE_VOLUME = 1.0  # reading
TARGET_LUFS = -20


def _valid_id(tid: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{12}", tid or ""))


def path(tid: str) -> Path | None:
    if not _valid_id(tid):
        return None
    p = TRACKS_DIR / f"{tid}.mp3"
    return p if p.exists() else None


def get(tid: str) -> dict | None:
    if not _valid_id(tid):
        return None
    try:
        return json.loads((TRACKS_DIR / f"{tid}.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _save(meta: dict):
    p = TRACKS_DIR / f"{meta['id']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def list_all() -> list[dict]:
    out = []
    for f in TRACKS_DIR.glob("*.json"):
        m = get(f.stem)
        if m and path(m["id"]):
            m["size"] = path(m["id"]).stat().st_size
            out.append(m)
    out.sort(key=lambda m: m.get("name", "").lower())
    return out


def probe_duration(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def add(src: Path, name: str) -> dict:
    """Convert an uploaded file into the library. Raises ValueError if it isn't audio."""
    tid = uuid.uuid4().hex[:12]
    out = TRACKS_DIR / f"{tid}.mp3"
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:a:0", "-vn",
         "-af", f"loudnorm=I={TARGET_LUFS}:TP=-2:LRA=11", "-ar", "44100", "-ac", "2",
         "-c:a", "libmp3lame", "-b:a", "192k", str(out)],
        capture_output=True, text=True)
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        out.unlink(missing_ok=True)
        raise ValueError("That file couldn't be read as audio.")
    dur = probe_duration(out)
    if dur < 1:
        out.unlink(missing_ok=True)
        raise ValueError("That track is too short to loop.")
    meta = {"id": tid, "name": (name.strip() or "Untitled track")[:120], "duration": round(dur, 2),
            "created": time.time()}
    _save(meta)
    return meta


def rename(tid: str, name: str) -> dict | None:
    m = get(tid)
    if not m:
        return None
    m["name"] = name.strip()[:120] or m["name"]
    _save(m)
    return m


def delete(tid: str) -> bool:
    if not _valid_id(tid):
        return False
    found = (TRACKS_DIR / f"{tid}.mp3").exists()
    (TRACKS_DIR / f"{tid}.mp3").unlink(missing_ok=True)
    (TRACKS_DIR / f"{tid}.json").unlink(missing_ok=True)
    s = load_settings()
    if s.get("backtrack", {}).get("id") == tid:
        s["backtrack"] = {"id": None, "volume": s["backtrack"].get("volume", DEFAULT_VOLUME)}
        save_settings(s)
    return found


# ----------------------------------------------------------------------------- settings

def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(s: dict):
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s), encoding="utf-8")
    os.replace(tmp, SETTINGS)


def clamp_volume(v, default: float = DEFAULT_VOLUME) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return round(min(1.0, max(0.0, v)), 2)


def effective(meta: dict | None) -> dict:
    """The track and volume a document uses: its own choice, else the last one chosen anywhere."""
    choice = (meta or {}).get("backtrack")
    if choice is None:
        choice = load_settings().get("backtrack") or {}
    tid = choice.get("id")
    if tid and not path(tid):
        tid = None
    return {"id": tid, "volume": clamp_volume(choice.get("volume", DEFAULT_VOLUME)),
            "voice_volume": clamp_volume(choice.get("voice_volume", DEFAULT_VOICE_VOLUME), DEFAULT_VOICE_VOLUME)}


def mix_gains(choice: dict) -> tuple[float, float]:
    """Reading and music gains for a download: the same balance as the sliders,
    scaled so the louder of the two plays at full level."""
    v, m = choice["voice_volume"], choice["volume"]
    top = max(v, m)
    if top <= 0:
        return 1.0, 0.0
    return v / top, m / top
