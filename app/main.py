from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from pathlib import Path

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import export, jobs, store
from .config import ADMIN_PASSWORD, MOCK, SESSION_SECRET, TTS_MODEL
from .gemini import GeminiError, list_voices
from .ingest import parse_pdf, parse_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lector")

app = FastAPI(title="Lector", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2000)
STATIC = Path(__file__).resolve().parent.parent / "static"
COOKIE = "lector_session"
SESSION_DAYS = 60
MAX_UPLOAD = 300 * 1024 * 1024


# ----------------------------------------------------------------------------- auth

def _sign(ts: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), f"ok:{ts}".encode(), hashlib.sha256).hexdigest()


def _valid(cookie: str | None) -> bool:
    if not ADMIN_PASSWORD:
        return True
    if not cookie or "." not in cookie:
        return False
    ts, sig = cookie.split(".", 1)
    if not ts.isdigit() or time.time() - int(ts) > SESSION_DAYS * 86400:
        return False
    return hmac.compare_digest(sig, _sign(ts))


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/login", "/api/me"):
        if not _valid(request.cookies.get(COOKIE)):
            return JSONResponse({"detail": "Not signed in"}, status_code=401)
    return await call_next(request)


class Login(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: Login, request: Request):
    if not ADMIN_PASSWORD:
        return {"ok": True}
    if not hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()):
        await asyncio.sleep(1.5)
        raise HTTPException(401, "Wrong password")
    ts = str(int(time.time()))
    r = JSONResponse({"ok": True})
    r.set_cookie(COOKIE, f"{ts}.{_sign(ts)}", max_age=SESSION_DAYS * 86400, httponly=True,
                 samesite="lax", secure=request.url.scheme == "https"
                 or request.headers.get("x-forwarded-proto") == "https")
    return r


@app.post("/api/logout")
async def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.get("/api/me")
async def me(request: Request):
    return {"signed_in": _valid(request.cookies.get(COOKIE)), "password_set": bool(ADMIN_PASSWORD),
            "mock": MOCK, "model": TTS_MODEL}


# ----------------------------------------------------------------------------- voices

_voice_cache: dict = {"t": 0, "v": []}


@app.get("/api/voices")
async def voices(refresh: bool = False):
    if refresh or time.time() - _voice_cache["t"] > 300 or not _voice_cache["v"]:
        try:
            _voice_cache["v"] = await list_voices()
            _voice_cache["t"] = time.time()
        except GeminiError as e:
            raise HTTPException(502, f"Could not list voices: {e}")
    return _voice_cache["v"]


# ----------------------------------------------------------------------------- library

def _summary(doc_id: str) -> dict:
    meta = store.load_meta(doc_id) or {}
    chunks = store.load_chunks(doc_id)
    active = [c for c in chunks if c["status"] != "off"]
    done = [c for c in active if c["status"] == "done"]
    return {
        "id": doc_id, "title": meta.get("title", "Untitled"), "created": meta.get("created"),
        "source": meta.get("source"), "state": meta.get("state"), "error": meta.get("error"),
        "pages": meta.get("pages", 0), "sections": meta.get("nsections", 0),
        "voice_name": meta.get("voice_name"), "size": store.folder_size(doc_id),
        "chunks_total": len(active), "chunks_done": len(done),
        "seconds": round(sum(c.get("mdur") or c["dur"] for c in done)),
        "progress": meta.get("progress"), "opened": meta.get("opened"),
    }


@app.get("/api/docs")
async def library():
    items = [_summary(i) for i in store.list_ids()]
    items.sort(key=lambda d: d.get("opened") or d.get("created") or 0, reverse=True)
    return {"items": items, "total_size": sum(d["size"] for d in items)}


