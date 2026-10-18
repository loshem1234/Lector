"""On-disk library: one folder per document under DATA_DIR/docs/<id>/."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from .config import DOCS_DIR

_locks: dict[str, threading.RLock] = {}
_glock = threading.Lock()


def lock(doc_id: str) -> threading.RLock:
    with _glock:
        return _locks.setdefault(doc_id, threading.RLock())


def new_id() -> str:
    return time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8]


def doc_dir(doc_id: str) -> Path:
    if not doc_id.replace("-", "").isalnum():
        raise ValueError("bad id")
    return DOCS_DIR / doc_id


def _write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _read(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_meta(doc_id: str, meta: dict):
    with lock(doc_id):
        _write(doc_dir(doc_id) / "meta.json", meta)


def load_meta(doc_id: str) -> dict | None:
    return _read(doc_dir(doc_id) / "meta.json")


def update_meta(doc_id: str, **kw) -> dict:
    with lock(doc_id):
        m = load_meta(doc_id) or {}
        m.update(kw)
        save_meta(doc_id, m)
        return m


def save_doc(doc_id: str, doc: dict):
    _write(doc_dir(doc_id) / "doc.json", doc)


def doc_path(doc_id: str) -> Path:
    return doc_dir(doc_id) / "doc.json"


def load_doc(doc_id: str) -> dict | None:
    return _read(doc_path(doc_id))


def save_chunks(doc_id: str, chunks: list):
    with lock(doc_id):
        _write(doc_dir(doc_id) / "chunks.json", chunks)


def load_chunks(doc_id: str) -> list:
    return _read(doc_dir(doc_id) / "chunks.json", []) or []


def list_ids() -> list[str]:
    return sorted((p.name for p in DOCS_DIR.iterdir() if p.is_dir() and (p / "meta.json").exists()), reverse=True)


def folder_size(doc_id: str) -> int:
    total = 0
    for root, _, files in os.walk(doc_dir(doc_id)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def delete(doc_id: str):
    shutil.rmtree(doc_dir(doc_id), ignore_errors=True)
    with _glock:
        _locks.pop(doc_id, None)


def clear_exports(doc_id: str):
    shutil.rmtree(doc_dir(doc_id) / "export", ignore_errors=True)
