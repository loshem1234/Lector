import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DOCS_DIR = DATA_DIR / "docs"
DOCS_DIR.mkdir(parents=True, exist_ok=True)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
# Secret used to sign the login cookie. Falls back to a value derived from the password.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip() or ("lector:" + ADMIN_PASSWORD)

TTS_MODEL = os.environ.get("TTS_MODEL", "gemini-3.8-flash-tts")
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gemini-3.5-transcribe")
API_BASE = "https://generativelanguage.googleapis.com"

# Mock mode lets the whole app run without spending API credits (used for testing).
MOCK = os.environ.get("MOCK_TTS", "").lower() in ("1", "true", "yes") or not GEMINI_API_KEY

# Target size of one generated audio chunk, in characters (~60-90 s of speech).
CHUNK_CHARS = int(os.environ.get("CHUNK_CHARS", "1100"))
# How many chunks are generated in parallel.
CONCURRENCY = int(os.environ.get("CONCURRENCY", "2"))
# Tesseract languages for scanned pages.
OCR_LANGS = os.environ.get("OCR_LANGS", "eng+grc+ell+lat")
MP3_BITRATE = os.environ.get("MP3_BITRATE", "96k")
