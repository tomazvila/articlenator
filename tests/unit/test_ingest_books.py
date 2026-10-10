"""Tests for the book Phase-A adapter (zettel_ralph/ingest_books.py).

Fixture PDFs and EPUBs are built in tmp dirs at test time. No network, no real books.
"""

from __future__ import annotations

import json
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

ZR = Path(__file__).resolve().parents[2] / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

import ingest_books as ib  # noqa: E402

# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def prose(n_words: int, seed: str = "alpha") -> list[str]:
    """Return wrapped text lines of about n_words words, in paragraphs of 60 words."""
    words = [f"{seed}{i % 17}" for i in range(n_words)]
    lines: list[str] = []
    for i in range(0, n_words, 60):
        chunk = " ".join(words[i : i + 60]) + "."
        chunk = chunk[0].upper() + chunk[1:]
        lines.extend(textwrap.wrap(chunk, 70))
    return lines


def make_pdf(path: Path, pages: list[list[str]], outline=None, title=None, author=None) -> Path:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    for lines in pages:
        page = w.add_blank_page(612, 792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})}
        )
        ops = ["BT", "/F1 9 Tf", "11 TL", "40 760 Td"]
        for ln in lines:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) '")
        ops.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(ops).encode("latin-1", "replace"))
        page[NameObject("/Contents")] = w._add_object(stream)
    for item in outline or []:
        t, pg = item[0], item[1]
        level = item[2] if len(item) > 2 else 0
        parent = None
        if level == 1:
            parent = w._last_top  # type: ignore[attr-defined]
        ref = w.add_outline_item(t, pg, parent=parent)
        if level == 0:
            w._last_top = ref  # type: ignore[attr-defined]
    meta = {}
    if title:
        meta["/Title"] = title
    if author:
        meta["/Author"] = author
    if meta:
        w.add_metadata(meta)
    with open(path, "wb") as f:
        w.write(f)
    return path


def chapter_pages(n_pages: int, words_per_page: int, label: str, header: str | None = None):
    """Pages with running header, page number footer, and prose."""
    pages = []
    for i in range(n_pages):
        body = prose(words_per_page, seed=label)
        top = [header] if header else []
        pages.append(top + body + [str(len(pages) + i + 1)])
    return pages