async def _parse_job(doc_id: str, kind: str, payload, title: str | None):
    def progress(msg):
        store.update_meta(doc_id, state="parsing", note=msg)

    try:
        if kind == "pdf":
            doc = await asyncio.to_thread(parse_pdf, payload, title, progress)
        else:
            doc = await asyncio.to_thread(parse_text, payload, title)
        if not doc["words"]:
            raise ValueError("No readable text was found in this document.")
        chunks = jobs.plan_chunks(doc)
        store.save_doc(doc_id, doc)
        store.save_chunks(doc_id, chunks)
        store.update_meta(doc_id, state="ready", title=doc["title"], pages=doc["pages"],
                          nsections=len(doc["sections"]), nwords=len(doc["words"]),
                          ocr_pages=doc.get("ocr_pages", 0), note=None)
    except Exception as e:
        log.exception("parse failed")
        store.update_meta(doc_id, state="failed", error=str(e)[:300])


@app.post("/api/docs")
async def create_doc(file: UploadFile | None = File(None), text: str = Form(""), title: str = Form("")):
    doc_id = store.new_id()
    d = store.doc_dir(doc_id)
    d.mkdir(parents=True)
    title = title.strip() or None
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            store.delete(doc_id)
            raise HTTPException(413, "File too large")
        if file.filename.lower().endswith(".pdf") or data[:4] == b"%PDF":
            (d / "source.pdf").write_bytes(data)
            kind, payload = "pdf", data
            title = title or Path(file.filename).stem
        else:
            kind, payload = "text", data.decode("utf-8", errors="replace")
            title = title or Path(file.filename).stem
    elif text.strip():
        kind, payload = "text", text
    else:
        store.delete(doc_id)
        raise HTTPException(400, "Paste some text or choose a PDF")
    store.save_meta(doc_id, {"id": doc_id, "title": title or "Untitled", "source": kind,
                             "created": time.time(), "state": "parsing", "gen": 0})
    asyncio.create_task(_parse_job(doc_id, kind, payload, title))
    return {"id": doc_id}


def _need(doc_id: str) -> dict:
    try:
        meta = store.load_meta(doc_id)
    except ValueError:
        meta = None
    if not meta:
        raise HTTPException(404, "Not found")
    return meta


@app.get("/api/docs/{doc_id}")
async def get_meta(doc_id: str):
    meta = _need(doc_id)
    store.update_meta(doc_id, opened=time.time())
    return {**meta, **_summary(doc_id)}


@app.get("/api/docs/{doc_id}/doc")
async def get_doc(doc_id: str):
    _need(doc_id)
    p = store.doc_path(doc_id)
    if not p.exists():
        raise HTTPException(409, "Still reading the document")
    return FileResponse(p, media_type="application/json")


@app.get("/api/docs/{doc_id}/manifest")
async def manifest(doc_id: str):
    meta = _need(doc_id)
    chunks = store.load_chunks(doc_id)
    working = jobs.working(doc_id)
    out = []
    for c in chunks:
        st = c["status"]
        if st == "pending" and c["i"] in working:
            st = "working"
        out.append({"i": c["i"], "sec": c["sec"], "w0": c["w0"], "w1": c["w1"], "status": st,
                    "dur": c.get("dur", 0), "file": c.get("file"), "err": c.get("err"),
                    "match": c.get("match")})
    return {"chunks": out, "gen": meta.get("gen", 0), "running": jobs.is_running(doc_id),
            "voice": meta.get("voice"), "voice_name": meta.get("voice_name"), "style": meta.get("style", ""),
            "paused": bool(meta.get("paused"))}


class GenerateBody(BaseModel):
    voice: str | None = None
    voice_name: str | None = None
    style: str | None = None
    sections: list[int] | None = None   # None = whole document
    focus_chunk: int | None = None
    retry_errors: bool = False


