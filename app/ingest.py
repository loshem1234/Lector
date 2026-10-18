"""Turn pasted text or a PDF into Lector's document model.

Document model (stored as doc.json):
  words:    [[text, page, x0, y0, x1, y1], ...]   page = -1 and no box for pasted text
  skipw:    word indices never read aloud (footnote markers like ¹ or [12])
  join:     word indices that continue the previous word (hyphenated line breaks)
  paras:    [{"s": first word, "e": end word (exclusive), "k": kind, "sec": section}]
            kind is body | heading | footnote | header | pagenum
  sections: [{"title", "level", "p0", "p1"}]  paragraph ranges, from the book's own splits
"""
from __future__ import annotations

import io
import re
import statistics
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

import pymupdf

from .config import OCR_LANGS

SKIP_KINDS = {"footnote", "header", "pagenum"}
HEADING_RE = re.compile(
    r"^\s*(part|book|chapter|section|volume|prologue|epilogue|preface|introduction|"
    r"appendix|liber|caput|pars|sectio|κεφάλαιον|κεφάλαιο|βιβλίον|μέρος|teil|kapitel|"
    r"chapitre|livre|partie)\b",
    re.I,
)
ROMAN_RE = re.compile(r"^[ivxlcdm]+\.?$", re.I)
MARKER_RE = re.compile(r"^(\[\d+\]|\(\d+\)|[*†‡§¶]+|\d{1,3})$")


def norm(s: str) -> str:
    """Lowercase, strip accents/breathings and punctuation (used for matching)."""
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("ς", "σ")
    return re.sub(r"[\W_]+", "", s)


# ----------------------------------------------------------------------------- text

def parse_text(raw: str, title: str | None = None) -> dict:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b for b in re.split(r"\n\s*\n", raw) if b.strip()]
    if len(blocks) == 1 and raw.count("\n") > 3:
        blocks = [b for b in raw.split("\n") if b.strip()]

    words, paras, skipw, heads = [], [], [], []
    for b in blocks:
        text = " ".join(b.split())
        kind, level = "body", 0
        m = re.match(r"^(#{1,6})\s+(.*)$", text)
        if m:
            kind, level, text = "heading", len(m.group(1)), m.group(2)
        elif len(text) < 90 and (HEADING_RE.match(text) or ROMAN_RE.match(text)
                                 or (text.isupper() and len(text.split()) <= 10 and any(c.isalpha() for c in text))):
            kind = "heading"
            level = 1 if re.match(r"^\s*(part|book|volume|liber|pars|βιβλίον|μέρος)\b", text, re.I) else 2
        s = len(words)
        for w in text.split():
            m = re.match(r"^(.*?)((?:\[\d+\])+)([.,;:!?»”\"')]*)$", w) if kind == "body" else None
            if m and m.group(2):
                # "things.[1]" -> "things." (read) + "[1]" (shown, not read)
                base = m.group(1) + m.group(3)
                if base:
                    words.append([base, -1, 0, 0, 0, 0])
                skipw.append(len(words))
                words.append([m.group(2), -1, 0, 0, 0, 0])
                continue
            words.append([w, -1, 0, 0, 0, 0])
        if len(words) > s:
            paras.append({"s": s, "e": len(words), "k": kind})
            if kind == "heading":
                heads.append((len(paras) - 1, level, text))

    sections = []
    if heads:
        if heads[0][0] > 0:
            sections.append({"title": "Opening", "level": 1, "p0": 0})
        for pi, lvl, t in heads:
            sections.append({"title": t[:120], "level": lvl, "p0": pi})
    else:
        sections.append({"title": title or "Text", "level": 1, "p0": 0})
    return _finish({"source": "text", "title": title or _guess_title(words, paras),
                    "pages": 0, "page_sizes": [], "words": words, "skipw": skipw,
                    "join": [], "paras": paras}, sections)


def _guess_title(words, paras):
    if not paras:
        return "Untitled"
    p = paras[0]
    t = " ".join(w[0] for w in words[p["s"]:min(p["e"], p["s"] + 10)])
    return (t[:60] + "…") if len(t) > 60 else t


