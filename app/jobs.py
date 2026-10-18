"""Chunk planning and the background generation worker."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import wave

import httpx

from . import store
from .align import align
from .config import CHUNK_CHARS, CONCURRENCY, MP3_BITRATE
from .gemini import GeminiError, synthesize, transcribe_words
from .ingest import SKIP_KINDS

log = logging.getLogger("lector.jobs")
SENT_END = re.compile(r"[.!?;:·»”\"')\]]$")


# ----------------------------------------------------------------------------- planning

def reading_tokens(doc: dict, sec_index: int):
    """[(word_index, spoken_text, merged_word_indices, para_break_after)] for one section."""
    words, skip, join = doc["words"], set(doc["skipw"]), set(doc["join"])
    sec = doc["sections"][sec_index]
    toks = []
    for p in doc["paras"][sec["p0"]:sec["p1"]]:
        if p["k"] in SKIP_KINDS:
            continue
        start_len = len(toks)
        for wi in range(p["s"], p["e"]):
            if wi in skip:
                continue
            w = words[wi][0]
            if wi in join and toks:
                prev = toks[-1]
                prev[1] = prev[1][:-1] + w if prev[1].endswith("-") else prev[1] + w
                prev[2].append(wi)
                continue
            toks.append([wi, w, [], False])
        if len(toks) > start_len:
            toks[-1][3] = True
            if p["k"] == "heading":
                toks[-1][1] = toks[-1][1] if SENT_END.search(toks[-1][1]) else toks[-1][1] + "."
    return toks


def plan_chunks(doc: dict) -> list[dict]:
    chunks = []
    for si in range(len(doc["sections"])):
        toks = reading_tokens(doc, si)
        cur, n = [], 0
        for k, t in enumerate(toks):
            cur.append(t)
            n += len(t[1]) + 1
            ends_sentence = SENT_END.search(t[1]) or t[3]
            if (n >= CHUNK_CHARS and ends_sentence) or n >= CHUNK_CHARS * 1.7:
                chunks.append(_mk_chunk(len(chunks), si, cur))
                cur, n = [], 0
        if cur:
            # fold a tiny tail into the previous chunk of the same section
            if chunks and chunks[-1]["sec"] == si and n < CHUNK_CHARS * 0.25 and \
                    len(chunks[-1]["text"]) + n < CHUNK_CHARS * 1.9:
                prev = chunks.pop()
                cur = prev["_toks"] + cur
            chunks.append(_mk_chunk(len(chunks), si, cur))
    for c in chunks:
        c.pop("_toks", None)
    return chunks


def _mk_chunk(i, si, toks):
    parts = []
    for t in toks:
        parts.append(t[1])
        parts.append("\n\n" if t[3] else " ")
    return {"i": i, "sec": si, "text": "".join(parts).strip(),
            "tok": [[t[0], t[1], t[2]] for t in toks],
            "w0": toks[0][0], "w1": max([toks[-1][0]] + toks[-1][2]),
            "status": "off", "dur": 0.0, "_toks": toks}


# ----------------------------------------------------------------------------- worker

_workers: dict[str, asyncio.Task] = {}
_taken: dict[str, set] = {}
_focus: dict[str, int] = {}
_sem: asyncio.Semaphore | None = None


def sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(CONCURRENCY)
    return _sem


def set_focus(doc_id: str, chunk: int):
    _focus[doc_id] = chunk


def ensure_worker(doc_id: str):
    t = _workers.get(doc_id)
    if t is None or t.done():
        _workers[doc_id] = asyncio.create_task(_run(doc_id))


def working(doc_id: str) -> set:
    return set(_taken.get(doc_id, set()))


def is_running(doc_id: str) -> bool:
    t = _workers.get(doc_id)
    return t is not None and not t.done()


def _next_pending(chunks: list, taken: set, focus: int):
    pend = [c["i"] for c in chunks if c["status"] in ("pending", "error_retry") and c["i"] not in taken]
    if not pend:
        return None
    after = [i for i in pend if i >= focus]
    return min(after) if after else min(pend)


async def _run(doc_id: str):
    taken: set[int] = set()
    _taken[doc_id] = taken
    lock = asyncio.Lock()

    async def one_worker(client):
        while True:
            async with lock:
                chunks = store.load_chunks(doc_id)
                meta = store.load_meta(doc_id)
                if not meta or meta.get("paused"):
                    return
                nxt = _next_pending(chunks, taken, _focus.get(doc_id, 0))
                if nxt is None:
                    return
                taken.add(nxt)
                chunk = chunks[nxt]
            try:
                async with sem():
                    res = await _generate_chunk(client, doc_id, chunk, meta)
                status, extra = "done", res
            except Exception as e:  # keep going with the rest of the book
                log.exception("chunk %s/%s failed", doc_id, nxt)
                status, extra = "error", {"err": str(e)[:400]}
            async with lock:
                chunks = store.load_chunks(doc_id)
                if not chunks or nxt >= len(chunks):
                    return
                cur_meta = store.load_meta(doc_id) or {}
                if chunks[nxt]["status"] == "pending" and cur_meta.get("gen", 0) == meta.get("gen", 0):
                    chunks[nxt]["status"] = status
                    chunks[nxt].update(extra)
                    store.save_chunks(doc_id, chunks)
                    store.clear_exports(doc_id)
                taken.discard(nxt)

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(one_worker(client) for _ in range(CONCURRENCY)))


async def _ffmpeg(*args, data: bytes | None = None) -> bytes:
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", *args,
        stdin=asyncio.subprocess.PIPE if data is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate(data)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + err.decode()[:300])
    return out


async def _probe_duration(path) -> float | None:
    p = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await p.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return None


async def _generate_chunk(client, doc_id: str, chunk: dict, meta: dict) -> dict:
    voice = meta.get("voice")
    if not voice:
        raise GeminiError("No voice selected")
    wav = await synthesize(client, chunk["text"], voice, meta.get("style", ""))
    with wave.open(io.BytesIO(wav)) as w:
        dur = w.getnframes() / float(w.getframerate())
    mp3 = await _ffmpeg("-f", "wav", "-i", "pipe:0", "-ac", "1", "-b:a", MP3_BITRATE,
                        "-f", "mp3", "pipe:1", data=wav)
    audio_dir = store.doc_dir(doc_id) / "audio"
    audio_dir.mkdir(exist_ok=True)
    stem = f"c{chunk['i']}_g{meta.get('gen', 0)}"
    (audio_dir / f"{stem}.mp3").write_bytes(mp3)
    mdur = await _probe_duration(audio_dir / f"{stem}.mp3") or dur

    tokens = [(t[0], t[1]) for t in chunk["tok"]]
    try:
        heard = await transcribe_words(client, mp3, "audio/mp3", mock_text=chunk["text"])
        aligned, ratio = align(tokens, heard, dur)
    except Exception as e:  # timing is best-effort; audio is what matters
        log.warning("alignment failed for %s/%s: %s", doc_id, chunk["i"], e)
        aligned, ratio = align(tokens, [], dur)
    merged = {t[0]: t[2] for t in chunk["tok"] if t[2]}
    timings = []
    for wi, s, e in aligned:
        timings.append([wi, s, e])
        for m in merged.get(wi, []):
            timings.append([m, s, e])
    (audio_dir / f"{stem}.json").write_text(json.dumps(timings, separators=(",", ":")))
    return {"dur": round(dur, 3), "mdur": round(mdur, 3), "match": round(ratio, 2), "err": None, "file": stem}


def resume_all():
    """On startup, continue any document that still has queued chunks."""
    for doc_id in store.list_ids():
        chunks = store.load_chunks(doc_id)
        meta = store.load_meta(doc_id) or {}
        if any(c["status"] == "pending" for c in chunks) and not meta.get("paused"):
            set_focus(doc_id, (meta.get("focus") or 0))
            ensure_worker(doc_id)