def make_epub(path: Path, chapters, *, title="Epub Book", author="Epub Author", nav="nav", extra_files=None):
    """chapters: list of (filename, nav_title_or_None, html_body). Spine follows list order."""
    manifest, spine, nav_li, ncx_pts = [], [], [], []
    for i, (fn, t, _) in enumerate(chapters):
        manifest.append(f'<item id="c{i}" href="text/{fn}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
        if t:
            nav_li.append(f'<li><a href="text/{fn}">{t}</a></li>')
            ncx_pts.append(
                f'<navPoint id="n{i}"><navLabel><text>{t}</text></navLabel><content src="text/{fn}"/></navPoint>'
            )
    nav_doc = (
        '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
        + "".join(nav_li)
        + "</ol></nav></body></html>"
    )
    ncx = (
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>' + "".join(ncx_pts) + "</navMap></ncx>"
    )
    extra = ""
    if nav == "nav":
        extra = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    elif nav == "ncx":
        extra = '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata>"
        f"<manifest>{extra}{''.join(manifest)}</manifest>"
        f'<spine{" toc=\"ncx\"" if nav == "ncx" else ""}>{"".join(spine)}</spine></package>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        z.writestr("OEBPS/content.opf", opf)
        if nav == "nav":
            z.writestr("OEBPS/nav.xhtml", nav_doc)
        elif nav == "ncx":
            z.writestr("OEBPS/toc.ncx", ncx)
        for fn, _, body in chapters:
            z.writestr(f"OEBPS/text/{fn}", f"<html><head><title>x</title></head><body>{body}</body></html>")
    return path


def html_paras(n_words: int, seed: str) -> str:
    words = [f"{seed}{i % 13}" for i in range(n_words)]
    return "".join("<p>" + " ".join(words[i : i + 50]) + ".</p>" for i in range(0, n_words, 50))


def run(staging: Path, files, **kw):
    overrides = kw.pop("overrides", {})
    return ib.ingest(
        staging,
        [Path(f) for f in files],
        kw.get("max_words", 7000),
        kw.get("min_words", 800),
        overrides,
    )


def queue(staging: Path) -> dict:
    return json.loads((staging / "queue.json").read_text())


def read_note(staging: Path, uid: str):
    text = (staging / "lit" / f"{uid}.md").read_text()
    _, fm, body = text.split("---\n", 2)
    meta = {}
    for ln in fm.strip().splitlines():
        k, v = ln.split(": ", 1)
        meta[k] = json.loads(v)
    return meta, body


@pytest.fixture
def stg(tmp_path):
    return tmp_path / "staging"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_hyphenation_join_and_paragraphs():
    lines = [
        "The quick brown fox jumps over the lazy dog and keeps run-",
        "ning through the forest until it reaches the end of the long",
        "trail near the river bank.",
        "A second paragraph starts here with enough words to fill the line width fully",
        "and ends short.",
    ]
    paras = ib.lines_to_paragraphs(lines)
    assert len(paras) == 2
    assert "running through" in paras[0]
    assert "run- ning" not in paras[0]
    assert paras[0].endswith("river bank.")


def test_short_first_line_with_lowercase_continuation_stays_joined():
    lines = [
        "When designing a website, many designers begin with the home page",
        "template, a logical starting point because the home page can establish so much of the",
        "aesthetic tone of the whole site.",
        "Next paragraph begins here.",
    ]
    paras = ib.lines_to_paragraphs(lines)
    assert len(paras) == 2
    assert paras[0].startswith("When designing a website, many designers begin with the home page template,")


def test_real_hyphen_before_uppercase_is_kept():
    paras = ib.lines_to_paragraphs(["The state of the art is the well-", "Known approach to the whole thing."])
    assert "well- Known" in paras[0] or "well-Known" in paras[0]


def test_skip_title_matching():
    for t in ["Copyright", "Table of Contents", "Index", "Acknowledgments", "About the Author", "Bibliography",
              "cover.pdf", "contentsCh1.pdf", "K"]:
        assert ib.is_skipped_title(t), t
    for t in ["Chapter 1", "Introduction", "Indexing Basics", "Notes on Design"]:
        assert not ib.is_skipped_title(t), t


def test_clean_title_filename_style():
    assert ib.clean_title("ch2.pdf") == "Chapter 2"
    assert ib.clean_title("Cover Page\r") == "Cover Page"


def test_split_paragraphs_prefers_heading_boundary():
    paras = []
    for i in range(10):
        paras.append(" ".join(["w"] * 100) + ".")
    paras.insert(6, "A Short Heading")
    parts = ib.split_paragraphs(paras, 600)
    assert len(parts) == 2
    assert parts[1][0] == "A Short Heading"


def test_split_paragraphs_small_and_single():
    assert ib.split_paragraphs(["only one paragraph"], 1) == [["only one paragraph"]]
    assert ib.split_paragraphs(["a b", "c d"], 100) == [["a b", "c d"]]


def test_repeated_header_and_page_number_removed():
    pages = [["RUNNING HEAD", f"{'abcdef'[i]}-unique {'ghijkl'[i]}-words", "more text here.", str(i + 10)] for i in range(6)]
    rep = ib.detect_repeated_lines(pages)
    out = ib.strip_page_furniture(pages[0], rep)
    assert out == ["a-unique g-words", "more text here."]


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def test_pdf_split_by_outline(tmp_path, stg):
    pages = chapter_pages(2, 600, "a") + chapter_pages(2, 600, "b") + chapter_pages(2, 600, "c")
    f = make_pdf(
        tmp_path / "Doe - Fixture Book.pdf",
        pages,
        outline=[("Chapter One", 0), ("Chapter Two", 2), ("Chapter Three", 4)],
    )
    s = run(stg, [f])
    assert s["books"][0]["units"] == 3
    items = queue(stg)["items"]
    assert [i["chapter_title"] for i in items] == ["Chapter One", "Chapter Two", "Chapter Three"]
    meta, body = read_note(stg, items[1]["id"])
    assert meta["pages"] == "3-4"
    assert meta["split_method"] == "outline"
    assert meta["kind"] == "article" and meta["source_type"] == "book"
    assert meta["source_file"] == str(f.resolve())
    assert body.strip().startswith("# Fixture Book — Chapter Two")
    assert "b0" in body and "a0" not in body


def test_pdf_page_window_fallback(tmp_path, stg):
    pages = chapter_pages(8, 500, "w")
    f = make_pdf(tmp_path / "Doe - No Outline.pdf", pages)
    s = run(stg, [f], max_words=1500)
    assert s["books"][0]["split_method"] == "page-window"
    items = queue(stg)["items"]
    assert len(items) >= 3
    meta, _ = read_note(stg, items[0]["id"])
    assert meta["split_method"] == "page-window"
    assert meta["pages"].startswith("1-")
    assert all(i["word_count"] <= 1700 for i in items)


def test_pdf_long_chapter_splits_into_parts(tmp_path, stg):
    pages = chapter_pages(6, 900, "long")  # chapter 3: five pages, 4500 words
    f = make_pdf(tmp_path / "Doe - Long Chapter.pdf", pages, outline=[("Chapter 3", 0), ("Chapter 4", 5)])
    run(stg, [f], max_words=2000)
    ids = [i["id"] for i in queue(stg)["items"]]
    assert any(i.endswith("-c01-p1") for i in ids) and any(i.endswith("-c01-p3") for i in ids)
    assert all(i["word_count"] <= 2400 for i in queue(stg)["items"] if i["chapter_index"] == 1)
    meta, _ = read_note(stg, [i for i in ids if i.endswith("-c01-p2")][0])
    assert meta["part"] == 2 and meta["part_count"] == 3
    assert meta["chapter_title"] == "Chapter 3 (part 2 of 3)"


def test_pdf_front_back_matter_skipped_and_tiny_merged(tmp_path, stg):
    pages = (
        [["Copyright 2020 All rights reserved."] + prose(100, "cr")]
        + [["Contents"] + prose(100, "toc")]
        + chapter_pages(2, 600, "one")
        + chapter_pages(2, 600, "two")
        + [prose(60, "tiny")]
        + [["Index"] + prose(300, "idx")]
    )
    f = make_pdf(
        tmp_path / "Doe - Matter.pdf",
        pages,
        outline=[("Copyright", 0), ("Table of Contents", 1), ("Chapter 1", 2), ("Chapter 2", 4), ("Epilogue", 6),
                 ("Index", 7)],
    )
    s = run(stg, [f])
    skipped = {x["title"] for x in s["skipped_sections"]}
    assert skipped == {"Copyright", "Table of Contents", "Index"}
    items = queue(stg)["items"]
    assert len(items) == 2
    assert "Epilogue" in items[1]["chapter_title"]  # tiny section joined the neighbor
    for it in items:
        assert "idx" not in (stg / "lit" / f"{it['id']}.md").read_text()


def test_pdf_nested_outline_uses_depth_that_fits(tmp_path, stg):
    pages = chapter_pages(8, 400, "n")
    outline = [("Part A", 0, 0), ("Item 1", 0, 1), ("Item 2", 2, 1), ("Part B", 4, 0), ("Item 3", 4, 1),
               ("Item 4", 6, 1)]
    f = make_pdf(tmp_path / "Doe - Nested.pdf", pages, outline=outline)
    run(stg, [f], max_words=800, min_words=200)
    titles = [i["chapter_title"] for i in queue(stg)["items"]]
    assert any("Item" in t for t in titles)


def test_pdf_running_header_and_page_numbers_dropped(tmp_path, stg):
    pages = chapter_pages(8, 400, "h", header="MY RUNNING HEADER")
    f = make_pdf(tmp_path / "Doe - Headers.pdf", pages, outline=[("One", 0), ("Two", 4), ("Three", 6)])
    run(stg, [f])
    for it in queue(stg)["items"]:
        text = (stg / "lit" / f"{it['id']}.md").read_text()
        assert "MY RUNNING HEADER" not in text


def test_pdf_scanned_without_text_is_failed(tmp_path, stg):
    f = make_pdf(tmp_path / "Doe - Scan.pdf", [[] for _ in range(5)])
    s = run(stg, [f])
    assert s["failures"] and "no text layer" in s["failures"][0]["reason"]
    it = queue(stg)["items"][0]
    assert it["stage"] == "failed" and "no text layer" in it["error"]


def test_corrupt_file_gives_failed_item(tmp_path, stg):
    bad = tmp_path / "Doe - Broken.pdf"
    bad.write_bytes(b"not a pdf at all")
    badepub = tmp_path / "Doe - Broken Epub.epub"
    badepub.write_bytes(b"not a zip")
    s = run(stg, [bad, badepub])
    assert len(s["failures"]) == 2
    items = queue(stg)["items"]
    assert {i["stage"] for i in items} == {"failed"}
    assert all(i["error"] for i in items)
    assert not list((stg / "lit").glob("*.md")) if (stg / "lit").exists() else True


def test_unsupported_extension_failed(tmp_path, stg):
    f = tmp_path / "Doe - Thing.txt"
    f.write_text("hello")
    s = run(stg, [f])
    assert "unsupported" in s["failures"][0]["reason"]


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #


def test_epub_spine_order_and_nav_titles(tmp_path, stg):
    chapters = [
        ("b.xhtml", "Second In Spine", f"<h1>Heading B</h1>{html_paras(900, 'bb')}"),
        ("a.xhtml", "First Nav Title", f"<h1>Heading A</h1>{html_paras(900, 'aa')}"),
    ]
    f = make_epub(tmp_path / "x.epub", chapters)
    run(stg, [f])
    items = queue(stg)["items"]
    assert [i["chapter_title"] for i in items] == ["Second In Spine", "First Nav Title"]
    meta, body = read_note(stg, items[0]["id"])
    assert meta["epub_href"] == "OEBPS/text/b.xhtml"
    assert meta["book_title"] == "Epub Book" and meta["book_author"] == "Epub Author"
    assert "bb0" in body and "aa0" not in body


def test_epub_ncx_titles(tmp_path, stg):
    chapters = [("c1.xhtml", "NCX One", html_paras(900, "n1")), ("c2.xhtml", "NCX Two", html_paras(900, "n2"))]
    f = make_epub(tmp_path / "n.epub", chapters, nav="ncx")
    run(stg, [f])
    assert [i["chapter_title"] for i in queue(stg)["items"]] == ["NCX One", "NCX Two"]


def test_epub_without_nav_uses_heading(tmp_path, stg):
    chapters = [("c1.xhtml", None, f"<h2>Heading One</h2>{html_paras(900, 'h1')}")]
    f = make_epub(tmp_path / "h.epub", chapters, nav=None)
    run(stg, [f])
    assert queue(stg)["items"][0]["chapter_title"] == "Heading One"


def test_epub_continuation_file_joins_previous(tmp_path, stg):
    chapters = [
        ("c1.xhtml", "Chapter One", html_paras(900, "p1")),
        ("c1b.xhtml", None, html_paras(900, "p1b")),
        ("c2.xhtml", "Chapter Two", html_paras(900, "p2")),
    ]
    f = make_epub(tmp_path / "j.epub", chapters)
    run(stg, [f])
    items = queue(stg)["items"]
    assert len(items) == 2
    assert items[0]["word_count"] >= 1800


def test_epub_image_only_title_page_names_next_section(tmp_path, stg):
    chapters = [
        ("t1.xhtml", "3 Action", "<img src='x.png'/>"),
        ("c1.xhtml", "Action Versus Inaction", f"<h2>Inner</h2>{html_paras(900, 'tp')}"),
    ]
    f = make_epub(tmp_path / "tp.epub", chapters)
    run(stg, [f])
    items = queue(stg)["items"]
    assert len(items) == 1
    assert items[0]["chapter_title"] == "3 Action — Action Versus Inaction"


def test_epub_skips_front_matter_by_nav_title(tmp_path, stg):
    chapters = [
        ("cp.xhtml", "Copyright", html_paras(100, "cp")),
        ("c1.xhtml", "Chapter One", html_paras(900, "k1")),
        ("ab.xhtml", "About the Author", html_paras(100, "ab")),
    ]
    f = make_epub(tmp_path / "s.epub", chapters)
    s = run(stg, [f])
    assert {x["title"] for x in s["skipped_sections"]} == {"Copyright", "About the Author"}
    assert len(queue(stg)["items"]) == 1


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


def test_metadata_fallback_to_filename(tmp_path, stg):
    f = make_pdf(tmp_path / "Jane Roe - A Book Title.pdf", chapter_pages(3, 500, "m"))
    run(stg, [f])
    it = queue(stg)["items"][0]
    assert it["book_author"] == "Jane Roe" and it["book_title"] == "A Book Title"


def test_metadata_from_pdf_info(tmp_path, stg):
    f = make_pdf(tmp_path / "Zed - Filename Title.pdf", chapter_pages(3, 500, "m"), title="Info Title",
                 author="Info Author")
    run(stg, [f])
    it = queue(stg)["items"][0]
    assert it["book_title"] == "Info Title" and it["book_author"] == "Info Author"


def test_metadata_junk_pdf_title_ignored(tmp_path, stg):
    f = make_pdf(tmp_path / "Zed - Real Title.pdf", chapter_pages(3, 500, "m"), title="0321713729.pdf")
    run(stg, [f])
    assert queue(stg)["items"][0]["book_title"] == "Real Title"


def test_meta_override_wins(tmp_path, stg):
    f = make_pdf(tmp_path / "Zed - Real Title.pdf", chapter_pages(3, 500, "m"), title="Info Title")
    run(stg, [f], overrides={str(f.resolve()): {"title": "Override Title", "author": "Over Ride"}})
    it = queue(stg)["items"][0]
    assert it["book_title"] == "Override Title" and it["book_author"] == "Over Ride"
    assert it["id"].startswith("book-over-ride-override-title-c")


def test_cli_meta_file(tmp_path, stg, capsys):
    f = make_pdf(tmp_path / "Zed - Real Title.pdf", chapter_pages(3, 500, "m"))
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({str(f): {"title": "CLI Title", "author": "Cli Auth"}}))
    assert ib.main(["--staging", str(stg), str(f), "--meta", str(meta)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["books"][0]["book_title"] == "CLI Title"


# --------------------------------------------------------------------------- #
# Ids, idempotence, queue shape
# --------------------------------------------------------------------------- #


def test_id_stability_across_runs_and_staging(tmp_path):
    f = make_pdf(tmp_path / "Doe - Stable.pdf", chapter_pages(6, 600, "s"),
                 outline=[("One", 0), ("Two", 3)])
    a, b = tmp_path / "a", tmp_path / "b"
    run(a, [f])
    run(b, [f])
    ids_a = [i["id"] for i in queue(a)["items"]]
    assert ids_a == [i["id"] for i in queue(b)["items"]]
    assert ids_a[0] == "book-doe-stable-c01"


def test_id_slug_limited_to_40_chars(tmp_path, stg):
    f = make_pdf(tmp_path / ("Author Name - " + "Very Long Title " * 8 + ".pdf"), chapter_pages(3, 500, "l"))
    run(stg, [f])
    uid = queue(stg)["items"][0]["id"]
    slug = uid[len("book-") : uid.rindex("-c")]
    assert 0 < len(slug) <= 40


def test_rerun_idempotent_and_keeps_stage(tmp_path, stg):
    f = make_pdf(tmp_path / "Doe - Idem.pdf", chapter_pages(6, 600, "i"), outline=[("One", 0), ("Two", 3)])
    run(stg, [f])
    q = queue(stg)
    q["items"][0]["stage"] = "synthesized"
    q["items"][0]["notes_emitted"] = ["Some Note"]
    (stg / "queue.json").write_text(json.dumps(q))
    lit = stg / "lit" / f"{q['items'][0]['id']}.md"
    before_lit = lit.read_text()
    before_q = (stg / "queue.json").read_text()
    s = run(stg, [f])
    assert (stg / "queue.json").read_text() == before_q
    assert lit.read_text() == before_lit
    assert s["books"][0]["status"] == "already ingested"
    assert queue(stg)["items"][0]["stage"] == "synthesized"


def test_failed_item_replaced_when_book_becomes_valid(tmp_path, stg):
    f = tmp_path / "Doe - Later.pdf"
    f.write_bytes(b"junk")
    run(stg, [f])
    assert queue(stg)["items"][0]["stage"] == "failed"
    make_pdf(f, chapter_pages(3, 500, "ok"))
    run(stg, [f])
    stages = {i["stage"] for i in queue(stg)["items"]}
    assert stages == {"extracted"}


def test_queue_item_shape_matches_ingest(tmp_path, stg):
    f = make_pdf(tmp_path / "Doe - Shape.pdf", chapter_pages(3, 500, "q"))
    run(stg, [f])
    it = queue(stg)["items"][0]
    for k in ("id", "url", "kind", "stage", "lit_note", "cluster", "notes_emitted", "attempts", "error", "title"):
        assert k in it
    assert it["kind"] == "article" and it["stage"] == "extracted"
    assert Path(it["lit_note"]).exists()
    assert queue(stg)["version"] == 1


def test_queue_claim_and_mark_work_with_items(tmp_path, stg, monkeypatch):
    import importlib

    f = make_pdf(tmp_path / "Doe - Claim.pdf", chapter_pages(3, 500, "q"))
    run(stg, [f])
    monkeypatch.setenv("ZR_STAGING", str(stg))
    import _lock
    import queue_claim
    import queue_mark

    importlib.reload(_lock)
    importlib.reload(queue_claim)
    importlib.reload(queue_mark)
    assert queue_claim.Q == stg / "queue.json"
    uid = queue(stg)["items"][0]["id"]
    monkeypatch.setattr(sys, "argv", ["queue_mark.py", "--ids", uid, "--stage", "skipped", "--reason", "test"])
    queue_mark.main()
    assert queue(stg)["items"][0]["stage"] == "skipped"


def test_two_books_summary_counts(tmp_path, stg):
    a = make_pdf(tmp_path / "A - One.pdf", chapter_pages(3, 500, "a"))
    b = make_epub(tmp_path / "B - Two.epub", [("c.xhtml", "Chap", html_paras(900, "e"))])
    s = run(stg, [a, b])
    assert [x["status"] for x in s["books"]] == ["ingested", "ingested"]
    assert all(x["units"] >= 1 and x["words"] > 0 for x in s["books"])