def _finish(doc: dict, sections: list[dict]) -> dict:
    sections = sorted(sections, key=lambda s: s["p0"])
    clean = []
    for s in sections:  # drop empty / duplicate starts
        if clean and clean[-1]["p0"] == s["p0"]:
            if s["level"] <= clean[-1]["level"]:
                clean[-1] = s
            continue
        clean.append(s)
    n = len(doc["paras"])
    for i, s in enumerate(clean):
        s["p1"] = clean[i + 1]["p0"] if i + 1 < len(clean) else n
    clean = [s for s in clean if s["p1"] > s["p0"]]
    for si, s in enumerate(clean):
        for p in doc["paras"][s["p0"]:s["p1"]]:
            p["sec"] = si
    doc["sections"] = clean
    return doc


# ----------------------------------------------------------------------------- PDF

class Line:
    __slots__ = ("page", "bbox", "words", "block", "size", "kind", "bold")

    def __init__(self, page, block):
        self.page, self.block, self.words, self.kind, self.bold = page, block, [], "body", False

    @property
    def text(self):
        return " ".join(w[0] for w in self.words)


def _lines_from_rawdict(page, pno) -> list[Line]:
    out = []
    d = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    for bi, b in enumerate(d["blocks"]):
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            if abs(ln.get("dir", (1, 0))[1]) > 0.3:  # skip rotated / vertical text
                continue
            L = Line(pno, bi)
            cur = None  # [text, x0, y0, x1, y1, size, sup]
            bold_chars = total = 0
            maxsize = max((s["size"] for s in ln["spans"]), default=0)

            def flush():
                nonlocal cur
                if cur and cur[0].strip():
                    L.words.append(cur)
                cur = None

            for sp in ln["spans"]:
                size = sp["size"]
                sup = bool(sp["flags"] & 1) or (size < 0.8 * maxsize and maxsize > 0)
                is_bold = bool(sp["flags"] & 16) or "bold" in sp.get("font", "").lower()
                for ch in sp["chars"]:
                    c = ch["c"]
                    if c.isspace():
                        flush()
                        continue
                    total += 1
                    bold_chars += is_bold
                    x0, y0, x1, y1 = ch["bbox"]
                    if cur is not None and cur[6] != sup:
                        flush()
                    if cur is None:
                        cur = [c, x0, y0, x1, y1, size, sup]
                    else:
                        cur[0] += c
                        cur[1], cur[2] = min(cur[1], x0), min(cur[2], y0)
                        cur[3], cur[4] = max(cur[3], x1), max(cur[4], y1)
                        cur[5] = max(cur[5], size)
            flush()
            if L.words:
                L.bbox = (min(w[1] for w in L.words), min(w[2] for w in L.words),
                          max(w[3] for w in L.words), max(w[4] for w in L.words))
                main = [w[5] for w in L.words if not w[6]] or [w[5] for w in L.words]
                L.size = statistics.median(main)
                L.bold = total > 0 and bold_chars / total > 0.6
                out.append(L)
    return out


