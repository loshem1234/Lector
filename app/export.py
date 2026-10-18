"""MP3 export: one file with chapter markers, or a zip with one MP3 per section."""
from __future__ import annotations

import re
import subprocess
import zipfile
from pathlib import Path

from . import store


def _safe(name: str, n: int = 60) -> str:
    s = re.sub(r"[^\w\s.-]", "", name, flags=re.UNICODE).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:n] or "section").strip()


def _ff(args):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])


def _esc(s: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def _ready(doc_id):
    doc = store.load_doc(doc_id)
    meta = store.load_meta(doc_id)
    chunks = store.load_chunks(doc_id)
    audio = store.doc_dir(doc_id) / "audio"
    secs = {}
    for c in chunks:
        if c["status"] == "done" and c.get("file") and (audio / f"{c['file']}.mp3").exists():
            secs.setdefault(c["sec"], []).append(c)
    return doc, meta, secs, audio


def section_mp3(doc_id: str, si: int) -> Path:
    doc, meta, secs, audio = _ready(doc_id)
    if si not in secs:
        raise FileNotFoundError("This section has no audio yet")
    out_dir = store.doc_dir(doc_id) / "export"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"s{si}.mp3"
    if out.exists():
        return out
    lst = out_dir / f"s{si}.txt"
    lst.write_text("".join(f"file '{(audio / (c['file'] + '.mp3')).as_posix()}'\n"
                           for c in sorted(secs[si], key=lambda c: c["i"])))
    title = doc["sections"][si]["title"]
    _ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
         "-metadata", f"title={title}", "-metadata", f"album={meta.get('title', '')}",
         "-metadata", f"track={si + 1}", "-id3v2_version", "3", str(out)])
    return out


def full_mp3(doc_id: str) -> Path:
    doc, meta, secs, audio = _ready(doc_id)
    if not secs:
        raise FileNotFoundError("No audio has been generated yet")
    out_dir = store.doc_dir(doc_id) / "export"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "full.mp3"
    if out.exists():
        return out
    files, chapters, t = [], [], 0.0
    for si in sorted(secs):
        cs = sorted(secs[si], key=lambda c: c["i"])
        start = t
        for c in cs:
            files.append(audio / f"{c['file']}.mp3")
            t += c.get("mdur") or c["dur"]
        chapters.append((start, t, doc["sections"][si]["title"]))
    lst = out_dir / "full.txt"
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files))
    md = [";FFMETADATA1", f"title={_esc(meta.get('title', ''))}", f"album={_esc(meta.get('title', ''))}",
          "artist=Lector"]
    for s, e, ttl in chapters:
        md += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(s * 1000)}", f"END={int(e * 1000)}",
               f"title={_esc(ttl)}"]
    mdf = out_dir / "chapters.txt"
    mdf.write_text("\n".join(md) + "\n", encoding="utf-8")
    _ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(mdf), "-map", "0:a",
         "-map_metadata", "1", "-map_chapters", "1", "-c", "copy", "-id3v2_version", "3", str(out)])
    return out


def sections_zip(doc_id: str) -> Path:
    doc, meta, secs, audio = _ready(doc_id)
    if not secs:
        raise FileNotFoundError("No audio has been generated yet")
    out_dir = store.doc_dir(doc_id) / "export"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "sections.zip"
    if out.exists():
        return out
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for n, si in enumerate(sorted(secs), 1):
            p = section_mp3(doc_id, si)
            z.write(p, f"{n:02d} - {_safe(doc['sections'][si]['title'])}.mp3")
    return out