@app.post("/api/docs/{doc_id}/generate")
async def generate(doc_id: str, body: GenerateBody):
    meta = _need(doc_id)
    if meta.get("state") != "ready":
        raise HTTPException(409, "Document is not ready yet")
    voice = body.voice or meta.get("voice")
    if not voice:
        raise HTTPException(400, "Choose a voice first")
    style = meta.get("style", "") if body.style is None else body.style
    with store.lock(doc_id):
        chunks = store.load_chunks(doc_id)
        changed_voice = bool(meta.get("voice")) and (voice != meta.get("voice") or style != meta.get("style", ""))
        gen = meta.get("gen", 0) + (1 if changed_voice else 0)
        wanted = set(body.sections) if body.sections is not None else None
        for c in chunks:
            if changed_voice and c["status"] in ("done", "pending", "error"):
                c.update(status="pending", file=None, dur=0.0, mdur=0.0, err=None)
            if (wanted is None or c["sec"] in wanted) and c["status"] == "off":
                c["status"] = "pending"
            if body.retry_errors and c["status"] == "error" and (wanted is None or c["sec"] in wanted):
                c.update(status="pending", err=None)
        store.save_chunks(doc_id, chunks)
        store.update_meta(doc_id, voice=voice, voice_name=body.voice_name or meta.get("voice_name") or voice,
                          style=style, gen=gen, paused=False,
                          focus=body.focus_chunk if body.focus_chunk is not None else meta.get("focus", 0))
    if changed_voice:
        _remove_audio(doc_id, keep_gen=gen)
        store.clear_exports(doc_id)
    if body.focus_chunk is not None:
        jobs.set_focus(doc_id, body.focus_chunk)
    jobs.ensure_worker(doc_id)
    return {"ok": True, "reset": changed_voice}


def _remove_audio(doc_id: str, keep_gen: int):
    a = store.doc_dir(doc_id) / "audio"
    if a.exists():
        for f in a.iterdir():
            if not f.stem.endswith(f"_g{keep_gen}"):
                f.unlink(missing_ok=True)


class Focus(BaseModel):
    chunk: int


@app.post("/api/docs/{doc_id}/focus")
async def focus(doc_id: str, body: Focus):
    meta = _need(doc_id)
    jobs.set_focus(doc_id, body.chunk)
    store.update_meta(doc_id, focus=body.chunk)
    chunks = store.load_chunks(doc_id)
    if 0 <= body.chunk < len(chunks) and chunks[body.chunk]["status"] in ("off", "error") and meta.get("voice"):
        # playing somewhere not generated yet: queue that section
        sec = chunks[body.chunk]["sec"]
        with store.lock(doc_id):
            chunks = store.load_chunks(doc_id)
            for c in chunks:
                if c["sec"] == sec and c["status"] in ("off", "error"):
                    c.update(status="pending", err=None)
            store.save_chunks(doc_id, chunks)
    jobs.ensure_worker(doc_id)
    return {"ok": True}


class Pause(BaseModel):
    paused: bool


@app.post("/api/docs/{doc_id}/pause")
async def pause(doc_id: str, body: Pause):
    _need(doc_id)
    store.update_meta(doc_id, paused=body.paused)
    if not body.paused:
        jobs.ensure_worker(doc_id)
    return {"ok": True}


class Progress(BaseModel):
    word: int
    view: str | None = None
    rate: float | None = None


@app.post("/api/docs/{doc_id}/progress")
async def progress(doc_id: str, body: Progress):
    _need(doc_id)
    kw = {"progress": body.word}
    if body.view:
        kw["view"] = body.view
    if body.rate:
        kw["rate"] = body.rate
    store.update_meta(doc_id, **kw)
    return {"ok": True}


class Rename(BaseModel):
    title: str


@app.post("/api/docs/{doc_id}/rename")
async def rename(doc_id: str, body: Rename):
    _need(doc_id)
    store.update_meta(doc_id, title=body.title.strip()[:200] or "Untitled")
    store.clear_exports(doc_id)
    return {"ok": True}


def _audio_file(doc_id: str, name: str, ext: str) -> Path:
    if not name.replace("_", "").isalnum():
        raise HTTPException(400, "bad name")
    p = store.doc_dir(doc_id) / "audio" / f"{name}.{ext}"
    if not p.exists():
        raise HTTPException(404, "Not generated yet")
    return p