def _lines_from_ocr(page, pno) -> list[Line]:
    import pytesseract
    from PIL import Image

    zoom = 300 / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    try:
        data = pytesseract.image_to_data(img, lang=OCR_LANGS, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:
        data = pytesseract.image_to_data(img, lang="eng", output_type=pytesseract.Output.DICT)
    lines: dict[tuple, Line] = {}
    order = []
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        if not txt or float(data["conf"][i]) < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = Line(pno, data["block_num"][i] * 1000 + data["par_num"][i])
            order.append(key)
        x0 = data["left"][i] / zoom
        y0 = data["top"][i] / zoom
        x1 = x0 + data["width"][i] / zoom
        y1 = y0 + data["height"][i] / zoom
        lines[key].words.append([txt, x0, y0, x1, y1, 0.0, False])
    out = []
    for key in order:
        L = lines[key]
        L.bbox = (min(w[1] for w in L.words), min(w[2] for w in L.words),
                  max(w[3] for w in L.words), max(w[4] for w in L.words))
        # cap-height-ish estimate of font size from line height
        L.size = round((L.bbox[3] - L.bbox[1]) * 0.8, 1)
        for w in L.words:
            w[5] = L.size
        out.append(L)
    return out


def _is_scanned(page) -> bool:
    return len(page.get_text("text").strip()) < 40 and len(page.get_images()) > 0


def parse_pdf(data: bytes, title: str | None = None, progress=None) -> dict:
    pdf = pymupdf.open(stream=data, filetype="pdf")
    npages = pdf.page_count
    page_sizes, lines, ocr_pages = [], [], 0
    for pno in range(npages):
        page = pdf[pno]
        page_sizes.append([round(page.rect.width, 2), round(page.rect.height, 2)])
        if _is_scanned(page):
            ocr_pages += 1
            lines.extend(_lines_from_ocr(page, pno))
        else:
            lines.extend(_lines_from_rawdict(page, pno))
        if progress and pno % 5 == 0:
            progress(f"Reading page {pno + 1} of {npages}")

    # --- body font size: mode weighted by characters
    sizes = Counter()
    for L in lines:
        sizes[round(L.size * 2) / 2] += len(L.text)
    body = sizes.most_common(1)[0][0] if sizes else 10.0

    # --- running headers / footers and page numbers
    def margin_key(L):
        return re.sub(r"\d+", "#", norm(L.text))

    repeats = Counter()
    for L in lines:
        h = page_sizes[L.page][1]
        if L.bbox[1] < 0.09 * h or L.bbox[3] > 0.91 * h:
            repeats[margin_key(L)] += 1
    min_rep = max(3, int(0.15 * npages))
    for L in lines:
        h = page_sizes[L.page][1]
        in_margin = L.bbox[1] < 0.09 * h or L.bbox[3] > 0.91 * h
        if not in_margin:
            continue
        t = L.text.strip()
        if re.fullmatch(r"[-–—\s]*(\d{1,4}|[ivxlcdm]{1,7})[-–—\s]*", t, re.I):
            L.kind = "pagenum"
        elif len(L.words) <= 14 and repeats[margin_key(L)] >= min_rep:
            L.kind = "header"

    # --- footnotes: small-type lines at the foot of a page
    by_page: dict[int, list[Line]] = {}
    for L in lines:
        by_page.setdefault(L.page, []).append(L)
    for pno, pls in by_page.items():
        h = page_sizes[pno][1]
        content = [L for L in pls if L.kind == "body"]
        content.sort(key=lambda L: L.bbox[1])
        for L in reversed(content):
            if L.size <= body * 0.9 and L.bbox[1] > 0.4 * h:
                L.kind = "footnote"
            else:
                break

    # --- headings
    for L in lines:
        if L.kind != "body":
            continue
        t = L.text.strip()
        nwords = len(L.words)
        if nwords > 14:
            continue
        if L.size >= body * 1.18 or (HEADING_RE.match(t) and (L.bold or L.size > body * 1.05)) \
                or (L.bold and nwords <= 8 and (HEADING_RE.match(t) or ROMAN_RE.match(t) or t.isupper())):
            L.kind = "heading"

    # --- reading order per page: running head, body in stream order, footnotes, foot of page
    ordered = []
    for pno in sorted(by_page):
        pls = by_page[pno]
        h = page_sizes[pno][1]
        top = [L for L in pls if L.kind in ("header", "pagenum") and L.bbox[1] < 0.5 * h]
        bottom = [L for L in pls if L.kind in ("header", "pagenum") and L.bbox[1] >= 0.5 * h]
        notes = sorted((L for L in pls if L.kind == "footnote"), key=lambda L: L.bbox[1])
        main = [L for L in pls if L.kind in ("body", "heading")]
        ordered += top + main + notes + bottom
    lines = ordered

    # --- words, paragraphs
    words, skipw, join, paras = [], [], [], []
    prev_key, prev_line = None, None
    for L in lines:
        key = (L.page, L.block, L.kind)
        if L.kind == "heading" and prev_key and prev_key[2] == "heading" and prev_key[0] == L.page \
                and prev_line is not None and abs(prev_line.size - L.size) < 0.6 \
                and L.bbox[1] - prev_line.bbox[3] < 1.2 * L.size:
            key = prev_key  # a heading wrapped over two lines stays one paragraph
        prev_line = L
        if key != prev_key or not paras:
            # body text that flows across a page break continues the same paragraph only for TTS;
            # visually we start a new paragraph per block.
            paras.append({"s": len(words), "e": len(words), "k": L.kind, "size": L.size})
            prev_key = key
        for wi, w in enumerate(L.words):
            idx = len(words)
            words.append([w[0], L.page, round(w[1], 1), round(w[2], 1), round(w[3], 1), round(w[4], 1)])
            if L.kind == "body" and (w[6] or (w[5] < body * 0.8)) and MARKER_RE.match(w[0]):
                skipw.append(idx)
            elif L.kind == "body" and wi == 0 and idx > 0 and words[idx - 1][0].endswith("-") \
                    and len(words[idx - 1][0]) > 1 and w[0][:1].islower():
                join.append(idx)
        paras[-1]["e"] = len(words)
    paras = [p for p in paras if p["e"] > p["s"]]

    # --- sections from the book's own splits
    sections = _sections_from_toc(pdf, paras, words) or _sections_from_headings(paras, words, body)
    if not sections:
        sections = [{"title": title or "Full text", "level": 1, "p0": 0}]
    elif sections[0]["p0"] > 0:
        if any(p["k"] not in SKIP_KINDS for p in paras[:sections[0]["p0"]]):
            sections.insert(0, {"title": "Front matter", "level": 1, "p0": 0})
        else:
            sections[0]["p0"] = 0
    for p in paras:
        p.pop("size", None)

    meta_title = (pdf.metadata or {}).get("title") or ""
    doc = {"source": "pdf", "title": title or meta_title.strip() or "Untitled PDF", "pages": npages,
           "page_sizes": page_sizes, "words": words, "skipw": skipw, "join": join, "paras": paras,
           "ocr_pages": ocr_pages, "body_size": body}
    return _finish(doc, sections)


def _para_text(p, words, limit=40):
    return " ".join(w[0] for w in words[p["s"]:min(p["e"], p["s"] + limit)])


def _sections_from_toc(pdf, paras, words):
    toc = pdf.get_toc(simple=True)
    toc = [t for t in toc if t[2] >= 1 and t[1].strip()]
    if len(toc) < 2:
        return None
    first_para_on_page = {}
    for i, p in enumerate(paras):
        pg = words[p["s"]][1]
        first_para_on_page.setdefault(pg, i)
    secs, last = [], -1
    for lvl, ttl, pg in toc:
        pg -= 1
        target = norm(ttl)
        cand = None
        # search the target page (and the next, for off-by-one outlines)
        for scan_pg in (pg, pg + 1):
            if scan_pg not in first_para_on_page:
                continue
            best = (0.0, None)
            for i in range(first_para_on_page[scan_pg], len(paras)):
                p = paras[i]
                if words[p["s"]][1] != scan_pg:
                    break
                if p["k"] in SKIP_KINDS:
                    continue
                pt = norm(_para_text(p, words, 25))
                if not pt:
                    continue
                if target and (pt.startswith(target[:30]) or target.startswith(pt[:30])):
                    score = 1.0
                else:
                    score = SequenceMatcher(None, target[:60], pt[:len(target[:60]) + 10]).ratio()
                if p["k"] == "heading":
                    score += 0.15
                if score > best[0]:
                    best = (score, i)
            if best[1] is not None and best[0] >= 0.6:
                cand = best[1]
                break
        if cand is None:
            cand = first_para_on_page.get(pg)
            if cand is None:  # page without text: next page that has some
                nxt = [p for p in first_para_on_page if p > pg]
                cand = first_para_on_page[min(nxt)] if nxt else None
        if cand is None or cand == last:
            continue  # nothing found, or several outline entries at the same spot: keep the first
        secs.append({"title": ttl.strip()[:160], "level": lvl, "p0": cand})
        last = cand
    secs.sort(key=lambda s: s["p0"])
    return secs if len(secs) >= 2 else None


def _sections_from_headings(paras, words, body):
    heads = [(i, p) for i, p in enumerate(paras) if p["k"] == "heading"]
    if not heads:
        return None
    sizes = sorted({round(p["size"]) for _, p in heads}, reverse=True)
    secs = []
    for i, p in heads:
        t = _para_text(p, words, 30)
        lvl = sizes.index(round(p["size"])) + 1
        if re.match(r"^\s*(part|book|volume|liber|pars|βιβλίον|μέρος)\b", t, re.I):
            lvl = 1
        secs.append({"title": t[:160], "level": min(lvl, 3), "p0": i})
    return secs
