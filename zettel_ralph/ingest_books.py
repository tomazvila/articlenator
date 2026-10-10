#!/usr/bin/env python3
"""Phase A book adapter: split PDF and EPUB books into bounded literature notes.

It emits the Phase-A contract (README "Articlenator handoff"): one note
`DIR/lit/<id>.md` per unit and one `DIR/queue.json` item per unit (kind "article",
stage "extracted"). The synthesis loop reads these like any other article.

Split rules:
  * EPUB: the OPF spine gives the order. The nav document or NCX gives the titles. A spine
    file without a nav title joins the unit before it.
  * PDF: the outline (bookmarks) gives the chapters. The adapter picks the shallowest
    outline depth where at most 20% of the words sit in sections above 1.5 times the limit. A PDF without a usable outline
    splits into page windows (`split_method: page-window`).
  * A unit longer than --max-words splits into parts at heading or paragraph boundaries.
  * A unit shorter than --min-words joins its neighbor.
  * Front and back matter (copyright, contents, index, ...) is skipped and reported.

Run inside the project's nix env:
    nix develop --command python3 zettel_ralph/ingest_books.py --staging DIR BOOK...
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _lock import state_lock  # noqa: E402

DEFAULT_MAX_WORDS = 7000
DEFAULT_MIN_WORDS = 800

SKIP_RE = re.compile(
    r"^\W*(?:"
    r"cover(?: page)?|half[- ]?title(?: page)?|title page|series page|copyright(?: page| notice)?|"
    r"table of contents|contents(?: at a glance| in brief)?|detailed contents|"
    r"list of (?:figures|tables|illustrations)|dedication|acknowledg(?:e)?ments?|"
    r"about the authors?|about the book|about the publisher|praise for.*|also by.*|"
    r"[a-z]|symbols|index|subject index|bibliography|colophon|endnotes|notes|color plates?|back cover"
    r")\W*$",
    re.IGNORECASE,
)
LOOSE_SKIP_RE = re.compile(
    r"(?:^|[^a-z])(?:cover|copyright|colou?r plates?|contents[a-z0-9]*)(?:\.pdf)?$", re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def slugify(text: str, limit: int) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:limit].strip("-")


def is_skipped_title(title: str) -> bool:
    t = (title or "").strip()
    return bool(SKIP_RE.match(t) or LOOSE_SKIP_RE.search(t))


def clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").replace("\r", " ")).strip()
    t = re.sub(r"\.pdf$", "", t, flags=re.IGNORECASE)
    m = re.fullmatch(r"(?:contents)?ch(?:apter)?\s*0*(\d+)", t, flags=re.IGNORECASE)
    if m:
        return f"Chapter {int(m.group(1))}"
    return t


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\xad", "")
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")


# --------------------------------------------------------------------------- #
# Text cleaning shared by PDF and EPUB
# --------------------------------------------------------------------------- #
_TERMINAL = tuple('.!?:;"\')]”’»…')
_BULLET_RE = re.compile(r"^(?:[•▪‣◦●■\-–—*]\s+|\(?\d{1,2}[.)]\s+)")
_PAGENUM_RE = re.compile(r"^(?:page\s+)?(?:\d{1,4}|[ivxlcdm]{1,7})$", re.IGNORECASE)


def _norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", line.lower())).strip()


def detect_repeated_lines(pages: list[list[str]]) -> set[str]:
    """Return normalized lines that repeat at page edges (running headers and footers)."""
    counts: dict[str, int] = {}
    for lines in pages:
        edge = set(lines[:2] + lines[-2:])
        for line in edge:
            key = _norm_line(line)
            if 3 <= len(key) <= 90 and not key.endswith("."):
                counts[key] = counts.get(key, 0) + 1
    need = max(3, int(0.03 * len(pages)))
    return {k for k, c in counts.items() if c >= need}


def strip_page_furniture(lines: list[str], repeated: set[str]) -> list[str]:
    """Drop repeated edge lines and lone page numbers from one page."""
    lines = [ln.strip() for ln in lines if ln.strip()]
    n = len(lines)
    drop = {
        i
        for i, line in enumerate(lines)
        if (i < 2 or i >= n - 2) and (_norm_line(line) in repeated or _PAGENUM_RE.match(line))
    }
    return [line for i, line in enumerate(lines) if i not in drop]


def _is_heading_line(line: str) -> bool:
    return (
        0 < len(line) <= 80
        and not line.endswith(_TERMINAL + (",",))
        and (line[0].isupper() or line[0].isdigit())
    )


def lines_to_paragraphs(lines: list[str]) -> list[str]:
    """Join wrapped lines into paragraphs and fix hyphenation at line ends."""
    lines = [ln.strip() for ln in lines if ln.strip()]
    if not lines:
        return []
    lens = sorted(len(ln) for ln in lines if len(ln) >= 30) or sorted(len(ln) for ln in lines)
    width = lens[min(len(lens) - 1, int(len(lens) * 0.75))]
    paras: list[str] = []
    cur = ""
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if cur:
            if cur.endswith("-") and len(cur) > 1 and cur[-2].isalpha() and line[:1].islower():
                cur = cur[:-1] + line
            else:
                cur = f"{cur} {line}"
        else:
            cur = line
        if nxt is None:
            break
        short = len(line) < 0.6 * width
        hyphen_join = line.endswith("-") and line[-2:-1].isalpha() and nxt[:1].islower()
        flush = False
        if not hyphen_join:
            if short and (line.endswith(_TERMINAL) or _is_heading_line(line)) and not nxt[:1].islower():
                flush = True
            elif _BULLET_RE.match(nxt) and not _BULLET_RE.match(cur[:3]):
                flush = True
            elif _BULLET_RE.match(nxt) and short:
                flush = True
        if flush:
            paras.append(cur)
            cur = ""
    if cur:
        paras.append(cur)
    return [re.sub(r"\s+", " ", p).strip() for p in paras if p.strip()]


def is_heading_paragraph(p: str) -> bool:
    return p.startswith("#") or (len(p) <= 80 and _is_heading_line(p) and "\n" not in p)


def split_paragraphs(paras: list[str], max_words: int) -> list[list[str]]:
    """Split paragraphs into parts of about equal size, preferring heading boundaries."""
    total = sum(len(p.split()) for p in paras)
    if total <= max_words or len(paras) < 2:
        return [paras]
    n_parts = math.ceil(total / max_words)
    target = total / n_parts
    cum = []
    run = 0
    for p in paras:
        cum.append(run)  # words before paragraph i
        run += len(p.split())
    cuts: list[int] = []
    last = 0
    for k in range(1, n_parts):
        ideal = k * target
        best, best_cost = None, None
        for i in range(last + 1, len(paras)):
            cost = abs(cum[i] - ideal)
            if is_heading_paragraph(paras[i]):
                cost = max(0.0, cost - 0.25 * target) - 1  # a heading within 25% beats a plain cut
            if best_cost is None or cost < best_cost:
                best, best_cost = i, cost
        if best is None:
            break
        cuts.append(best)
        last = best
    parts, start = [], 0
    for c in cuts + [len(paras)]:
        if paras[start:c]:
            parts.append(paras[start:c])
        start = c
    return parts


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
class Section:
    """A candidate chapter: title, paragraphs, and location data."""

    def __init__(self, title: str, paras: list[str], *, pages: tuple[int, int] | None = None,
                 href: str | None = None) -> None:
        self.title = title
        self.paras = paras
        self.pages = pages
        self.href = href

    @property
    def words(self) -> int:
        return sum(len(p.split()) for p in self.paras)


class BookError(Exception):
    """A book file that the adapter cannot parse."""


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _flatten_outline(reader, outline, depth=0) -> list[tuple[int, str, int]]:
    out = []
    for item in outline:
        if isinstance(item, list):
            out.extend(_flatten_outline(reader, item, depth + 1))
            continue
        try:
            page = reader.get_destination_page_number(item)
        except Exception:  # noqa: BLE001 - a broken bookmark is skipped
            continue
        if page is None or page < 0:
            continue
        out.append((depth, str(item.title), page))
    return out


def _outline_segments(entries, n_pages, max_words, page_words):
    """Pick the shallowest depth whose segments fit; return [(title, start, end)] (0-based)."""
    best = None
    for depth in sorted({d for d, _, _ in entries}):
        sel = [e for e in entries if e[0] <= depth]
        sel.sort(key=lambda e: e[2])
        uniq: list[tuple[int, str, int]] = []
        for e in sel:
            if uniq and uniq[-1][2] == e[2]:
                continue
            uniq.append(e)
        if len(uniq) < 2:
            continue
        segs = []
        for i, (_, title, start) in enumerate(uniq):
            end = (uniq[i + 1][2] - 1) if i + 1 < len(uniq) else n_pages - 1
            segs.append((title, start, max(start, end)))
        best = segs
        sizes = [sum(page_words[s:e + 1]) for _, s, e in segs]
        heavy = sum(w for w in sizes if w > 1.5 * max_words)
        if heavy <= 0.2 * max(1, sum(sizes)):
            break
    return best


def parse_pdf(path: Path, max_words: int) -> tuple[dict, list[Section], str]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        n_pages = len(reader.pages)
        raw_pages = [normalize_text(pg.extract_text() or "").split("\n") for pg in reader.pages]
        info = reader.metadata
        entries = _flatten_outline(reader, reader.outline) if reader.outline else []
    except Exception as exc:  # noqa: BLE001
        raise BookError(f"cannot parse pdf: {str(exc)[:160]}") from exc
    if n_pages == 0:
        raise BookError("pdf has no pages")
    text_pages = sum(1 for p in raw_pages if len(" ".join(p).split()) >= 20)
    if text_pages < max(1, n_pages // 10):
        raise BookError("no text layer (scanned image PDF, OCR not done)")
    repeated = detect_repeated_lines(raw_pages)
    pages = [strip_page_furniture(p, repeated) for p in raw_pages]
    page_words = [len(" ".join(p).split()) for p in pages]
    meta = {
        "title": (getattr(info, "title", None) or "") if info else "",
        "author": (getattr(info, "author", None) or "") if info else "",
    }
    segs = _outline_segments(entries, n_pages, max_words, page_words) if entries else None
    sections: list[Section] = []
    if segs:
        method = "outline"
        for title, s, e in segs:
            lines = [ln for pg in pages[s:e + 1] for ln in pg]
            sections.append(Section(clean_title(title), lines_to_paragraphs(lines), pages=(s + 1, e + 1)))
    else:
        method = "page-window"
        start, acc = 0, 0
        for i in range(n_pages):
            if acc and acc + page_words[i] > max_words:
                lines = [ln for pg in pages[start:i] for ln in pg]
                sections.append(Section(f"Pages {start + 1}-{i}", lines_to_paragraphs(lines), pages=(start + 1, i)))
                start, acc = i, 0
            acc += page_words[i]
        lines = [ln for pg in pages[start:] for ln in pg]
        sections.append(
            Section(f"Pages {start + 1}-{n_pages}", lines_to_paragraphs(lines), pages=(start + 1, n_pages))
        )
    return meta, sections, method


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #
class _EpubHtml(HTMLParser):
    _HEAD = {"h1", "h2", "h3", "h4", "h5", "h6"}
    _BLOCK = {"p", "div", "li", "blockquote", "tr", "section", "article", "br", "pre", "figcaption"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paras: list[str] = []
        self._buf: list[str] = []
        self._head: int | None = None
        self._skip = 0
        self._li = 0
        self.first_heading = ""

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if text:
            if self._head:
                if not self.first_heading:
                    self.first_heading = text
                text = "#" * max(2, self._head) + " " + text
            elif self._li:
                text = "- " + text
            self.paras.append(text)

    def handle_starttag(self, tag, attrs):  # noqa: D102
        if tag in ("script", "style", "head", "nav"):
            self._skip += 1
        elif tag in self._HEAD:
            self._flush()
            self._head = int(tag[1])
        elif tag == "li":
            self._flush()
            self._li += 1
        elif tag in self._BLOCK:
            self._flush()

    def handle_endtag(self, tag):  # noqa: D102
        if tag in ("script", "style", "head", "nav"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._HEAD:
            self._flush()
            self._head = None
        elif tag == "li":
            self._flush()
            self._li = max(0, self._li - 1)
        elif tag in self._BLOCK:
            self._flush()

    def handle_data(self, data):  # noqa: D102
        if not self._skip:
            self._buf.append(data)


def html_to_paragraphs(html: str) -> tuple[list[str], str]:
    p = _EpubHtml()
    p.feed(html)
    p._flush()
    return [normalize_text(x) for x in p.paras], normalize_text(p.first_heading)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve(base: str, href: str) -> str:
    href = unquote(href.split("#", 1)[0])
    return _norm_path(PurePosixPath(base) / href if base else PurePosixPath(href))


def _norm_path(p: PurePosixPath) -> str:
    parts: list[str] = []
    for part in p.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def parse_epub(path: Path) -> tuple[dict, list[Section], str]:
    try:
        zf = zipfile.ZipFile(str(path))
    except Exception as exc:  # noqa: BLE001
        raise BookError(f"cannot open epub: {str(exc)[:160]}") from exc
    with zf:
        try:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
            opf_path = next(e.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile")
            opf = ET.fromstring(zf.read(opf_path))
        except Exception as exc:  # noqa: BLE001
            raise BookError(f"cannot read epub package: {str(exc)[:160]}") from exc
        base = str(PurePosixPath(opf_path).parent)
        base = "" if base == "." else base
        manifest: dict[str, dict] = {}
        spine: list[str] = []
        title = author = ""
        toc_id = None
        for el in opf.iter():
            tag = _local(el.tag)
            if tag == "item":
                manifest[el.get("id", "")] = {
                    "href": el.get("href", ""),
                    "props": el.get("properties", ""),
                    "type": el.get("media-type", ""),
                }
            elif tag == "itemref":
                spine.append(el.get("idref", ""))
            elif tag == "spine":
                toc_id = el.get("toc")
            elif tag == "title" and not title:
                title = (el.text or "").strip()
            elif tag == "creator" and not author:
                author = (el.text or "").strip()
        if not spine:
            raise BookError("epub spine is empty")
        nav_titles: dict[str, str] = {}
        nav_item = next((m for m in manifest.values() if "nav" in m["props"].split()), None)
        ncx_item = manifest.get(toc_id or "") or next(
            (m for m in manifest.values() if m["type"] == "application/x-dtbncx+xml"), None
        )
        try:
            if nav_item:
                nav_titles = _read_nav(zf.read(_resolve(base, nav_item["href"])).decode("utf-8", "replace"), base,
                                       _resolve(base, nav_item["href"]))
            if not nav_titles and ncx_item:
                ncx_path = _resolve(base, ncx_item["href"])
                nav_titles = _read_ncx(zf.read(ncx_path), base, ncx_path)
        except KeyError:
            nav_titles = {}
        sections: list[Section] = []
        pending_title: str | None = None
        for idref in spine:
            item = manifest.get(idref)
            if not item or "html" not in item["type"]:
                continue
            doc_path = _resolve(base, item["href"])
            if nav_item and doc_path == _resolve(base, nav_item["href"]):
                continue
            try:
                html = zf.read(doc_path).decode("utf-8", "replace")
            except KeyError:
                continue
            paras, first = html_to_paragraphs(html)
            nav_t = nav_titles.get(doc_path)
            if not paras:
                if nav_t:
                    pending_title = nav_t  # an image-only chapter title page names the next section
                continue
            if nav_t is None and sections and nav_titles:
                sections[-1].paras.extend(paras)  # continuation file joins the unit before it
                continue
            sec_title = clean_title(nav_t or first or PurePosixPath(doc_path).stem)
            if pending_title:
                sec_title = f"{clean_title(pending_title)} — {sec_title}"
                pending_title = None
            if paras and paras[0].lstrip("# ").strip() == sec_title:
                paras = paras[1:]  # the title shows in the note heading
            sections.append(Section(sec_title, paras, href=doc_path))
    if not sections:
        raise BookError("epub has no readable text")
    return {"title": title, "author": author}, sections, "epub-spine"


def _read_nav(html: str, base: str, nav_path: str) -> dict[str, str]:
    """Map document path -> first nav title."""
    out: dict[str, str] = {}
    nav_dir = str(PurePosixPath(nav_path).parent)
    nav_dir = "" if nav_dir == "." else nav_dir

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.href = None
            self.txt: list[str] = []
            self.in_toc = 0

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "nav" and ("toc" in (a.get("epub:type") or a.get("type") or "") or not self.in_toc):
                self.in_toc += 1
            if tag == "a" and self.in_toc and a.get("href"):
                self.href, self.txt = a["href"], []

        def handle_endtag(self, tag):
            if tag == "a" and self.href is not None:
                key = _resolve(nav_dir, self.href)
                t = re.sub(r"\s+", " ", "".join(self.txt)).strip()
                if t and key and key not in out:
                    out[key] = t
                self.href = None
            if tag == "nav":
                self.in_toc = max(0, self.in_toc - 1)

        def handle_data(self, data):
            if self.href is not None:
                self.txt.append(data)

    P().feed(html)
    return out


def _read_ncx(data: bytes, base: str, ncx_path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    ncx_dir = str(PurePosixPath(ncx_path).parent)
    ncx_dir = "" if ncx_dir == "." else ncx_dir
    root = ET.fromstring(data)
    for pt in root.iter():
        if _local(pt.tag) != "navPoint":
            continue
        label = next((t.text for t in pt.iter() if _local(t.tag) == "text"), "") or ""
        content = next((c for c in pt if _local(c.tag) == "content"), None)
        if content is None:
            continue
        key = _resolve(ncx_dir, content.get("src", ""))
        if label.strip() and key not in out:
            out[key] = re.sub(r"\s+", " ", label).strip()
    return out


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def _meta_from_filename(path: Path) -> dict:
    stem = path.stem
    if " - " in stem:
        author, title = stem.split(" - ", 1)
        return {"title": title.strip(), "author": author.strip()}
    return {"title": stem.strip(), "author": ""}


def _usable_title(t: str) -> bool:
    t = (t or "").strip()
    return len(t) >= 4 and not t.lower().endswith((".pdf", ".epub", ".indd", ".doc", ".docx"))


def resolve_meta(path: Path, found: dict, overrides: dict) -> dict:
    fn = _meta_from_filename(path)
    ov = overrides.get(str(path)) or overrides.get(path.name) or {}
    title = ov.get("title") or (found.get("title", "").strip() if _usable_title(found.get("title", "")) else "") or fn["title"]
    author = ov.get("author") or found.get("author", "").strip() or fn["author"]
    return {"title": title, "author": author}


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #
def plan_units(sections: list[Section], max_words: int, min_words: int) -> tuple[list[Section], list[dict]]:
    """Drop front/back matter, merge tiny sections, return (kept, skipped records)."""
    skipped, kept = [], []
    for sec in sections:
        if is_skipped_title(sec.title):
            skipped.append({"title": sec.title, "words": sec.words, "reason": "front/back matter"})
        elif sec.words == 0:
            skipped.append({"title": sec.title, "words": 0, "reason": "no text"})
        else:
            kept.append(sec)
    merged: list[Section] = []
    i = 0
    while i < len(kept):
        cur = kept[i]
        if cur.words < min_words:
            nxt = kept[i + 1] if i + 1 < len(kept) else None
            if nxt is not None and cur.words + nxt.words <= max_words:
                kept[i + 1] = _merge(cur, nxt)
                i += 1
                continue
            prev = merged[-1] if merged else None
            if prev is not None and cur.words + prev.words <= max_words:
                merged[-1] = _merge(prev, cur)
                i += 1
                continue
        merged.append(cur)
        i += 1
    return merged, skipped


def _merge(a: Section, b: Section) -> Section:
    titles = [t for t in (a.title, b.title) if t]
    if a.title.endswith("…") or " … " in a.title:
        title = a.title.rsplit(" … ", 1)[0] + " … " + b.title
    elif a.title.count("; ") >= 1:
        title = a.title.split("; ", 1)[0] + " … " + b.title
    else:
        title = "; ".join(titles)
    pages = None
    if a.pages and b.pages:
        pages = (min(a.pages[0], b.pages[0]), max(a.pages[1], b.pages[1]))
    return Section(title, a.paras + b.paras, pages=pages, href=a.href or b.href)


def book_slug(meta: dict) -> str:
    return slugify(f"{meta['author']} {meta['title']}", 40) or "untitled"


def build_units(path: Path, meta: dict, sections: list[Section], method: str, max_words: int,
                min_words: int) -> tuple[list[dict], list[dict]]:
    kept, skipped = plan_units(sections, max_words, min_words)
    slug = book_slug(meta)
    units = []
    for ci, sec in enumerate(kept, start=1):
        parts = split_paragraphs(sec.paras, max_words)
        for pi, part in enumerate(parts, start=1):
            uid = f"book-{slug}-c{ci:02d}" + (f"-p{pi}" if len(parts) > 1 else "")
            title = sec.title + (f" (part {pi} of {len(parts)})" if len(parts) > 1 else "")
            unit = {
                "id": uid,
                "book_title": meta["title"],
                "book_author": meta["author"],
                "chapter_title": title,
                "chapter_index": ci,
                "split_method": method,
                "source_file": str(path),
                "paras": part,
                "word_count": sum(len(p.split()) for p in part),
            }
            if len(parts) > 1:
                unit["part"] = pi
                unit["part_count"] = len(parts)
            if sec.pages:
                unit["pages"] = f"{sec.pages[0]}-{sec.pages[1]}"
            if sec.href:
                unit["epub_href"] = sec.href
            units.append(unit)
    return units, skipped


def render_note(unit: dict, extracted_at: str) -> str:
    keys = ["id", "kind", "source_type", "book_title", "book_author", "chapter_title",
            "chapter_index", "part", "part_count", "pages", "epub_href", "source_file",
            "split_method", "word_count", "extracted_at"]
    fm = dict(unit, kind="article", source_type="book", extracted_at=extracted_at)
    head = "\n".join(f"{k}: {json.dumps(fm[k], ensure_ascii=False)}" for k in keys if k in fm)
    body = "\n\n".join(unit["paras"])
    return f"---\n{head}\n---\n\n# {unit['book_title']} — {unit['chapter_title']}\n\n{body}\n"


def queue_item(unit: dict, staging: Path) -> dict:
    return {
        "id": unit["id"],
        "url": "file://" + unit["source_file"],
        "kind": "article",
        "stage": "extracted",
        "lit_note": str(staging / "lit" / f"{unit['id']}.md"),
        "cluster": None,
        "notes_emitted": [],
        "attempts": 0,
        "error": None,
        "title": f"{unit['book_title']} — {unit['chapter_title']}",
        "source_type": "book",
        "book_title": unit["book_title"],
        "book_author": unit["book_author"],
        "chapter_title": unit["chapter_title"],
        "chapter_index": unit["chapter_index"],
        "split_method": unit["split_method"],
        "source_file": unit["source_file"],
        "word_count": unit["word_count"],
        **({"part": unit["part"]} if "part" in unit else {}),
    }


def failed_item(path: Path, meta: dict, reason: str) -> dict:
    return {
        "id": f"book-{book_slug(meta)}",
        "url": "file://" + str(path),
        "kind": "article",
        "stage": "failed",
        "lit_note": None,
        "cluster": None,
        "notes_emitted": [],
        "attempts": 1,
        "error": reason,
        "title": meta["title"],
        "source_type": "book",
        "book_title": meta["title"],
        "book_author": meta["author"],
        "source_file": str(path),
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def ingest(staging: Path, files: list[Path], max_words: int, min_words: int, overrides: dict) -> dict:
    staging.mkdir(parents=True, exist_ok=True)
    qpath = staging / "queue.json"
    summary: dict = {"books": [], "failures": [], "skipped_sections": []}
    for path in files:
        path = Path(path).resolve()
        with state_lock(staging):
            q = json.loads(qpath.read_text()) if qpath.exists() else {"version": 1, "items": []}
        existing = [it for it in q["items"] if it.get("source_file") == str(path) and it["stage"] != "failed"]
        if existing:
            summary["books"].append({
                "file": str(path), "book_title": existing[0].get("book_title"), "status": "already ingested",
                "units": len(existing), "words": sum(it.get("word_count", 0) for it in existing),
            })
            continue
        fallback = resolve_meta(path, {}, overrides)
        try:
            ext = path.suffix.lower()
            if ext == ".pdf":
                found, sections, method = parse_pdf(path, max_words)
            elif ext == ".epub":
                found, sections, method = parse_epub(path)
            else:
                raise BookError(f"unsupported file type: {ext or 'none'}")
            meta = resolve_meta(path, found, overrides)
            units, skipped = build_units(path, meta, sections, method, max_words, min_words)
            if not units:
                raise BookError("no usable text after skipping front/back matter")
        except BookError as exc:
            item = failed_item(path, fallback, str(exc))
            _upsert(staging, qpath, [item], replace_failed_for=str(path))
            summary["failures"].append({"file": str(path), "reason": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 - never drop a book silently
            item = failed_item(path, fallback, f"unexpected error: {str(exc)[:160]}")
            _upsert(staging, qpath, [item], replace_failed_for=str(path))
            summary["failures"].append({"file": str(path), "reason": item["error"]})
            continue
        stamp = _now()
        for u in units:
            _atomic_write(staging / "lit" / f"{u['id']}.md", render_note(u, stamp))
        _upsert(staging, qpath, [queue_item(u, staging) for u in units], replace_failed_for=str(path))
        summary["books"].append({
            "file": str(path), "book_title": meta["title"], "book_author": meta["author"],
            "status": "ingested", "split_method": method, "units": len(units),
            "words": sum(u["word_count"] for u in units),
        })
        for s in skipped:
            summary["skipped_sections"].append({"book": meta["title"], **s})
    return summary


def _upsert(staging: Path, qpath: Path, items: list[dict], replace_failed_for: str) -> None:
    with state_lock(staging):
        q = json.loads(qpath.read_text()) if qpath.exists() else {"version": 1, "items": []}
        q["items"] = [
            it for it in q["items"]
            if not (it.get("source_file") == replace_failed_for and it["stage"] == "failed")
        ]
        have = {it["id"] for it in q["items"]}
        q["items"].extend(it for it in items if it["id"] not in have)
        _atomic_write(qpath, json.dumps(q, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Split PDF and EPUB books into staged lit notes")
    ap.add_argument("--staging", required=True, help="staging folder (lit/ and queue.json go here)")
    ap.add_argument("books", nargs="+", help="book files (.pdf, .epub)")
    ap.add_argument("--max-words", type=int, default=DEFAULT_MAX_WORDS)
    ap.add_argument("--min-words", type=int, default=DEFAULT_MIN_WORDS)
    ap.add_argument("--meta", help="JSON file: book path -> {title, author}")
    args = ap.parse_args(argv)
    overrides = json.loads(Path(args.meta).read_text()) if args.meta else {}
    overrides = {str(Path(k).resolve()) if "/" in k else k: v for k, v in overrides.items()}
    summary = ingest(Path(args.staging).resolve(), [Path(b) for b in args.books], args.max_words,
                     args.min_words, overrides)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