@app.get("/api/docs/{doc_id}/audio/{name}.mp3")
async def audio(doc_id: str, name: str):
    _need(doc_id)
    return FileResponse(_audio_file(doc_id, name, "mp3"), media_type="audio/mpeg",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/docs/{doc_id}/timing/{name}.json")
async def timing(doc_id: str, name: str):
    _need(doc_id)
    return FileResponse(_audio_file(doc_id, name, "json"), media_type="application/json",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/docs/{doc_id}/page/{n}.png")
async def page_image(doc_id: str, n: int, scale: float = 2.0):
    _need(doc_id)
    src = store.doc_dir(doc_id) / "source.pdf"
    if not src.exists():
        raise HTTPException(404, "No PDF for this document")
    scale = 1.0 if scale < 1.25 else (2.0 if scale < 2.5 else 3.0)
    cache = store.doc_dir(doc_id) / "pages"
    cache.mkdir(exist_ok=True)
    out = cache / f"p{n}_{scale:g}.png"
    if not out.exists():
        def render():
            with pymupdf.open(src) as pdf:
                if not 0 <= n < pdf.page_count:
                    raise HTTPException(404, "No such page")
                pix = pdf[n].get_pixmap(matrix=pymupdf.Matrix(scale, scale))
                pix.save(out)
        await asyncio.to_thread(render)
    return FileResponse(out, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/docs/{doc_id}/source.pdf")
async def source_pdf(doc_id: str):
    _need(doc_id)
    p = store.doc_dir(doc_id) / "source.pdf"
    if not p.exists():
        raise HTTPException(404, "No PDF")
    return FileResponse(p, media_type="application/pdf")


def _dl_name(meta, suffix):
    return export._safe(meta.get("title", "lector"), 80) + suffix


@app.get("/api/docs/{doc_id}/export/full.mp3")
async def export_full(doc_id: str):
    meta = _need(doc_id)
    try:
        p = await asyncio.to_thread(export.full_mp3, doc_id)
    except FileNotFoundError as e:
        raise HTTPException(409, str(e))
    return FileResponse(p, media_type="audio/mpeg", filename=_dl_name(meta, ".mp3"))


@app.get("/api/docs/{doc_id}/export/sections.zip")
async def export_zip(doc_id: str):
    meta = _need(doc_id)
    try:
        p = await asyncio.to_thread(export.sections_zip, doc_id)
    except FileNotFoundError as e:
        raise HTTPException(409, str(e))
    return FileResponse(p, media_type="application/zip", filename=_dl_name(meta, " (sections).zip"))


@app.get("/api/docs/{doc_id}/export/section/{si}.mp3")
async def export_section(doc_id: str, si: int):
    meta = _need(doc_id)
    doc = store.load_doc(doc_id)
    try:
        p = await asyncio.to_thread(export.section_mp3, doc_id, si)
    except FileNotFoundError as e:
        raise HTTPException(409, str(e))
    name = f"{si + 1:02d} - {export._safe(doc['sections'][si]['title'])}.mp3"
    return FileResponse(p, media_type="audio/mpeg", filename=name)


@app.delete("/api/docs/{doc_id}")
async def delete_doc(doc_id: str):
    _need(doc_id)
    store.update_meta(doc_id, paused=True)
    await asyncio.sleep(0.2)
    store.delete(doc_id)
    return {"ok": True}


@app.delete("/api/docs/{doc_id}/audio")
async def delete_audio(doc_id: str):
    """Free space but keep the document: drop generated audio and exports."""
    meta = _need(doc_id)
    with store.lock(doc_id):
        chunks = store.load_chunks(doc_id)
        for c in chunks:
            c.update(status="off", file=None, dur=0.0, mdur=0.0, err=None)
        store.save_chunks(doc_id, chunks)
        store.update_meta(doc_id, gen=meta.get("gen", 0) + 1, paused=False)
    _remove_audio(doc_id, keep_gen=-1)
    store.clear_exports(doc_id)
    return {"ok": True}


@app.on_event("startup")
async def startup():
    if not ADMIN_PASSWORD:
        log.warning("ADMIN_PASSWORD is not set: the app is open to anyone who finds the URL.")
    if MOCK:
        log.warning("Running in MOCK mode (no GEMINI_API_KEY): audio is placeholder tones.")
    # documents whose parse was interrupted by a restart
    for doc_id in store.list_ids():
        m = store.load_meta(doc_id) or {}
        if m.get("state") == "parsing":
            store.update_meta(doc_id, state="failed", error="Interrupted by a restart; please upload again.")
    jobs.resume_all()


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/manifest.webmanifest")
async def webmanifest():
    return Response(content=(STATIC / "manifest.webmanifest").read_text(), media_type="application/manifest+json")
