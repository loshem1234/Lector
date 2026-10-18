"""Gemini API calls: list voices, synthesize speech, transcribe with word timestamps.

Plain REST over httpx, so nothing depends on a particular SDK version.
In mock mode (no API key, or MOCK_TTS=1) every call is simulated locally.
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import random
import re
import struct
import wave

import httpx

from .config import API_BASE, GEMINI_API_KEY, MOCK, TRANSCRIBE_MODEL, TTS_MODEL


class GeminiError(Exception):
    pass


def _headers(extra=None):
    h = {"x-goog-api-key": GEMINI_API_KEY}
    if extra:
        h.update(extra)
    return h


async def _post_json(client: httpx.AsyncClient, url: str, body: dict, tries: int = 5) -> dict:
    delay = 2.0
    for attempt in range(tries):
        try:
            r = await client.post(url, json=body, headers=_headers(), timeout=240)
        except httpx.HTTPError as e:
            if attempt == tries - 1:
                raise GeminiError(f"Network error: {e}") from e
            await asyncio.sleep(delay)
            delay *= 2
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < tries - 1:
            ra = r.headers.get("retry-after")
            await asyncio.sleep(float(ra) if ra and ra.isdigit() else delay + random.random())
            delay *= 2
            continue
        raise GeminiError(f"{r.status_code}: {r.text[:500]}")
    raise GeminiError("Gave up after retries")


# ----------------------------------------------------------------------------- voices

async def list_voices() -> list[dict]:
    """Custom voices (designed + replicated) stored in the project."""
    if MOCK:
        return [{"id": "voice_mock_narrator", "name": "Mock narrator (no API key set)", "type": "prompted"}]
    out, token = [], None
    async with httpx.AsyncClient() as c:
        for _ in range(20):
            params = [("type", "prompted"), ("type", "replicated"), ("pageSize", "100")]
            if token:
                params.append(("pageToken", token))
            r = await c.get(f"{API_BASE}/v1beta/voices", params=params, headers=_headers(), timeout=30)
            if r.status_code == 400 and not token:
                # filters not accepted: list everything and keep the custom voices
                r = await c.get(f"{API_BASE}/v1beta/voices", headers=_headers(), timeout=30)
            if r.status_code != 200:
                raise GeminiError(f"{r.status_code}: {r.text[:300]}")
            j = r.json()
            for v in j.get("voices", []):
                vtype = str(v.get("type", "")).lower()
                if vtype and not any(k in vtype for k in ("prompt", "replic", "custom")):
                    continue  # prebuilt library voices
                out.append({
                    "id": v.get("id") or v.get("name", "").split("/")[-1],
                    "name": v.get("displayName") or v.get("display_name") or v.get("id"),
                    "type": v.get("type", ""),
                    "language": v.get("languageCode") or v.get("language_code") or "",
                })
            token = j.get("nextPageToken") or j.get("next_page_token")
            if not token:
                break
    return out


# ----------------------------------------------------------------------------- TTS

def _wav_from_pcm(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def clean_for_tts(text: str) -> str:
    # 3.8 TTS treats angle brackets as vocal-event tags; keep source brackets from being acted out.
    return text.replace("<", "(").replace(">", ")")


async def synthesize(client: httpx.AsyncClient, text: str, voice: str, style: str = "") -> bytes:
    """Returns WAV bytes."""
    if MOCK:
        return _mock_tts(text)
    part = {"text": clean_for_tts(text)}
    if style.strip():
        part["speech_metadata"] = {"style": style.strip()}
    body = {
        "contents": [{"role": "user", "parts": [part]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"voice": voice}},
        },
    }
    j = await _post_json(client, f"{API_BASE}/v1beta/models/{TTS_MODEL}:generateContent", body)
    try:
        parts = j["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        raise GeminiError(f"No audio returned: {str(j)[:300]}")
    chunks, mime = [], ""
    for p in parts:
        inl = p.get("inlineData") or p.get("inline_data")
        if inl and inl.get("data"):
            chunks.append(base64.b64decode(inl["data"]))
            mime = inl.get("mimeType") or inl.get("mime_type") or mime
    if not chunks:
        raise GeminiError(f"No audio in response: {str(j)[:300]}")
    if "wav" in mime or chunks[0][:4] == b"RIFF":
        if len(chunks) == 1:
            return chunks[0]
        # several WAV parts: merge their PCM
        pcm, rate = b"", 24000
        for ch in chunks:
            with wave.open(io.BytesIO(ch)) as w:
                rate = w.getframerate()
                pcm += w.readframes(w.getnframes())
        return _wav_from_pcm(pcm, rate)
    m = re.search(r"rate=(\d+)", mime)
    return _wav_from_pcm(b"".join(chunks), int(m.group(1)) if m else 24000)


# ----------------------------------------------------------------------------- transcription

def _secs(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):  # {"seconds": "1", "nanos": 5e8}
        return float(v.get("seconds", 0)) + float(v.get("nanos", 0)) / 1e9
    return float(str(v).rstrip("s") or 0)


async def _upload_file(client: httpx.AsyncClient, data: bytes, mime: str) -> tuple[str, str]:
    start = await client.post(
        f"{API_BASE}/upload/v1beta/files",
        headers=_headers({
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": mime,
        }),
        json={"file": {"display_name": "lector-chunk"}}, timeout=60)
    url = start.headers.get("x-goog-upload-url")
    if not url:
        raise GeminiError(f"Upload start failed: {start.status_code} {start.text[:200]}")
    up = await client.post(url, content=data, headers={
        "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
        "Content-Length": str(len(data))}, timeout=120)
    f = up.json().get("file", {})
    if not f.get("uri"):
        raise GeminiError(f"Upload failed: {up.text[:200]}")
    for _ in range(30):
        if f.get("state", "ACTIVE") == "ACTIVE":
            break
        await asyncio.sleep(1)
        f = (await client.get(f"{API_BASE}/v1beta/{f['name']}", headers=_headers())).json()
    return f["uri"], f.get("name", "")


async def transcribe_words(client: httpx.AsyncClient, audio: bytes, mime: str = "audio/mp3",
                           mock_text: str = "") -> list[tuple[str, float, float]]:
    """Word-level timestamps for an audio clip: [(word, start_s, end_s), ...]."""
    if MOCK:
        return _mock_transcribe(mock_text, audio)
    cfg = {"audioTranscriptionConfig": {"wordTimestamp": True}}
    url = f"{API_BASE}/v1beta/models/{TRANSCRIBE_MODEL}:generateContent"
    file_name = None
    try:
        body = {"contents": [{"parts": [{"inlineData": {"mimeType": mime,
                                                         "data": base64.b64encode(audio).decode()}}]}],
                "generationConfig": cfg}
        try:
            j = await _post_json(client, url, body, tries=3)
        except GeminiError:
            uri, file_name = await _upload_file(client, audio, mime)
            body = {"contents": [{"parts": [{"fileData": {"fileUri": uri, "mimeType": mime}}]}],
                    "generationConfig": cfg}
            j = await _post_json(client, url, body)
    finally:
        if file_name:
            try:
                await client.delete(f"{API_BASE}/v1beta/{file_name}", headers=_headers())
            except httpx.HTTPError:
                pass
    words = []
    for cand in j.get("candidates", []):
        for p in cand.get("content", {}).get("parts", []):
            tr = p.get("audioTranscription") or p.get("audio_transcription")
            if not tr:
                continue
            for w in tr.get("words", []):
                s = w.get("startOffset", w.get("start_offset"))
                e = w.get("endOffset", w.get("end_offset"))
                if s is None or e is None:
                    continue
                words.append((w.get("word", ""), _secs(s), _secs(e)))
    return words


# ----------------------------------------------------------------------------- mock

_MOCK_RATE = 16000


def _mock_durations(text: str):
    t, out = 0.3, []
    for w in text.split():
        d = 0.12 + 0.055 * len(w)
        out.append((w, t, t + d))
        t += d + (0.35 if w[-1:] in ".;:!?" else 0.08)
    return out, t + 0.3


def _mock_tts(text: str) -> bytes:
    """A soft tone per word so you can hear the rhythm while testing."""
    words, total = _mock_durations(text)
    n = int(total * _MOCK_RATE)
    samples = [0] * n
    for i, (w, s, e) in enumerate(words):
        f = 180 + (i % 7) * 25
        a, b = int(s * _MOCK_RATE), int(e * _MOCK_RATE)
        for k in range(a, min(b, n)):
            env = min(1.0, (k - a) / 400, (b - k) / 400)
            samples[k] = int(5000 * env * math.sin(2 * math.pi * f * k / _MOCK_RATE))
    return _wav_from_pcm(struct.pack(f"<{n}h", *samples), _MOCK_RATE)


def _mock_transcribe(text: str, audio: bytes):
    words, _ = _mock_durations(text)
    # behave like a real transcript: drop punctuation, occasionally miss a word
    rnd = random.Random(len(text))
    return [(re.sub(r"[^\w]", "", w), s, e) for w, s, e in words if rnd.random() > 0.05]
