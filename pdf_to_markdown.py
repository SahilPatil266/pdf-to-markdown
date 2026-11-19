#!/usr/bin/env python3
"""
pdf_to_markdown.py - Deterministically convert the text of a PDF into clean, readable Markdown.

No AI involved: the same PDF always gives the same Markdown. Images are ignored.

What it handles
  * Headings         font size relative to body text (#, ##, ###); short bold lines -> ####
  * Paragraphs       wrapped lines joined, hyphenated line breaks repaired
  * Bold / italic    preserved inline
  * Lists            bullets (•, -, ▪ ...) and numbering (1. / 1) / a) / i.)
  * Ruled tables     tables drawn with lines (PyMuPDF), including rows that are not separated by rules
  * Borderless tables  rows of text aligned in 3+ columns (invoices, statements, reports)
  * Key/value pairs  "Label ..... value" rows (totals, invoice details, form fields) -> bullet list
  * Short-line blocks  addresses / contact blocks keep their line breaks
  * Code             monospace text -> fenced code blocks
  * Headers/footers  repeated top/bottom lines and page numbers removed
  * Two-column pages read column by column

Usage
    python pdf_to_markdown.py input.pdf                 # writes input.md next to the PDF
    python pdf_to_markdown.py input.pdf -o out.md
    python pdf_to_markdown.py input.pdf --pages 1-5,8
    python pdf_to_markdown.py input.pdf --page-breaks   # marker between pages
    python pdf_to_markdown.py input.pdf --no-tables     # treat everything as text
    python pdf_to_markdown.py folder_of_pdfs/ -o md_out/

Requires:  pip install pymupdf
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pymupdf  # PyMuPDF >= 1.24
except ImportError:
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:
        sys.exit("PyMuPDF is required:  pip install pymupdf")


# --------------------------------------------------------------------------- #
# Patterns and constants
# --------------------------------------------------------------------------- #
BULLET_CHARS = "•●○◦▪▫■□►▸‣⁃∙·\\-–—*✓✔➢➤→"
BULLET_RE = re.compile(rf"^\s*([{BULLET_CHARS}])\s+(.*)$")
NUMBERED_RE = re.compile(r"^\s*((?:\d{1,3}|[a-zA-Z]|[ivxlcdm]{1,5})[.)])\s+(.*)$", re.I)
MARKER_ONLY_RE = re.compile(rf"^\s*(?:([{BULLET_CHARS}])|(\d{{1,3}}|[a-zA-Z]|[ivxlcdm]{{1,5}})[.)]?)\s*$", re.I)
PAGE_NUM_RE = re.compile(r"^\s*(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?\s*$", re.I)
MONO_FONT_RE = re.compile(r"mono|courier|consol|menlo|code|inconsolata|source\s?code", re.I)
VALUE_RE = re.compile(
    r"^\s*(?:[$£€₹¥]|rs\.?|inr|usd|gbp|eur)?\s*[-+(]?\s*[\d.,:/%\- ]*\d[\d.,:/%\- )]*\s*"
    r"(?:[$£€₹¥%]|[a-z]{1,4})?\s*$"            # money, numbers, percentages, numeric dates
    r"|^\s*\S+@\S+\.\S+\s*$"                    # e-mail
    r"|^\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4}\s*$"
    r"|^\s*\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{2,4}\s*$"
    r"|^\s*[A-Z]{2,}[-/]?\d[\w\-/]*\s*$",       # IDs like INV-3337
    re.I,
)

FLAG_ITALIC, FLAG_MONO, FLAG_BOLD = 2, 8, 16


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class Frag:
    """A run of text on one baseline with no large horizontal gap inside it (= one cell)."""
    text: str      # markdown (inline bold/italic)
    plain: str
    size: float
    bold: bool
    italic: bool
    mono: bool
    x0: float
    y0: float
    x1: float
    y1: float
    bid: int = 0   # source block id
    base: float = 0.0  # baseline y


@dataclass
class Line:
    cells: list[Frag]
    page: int
    page_width: float

    @property
    def x0(self): return self.cells[0].x0
    @property
    def x1(self): return self.cells[-1].x1
    @property
    def y0(self): return min(c.y0 for c in self.cells)
    @property
    def y1(self): return max(c.y1 for c in self.cells)
    @property
    def text(self): return " ".join(c.text for c in self.cells)
    @property
    def plain(self): return " ".join(c.plain for c in self.cells)
    @property
    def size(self): return max(self.cells, key=lambda c: len(c.plain)).size
    @property
    def bold(self): return all(c.bold for c in self.cells)
    @property
    def mono(self): return all(c.mono for c in self.cells)
    @property
    def multi(self): return len(self.cells) >= 2


@dataclass
class Block:
    kind: str                       # "text" | "table"
    lines: list[Line] = field(default_factory=list)
    table_md: str = ""
    y0: float = 0.0
    x0: float = 0.0
    page: int = 0


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
_REPL = {" ": " ", " ": " ", " ": " ", " ": " ", "​": "", "\t": " ",
         "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "­": ""}


def clean(text: str) -> str:
    for k, v in _REPL.items():
        text = text.replace(k, v)
    return text


def escape_md(text: str) -> str:
    return re.sub(r"([\\`*_|])", r"\\\1", text)


def span_bold(s): f = s.get("font", "").lower(); return bool(s["flags"] & FLAG_BOLD) or any(w in f for w in ("bold", "black", "heavy", "semibold"))
def span_italic(s): f = s.get("font", "").lower(); return bool(s["flags"] & FLAG_ITALIC) or "italic" in f or "oblique" in f
def span_mono(s): return bool(s["flags"] & FLAG_MONO) or bool(MONO_FONT_RE.search(s.get("font", "")))


def parse_page_ranges(spec: str, n: int) -> list[int]:
    pages: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a) if a else 1, (int(b) if b else n) + 1))
        else:
            pages.add(int(part))
    return sorted(p - 1 for p in pages if 1 <= p <= n)


def col_gap(size: float) -> float:
    """Horizontal gap that separates two cells/columns rather than two words."""
    return max(1.5 * size, 9)


def inside(x: float, y: float, r: tuple) -> bool:
    return r[0] - 1 <= x <= r[2] + 1 and r[1] - 1 <= y <= r[3] + 1


# --------------------------------------------------------------------------- #
# Fragments: spans -> cells
# --------------------------------------------------------------------------- #
def make_frag(spans: list[dict], bid: int) -> Frag | None:
    pieces, sizes = [], Counter()
    n_bold = n_ital = n_mono = total = 0
    for s in spans:
        t = clean(s["text"])
        if not t:
            continue
        b, i, m = span_bold(s), span_italic(s), span_mono(s)
        n = len(t.strip())
        total += n
        n_bold += n * b; n_ital += n * i; n_mono += n * m
        sizes[round(s["size"], 1)] += n
        pieces.append((t, b, i))
    plain = re.sub(r"\s+", " ", "".join(p[0] for p in pieces)).strip()
    if not plain or not total:
        return None
    all_b, all_i, all_m = n_bold / total > 0.9, n_ital / total > 0.9, n_mono / total > 0.9
    size = sizes.most_common(1)[0][0]
    x0 = min(s["bbox"][0] for s in spans); y0 = min(s["bbox"][1] for s in spans)
    x1 = max(s["bbox"][2] for s in spans); y1 = max(s["bbox"][3] for s in spans)

    if all_m or (all_b and all_i):
        text = escape_md(plain) if not all_m else plain
    else:
        merged: list[list] = []
        for t, b, i in pieces:
            b = b and not all_b
            i = i and not all_i
            if merged and merged[-1][1] == b and merged[-1][2] == i:
                merged[-1][0] += t
            else:
                merged.append([t, b, i])
        out = []
        for t, b, i in merged:
            core = escape_md(t.strip())
            if not core:
                out.append(" ")
                continue
            mark = "***" if b and i else "**" if b else "*" if i else ""
            out.append((" " if t[:1].isspace() else "") + f"{mark}{core}{mark}" + (" " if t[-1:].isspace() else ""))
        text = re.sub(r"\s+", " ", "".join(out)).strip()
        text = re.sub(r"\*\*\s+\*\*", " ", text)
    base = Counter(round(s["origin"][1], 1) for s in spans).most_common(1)[0][0] if "origin" in spans[0] else y1
    return Frag(text, plain, size, all_b, all_i, all_m, x0, y0, x1, y1, bid, base)


def split_line_into_frags(ln: dict, bid: int) -> list[Frag]:
    """Split a PyMuPDF line wherever there is a wide horizontal gap (column / tab stop)."""
    spans = [s for s in ln.get("spans", []) if s["text"].strip()]
    if not spans:
        return []
    groups, cur = [], [spans[0]]
    for s in spans[1:]:
        gap = s["bbox"][0] - cur[-1]["bbox"][2]
        if gap > col_gap(s["size"]):
            groups.append(cur); cur = [s]
        else:
            cur.append(s)
    groups.append(cur)
    return [f for f in (make_frag(g, bid) for g in groups) if f]


# --------------------------------------------------------------------------- #
# Page extraction
# --------------------------------------------------------------------------- #
def extract_page(page, page_no: int, use_tables: bool) -> list[Block]:
    pw = page.rect.width
    blocks: list[Block] = []
    table_rects: list[tuple] = []

    # 1) tables that are drawn with ruling lines
    if use_tables and hasattr(page, "find_tables"):
        try:
            found = page.find_tables(strategy="lines_strict").tables
        except Exception:
            try:
                found = page.find_tables().tables
            except Exception:
                found = []
        verticals = vertical_rules(page)
        for tab in found:
            if not has_inner_rule(tab.bbox, verticals):   # alignment-only guess: use our own grid logic
                continue
            md = ruled_table_to_markdown(tab, page)
            if md:
                r = tuple(tab.bbox)
                table_rects.append(r)
                blocks.append(Block("table", table_md=md, y0=r[1], x0=r[0], page=page_no))

    # 2) text -> fragments grouped per source block
    data = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    raw: dict[int, list[Frag]] = {}
    for bid, b in enumerate(data.get("blocks", [])):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            dx, dy = ln.get("dir", (1, 0))
            if abs(dy) > 0.1:                       # rotated / vertical text
                continue
            for f in split_line_into_frags(ln, bid):
                cx, cy = (f.x0 + f.x1) / 2, (f.y0 + f.y1) / 2
                if any(inside(cx, cy, r) for r in table_rects):
                    continue
                raw.setdefault(bid, []).append(f)

    two_col = is_two_column(raw, pw)

    # 3) union blocks that share baselines (cells of the same row living in different blocks)
    parent = {b: b for b in raw}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    if not two_col:
        frags = [f for fs in raw.values() for f in fs]
        frags.sort(key=lambda f: f.base)
        for i, f in enumerate(frags):
            for g in frags[i + 1:]:
                if g.base - f.base > 2.5:
                    break
                if g.bid != f.bid and (g.x0 >= f.x1 or f.x0 >= g.x1) \
                        and (f.x1 - f.x0) < 0.45 * pw and (g.x1 - g.x0) < 0.45 * pw:
                    parent[find(g.bid)] = find(f.bid)

    groups: dict[int, list[Frag]] = {}
    for bid, fs in raw.items():
        groups.setdefault(find(bid), []).extend(fs)

    for fs in groups.values():
        lines = frags_to_lines(fs, page_no, pw)
        attach_list_markers(lines)
        if lines:
            blocks.append(Block("text", lines=lines, y0=lines[0].y0, x0=min(l.x0 for l in lines), page=page_no))

    return order_blocks(blocks, pw, two_col)


def frags_to_lines(frags: list[Frag], page_no: int, pw: float) -> list[Line]:
    frags = sorted(frags, key=lambda f: (f.base, f.x0))
    rows: list[list[Frag]] = []
    for f in frags:
        if rows and abs(rows[-1][0].base - f.base) <= 2.5:
            rows[-1].append(f)
        else:
            rows.append([f])
    lines = []
    for r in rows:
        r.sort(key=lambda f: f.x0)
        # re-join fragments that ended up adjacent (no real gap)
        merged = [r[0]]
        for f in r[1:]:
            p = merged[-1]
            if f.x0 - p.x1 <= col_gap(f.size):
                mk = MARKER_ONLY_RE.match(p.plain)
                if mk and len(p.plain) <= 4:          # stand-alone bullet / number glyph
                    num = mk.group(2)
                    pre = "•" if mk.group(1) else (f"{num}." if num.isdigit() else f"{num})")
                    p = Frag(pre, pre, f.size, f.bold, f.italic, f.mono, p.x0, p.y0, p.x1, p.y1, p.bid, p.base)
                main_size = p.size if len(p.plain) >= len(f.plain) else f.size
                merged[-1] = Frag(p.text + " " + f.text, p.plain + " " + f.plain, main_size,
                                  p.bold and f.bold, p.italic and f.italic, p.mono and f.mono,
                                  p.x0, min(p.y0, f.y0), f.x1, max(p.y1, f.y1), p.bid, p.base)
            else:
                merged.append(f)
        lines.append(Line(merged, page_no, pw))
    return lines


def attach_list_markers(lines: list[Line]) -> None:
    """A bullet / number drawn as its own object becomes the first 'cell'; glue it to the text."""
    for l in lines:
        if len(l.cells) >= 2 and MARKER_ONLY_RE.match(l.cells[0].plain) and len(l.cells[0].plain) <= 4:
            m = MARKER_ONLY_RE.match(l.cells[0].plain)
            nxt = l.cells[1]
            if nxt.x0 - l.cells[0].x1 > 40:
                continue
            num = m.group(2)
            prefix = "•" if m.group(1) else (f"{num}." if num.isdigit() else f"{num})")
            l.cells[1] = Frag(prefix + " " + nxt.text, prefix + " " + nxt.plain, nxt.size, nxt.bold,
                              nxt.italic, nxt.mono, l.cells[0].x0, nxt.y0, nxt.x1, nxt.y1, nxt.bid, nxt.base)
            del l.cells[0]


def is_two_column(raw: dict[int, list[Frag]], pw: float) -> bool:
    """True only for real two-column prose (papers, newsletters), not forms or invoices."""
    mid = pw / 2
    left = right = total = 0
    for fs in raw.values():
        x0, x1 = min(f.x0 for f in fs), max(f.x1 for f in fs)
        chars = sum(len(f.plain) for f in fs)
        avg = chars / max(len(fs), 1)
        total += chars
        if avg < 35:                     # short lines are not prose
            continue
        if x1 < mid + 10:
            left += chars
        elif x0 > mid - 10:
            right += chars
    return total > 0 and left / total > 0.3 and right / total > 0.3


def order_blocks(blocks: list[Block], pw: float, two_col: bool) -> list[Block]:
    if not two_col:
        return sorted(blocks, key=lambda b: (round(b.y0 / 3), b.x0))
    mid = pw / 2

    def full(b: Block) -> bool:
        if b.kind == "table":
            return True
        return b.x0 < mid - 10 and max(l.x1 for l in b.lines) > mid + 10

    ordered, band = [], []
    for b in sorted(blocks, key=lambda b: b.y0):
        if full(b):
            ordered += sorted(band, key=lambda x: (x.x0 >= mid - 10, x.y0)); band = []
            ordered.append(b)
        else:
            band.append(b)
    return ordered + sorted(band, key=lambda x: (x.x0 >= mid - 10, x.y0))


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
def md_table(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i].strip() for r in rows)]
    rows = [[r[i] for i in keep] for r in rows]
    header = [h or " " for h in rows[0]]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def vertical_rules(page) -> list[tuple[float, float, float]]:
    """Vertical line segments drawn on the page as (x, y_top, y_bottom)."""
    segs = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return segs
    for d in drawings:
        for it in d.get("items", []):
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.x - b.x) < 1.5 and abs(a.y - b.y) > 4:
                    segs.append((a.x, min(a.y, b.y), max(a.y, b.y)))
            elif it[0] == "re":
                r = it[1]
                if r.width < 3 and r.height > 4:           # hairline drawn as a thin rectangle
                    segs.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
                elif r.width >= 3 and r.height > 4:
                    segs += [(r.x0, r.y0, r.y1), (r.x1, r.y0, r.y1)]
    return segs


def has_inner_rule(bbox, segs) -> bool:
    x0, y0, x1, y1 = bbox
    h = max(y1 - y0, 1)
    for x, a, b in segs:
        if x0 + 5 < x < x1 - 5 and min(b, y1) - max(a, y0) >= 0.4 * h:
            return True
    return False


def ruled_table_to_markdown(tab, page) -> str:
    try:
        grid = [[(page.get_text("text", clip=c).strip() if c else "") for c in r.cells] for r in tab.rows]
    except Exception:
        try:
            grid = [[c or "" for c in r] for r in tab.extract()]
        except Exception:
            return ""
    rows: list[list[str]] = []
    for r in grid:
        cells = [[clean(x).strip() for x in c.split("\n") if x.strip()] for c in r]
        filled = [c for c in cells if c]
        counts = {len(c) for c in filled}
        # rows not separated by a rule come out as one row with N lines in every cell: split them
        if len(filled) >= 2 and len(counts) == 1 and counts.pop() >= 2:
            n = len(filled[0])
            for k in range(n):
                rows.append([c[k] if c else "" for c in cells])
        else:
            rows.append([" ".join(c) for c in cells])
    rows = [[re.sub(r"\s+", " ", c).replace("|", "\\|") for c in r] for r in rows if any(r)]
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return ""
    return md_table(rows)


def cell_md(f: Frag, header: bool = False) -> str:
    t = f.text.replace("|", "\\|") if not f.mono else f"`{f.plain}`"
    if f.bold and not header and not t.startswith("**"):
        t = f"**{t}**"
    if f.italic and not f.bold and not t.startswith("*"):
        t = f"*{t}*"
    return t


def render_grid(run: list[Line]) -> str:
    """Rows of aligned cells -> Markdown table (3+ columns) or key/value list (2 columns)."""
    multi = [l for l in run if l.multi]
    ncols = max(len(l.cells) for l in multi)

    if ncols == 2:
        labels = [l.cells[0].plain for l in multi]
        values = [l.cells[1].plain for l in multi]
        kv = sum(lb.rstrip().endswith(":") for lb in labels) + sum(bool(VALUE_RE.match(v)) for v in values)
        if kv >= len(multi) * 0.5 or len(multi) == 1:
            items: list[str] = []
            for l in run:
                if l.multi:
                    label = escape_md(" ".join(c.plain for c in l.cells[:-1]).rstrip().rstrip(":"))
                    items.append(f"- **{label}:** {cell_md(l.cells[-1])}")
                elif items:                                   # wrapped / extra value lines
                    items[-1] += "  \n  " + l.text
            return "\n".join(items)

    # column anchors from the row with the most cells (header preferred on ties)
    anchor = max(multi, key=lambda l: len(l.cells))
    cols = [(c.x0, c.x1) for c in anchor.cells]

    def col_of(f: Frag) -> int:
        best, score = 0, None
        for i, (a, b) in enumerate(cols):
            ov = min(b, f.x1) - max(a, f.x0)
            dist = abs((a + b) / 2 - (f.x0 + f.x1) / 2)
            s = (ov > 0, ov if ov > 0 else -dist)
            if score is None or s > score:
                best, score = i, s
        return best

    rows: list[list[str]] = []
    for l in run:
        if not l.multi and rows:                              # continuation line: append to its column
            c = col_of(l.cells[0])
            sep = " - " if (l.cells[0].italic or l.cells[0].size < l.size + 0.01 and l.cells[0].italic) else " "
            rows[-1][c] = (rows[-1][c] + sep + cell_md(l.cells[0])).strip()
            continue
        row = [""] * len(cols)
        for f in l.cells:
            c = col_of(f)
            row[c] = (row[c] + " " + cell_md(f, header=not rows)).strip()
        rows.append(row)
    return md_table(rows)


# --------------------------------------------------------------------------- #
# Header / footer removal
# --------------------------------------------------------------------------- #
def margin_key(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower()).strip()


def find_repeating_lines(pages_blocks, heights) -> set[str]:
    if len(pages_blocks) < 3:
        return set()
    counter: Counter = Counter()
    for blocks, h in zip(pages_blocks, heights):
        seen = set()
        for b in blocks:
            for l in b.lines:
                if l.y0 < h * 0.08 or l.y1 > h * 0.92:
                    k = margin_key(l.plain)
                    if k and k not in seen:
                        seen.add(k); counter[k] += 1
    return {k for k, c in counter.items() if c >= max(2, len(pages_blocks) * 0.5)}


def strip_margins(blocks, h, repeating) -> list[Block]:
    out = []
    for b in blocks:
        if b.kind == "text":
            b.lines = [l for l in b.lines if not ((l.y0 < h * 0.08 or l.y1 > h * 0.92)
                       and (margin_key(l.plain) in repeating or PAGE_NUM_RE.match(l.plain)))]
            if not b.lines:
                continue
        out.append(b)
    return out


# --------------------------------------------------------------------------- #
# Headings
# --------------------------------------------------------------------------- #
def build_heading_map(blocks: list[Block]) -> tuple[float, dict[float, int]]:
    sizes: Counter = Counter()
    for b in blocks:
        for l in b.lines:
            for c in l.cells:
                sizes[c.size] += len(c.plain)
    if not sizes:
        return 0.0, {}
    body = sizes.most_common(1)[0][0]
    total = sum(sizes.values())
    bigger = sorted((s for s in sizes if s >= body + 1.5 and sizes[s] / total < 0.25), reverse=True)
    levels: list[float] = []
    for s in bigger:
        if not levels or levels[-1] - s > 0.6:
            levels.append(s)
    levels = levels[:3]
    hmap = {}
    for s in bigger:
        hmap[s] = next((i + 1 for i, lv in enumerate(levels) if abs(s - lv) <= 0.6), len(levels))
    return body, hmap


def heading_level(l: Line, body: float, hmap: dict, seg_len: int) -> int:
    if l.multi or len(l.plain) > 150 or (l.plain.endswith((".", ",", ";")) and len(l.plain) > 60):
        return 0
    if BULLET_RE.match(l.plain) or VALUE_RE.match(l.plain):
        return 0
    if l.size in hmap:
        return hmap[l.size]
    if l.bold and seg_len <= 2 and len(l.plain) < 90 and not l.plain.endswith((".", ":", ",")) \
            and abs(l.size - body) < 1.5 and "@" not in l.plain:
        return 4
    return 0


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def join_lines(lines: list[str]) -> str:
    out = ""
    for t in (x.strip() for x in lines):
        if not out:
            out = t
        elif re.search(r"[A-Za-z]-$", out) and t[:1].islower():
            out = out[:-1] + t
        else:
            out += " " + t
    return out


def segments(lines: list[Line]) -> list[list[Line]]:
    """Split a block wherever there is a clear vertical gap."""
    segs: list[list[Line]] = []
    for l in lines:
        if segs and l.y0 - segs[-1][-1].y1 <= 0.6 * l.size:
            segs[-1].append(l)
        else:
            segs.append([l])
    return segs


def is_short_line_block(seg: list[Line]) -> bool:
    """Addresses, contact details, signature blocks: keep the line breaks."""
    if len(seg) < 2 or len(seg) > 10 or any(l.multi or l.mono for l in seg):
        return False
    if any(BULLET_RE.match(l.plain) or NUMBERED_RE.match(l.plain) for l in seg):
        return False
    avg = sum(len(l.plain) for l in seg) / len(seg)
    widest = max(l.x1 - l.x0 for l in seg)
    return avg < 45 and widest < 0.5 * seg[0].page_width


def render(blocks: list[Block], body: float, hmap: dict, page_breaks: bool, title: str | None) -> str:
    md: list[str] = []
    if title:
        md.append(f"# {escape_md(title)}")

    # flatten into a stream of (line, segment info) so tables can span blocks
    stream: list[tuple[str, object]] = []
    for b in blocks:
        if b.kind == "table":
            stream.append(("table", b))
            continue
        for seg in segments(b.lines):
            stream.append(("seg", seg))

    current_page = None
    i = 0
    while i < len(stream):
        kind, obj = stream[i]
        page = obj.page if kind == "table" else obj[0].page
        if page_breaks and page != current_page:
            if current_page is not None:
                md.append(f"---\n<!-- Page {page + 1} -->")
            current_page = page

        if kind == "table":
            md.append(obj.table_md)
            i += 1
            continue

        seg: list[Line] = obj
        # ---- grid rows (tables / key-value) can continue into following segments
        if any(l.multi for l in seg):
            pre = []
            while seg and not seg[0].multi:
                pre.append(seg.pop(0))
            if pre:
                stream.insert(i, ("seg", pre)); stream[i + 1] = ("seg", seg)
                continue
            run = []
            j = i
            ncat = None
            prev_line = prev_any = None
            while j < len(stream) and stream[j][0] == "seg":
                s: list[Line] = stream[j][1]
                if s[0].page != seg[0].page:
                    break
                if run and s[0].y0 - run[-1].y1 > 2.5 * s[0].size:
                    break
                ok = True
                for k, l in enumerate(s):
                    cat = None if not l.multi else (2 if len(l.cells) == 2 else 3)
                    if l.multi:
                        if ncat and cat != ncat:
                            ok = False
                        else:
                            ncat = cat
                    else:
                        # single cell continues the row only if it sits inside the grid, close below it
                        last = prev_line
                        above = prev_any
                        if not last or l.y0 - above.y1 > 1.2 * l.size or l.x0 <= last.x0 + 5:
                            ok = False
                        elif not any(abs(l.x0 - c.x0) <= 6 or c.x0 <= l.x0 <= c.x1 for c in last.cells[1:]):
                            ok = False
                    if not ok:
                        if k > 0:                                # split segment: rest goes back to stream
                            stream.insert(j + 1, ("seg", s[k:]))
                            stream[j] = ("seg", s[:k])
                            run += s[:k]
                            j += 1
                        break
                    prev_line = l if l.multi else prev_line
                    prev_any = l
                    if k == len(s) - 1:
                        run += s
                if not ok:
                    break
                j += 1
            if not run:                                           # safety
                run = [seg[0]]; stream[i] = ("seg", seg[1:]) if len(seg) > 1 else stream[i]; j = i + (len(seg) == 1)
            md.append(render_grid(run))
            i = max(j, i + 1)
            continue

        md.extend(render_segment(seg, body, hmap))
        i += 1

    # tight lists; blank line between different list types
    out: list[str] = []
    for chunk in (c for c in md if c.strip()):
        kind = "ul" if chunk.startswith("- ") else "ol" if re.match(r"^\d+\. ", chunk) else None
        last = out[-1].splitlines()[-1] if out else ""
        pk = "ul" if last.startswith(("- ", "  ")) and out[-1].startswith("- ") else \
             "ol" if re.match(r"^\d+\. ", out[-1] if out else "") else None
        if kind and kind == pk:
            out[-1] += "\n" + chunk
        else:
            out.append(chunk)
    text = "\n\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def render_segment(seg: list[Line], body: float, hmap: dict) -> list[str]:
    out: list[str] = []
    para: list[str] = []
    code: list[str] = []
    list_x0 = None
    short = is_short_line_block([l for l in seg if not heading_level(l, body, hmap, len(seg))])

    def flush():
        nonlocal para, list_x0
        if para:
            out.append("  \n".join(p.strip() for p in para) if short and not list_x0 else join_lines(para))
        para, list_x0 = [], None

    def flush_code():
        nonlocal code
        if code:
            out.append("```\n" + "\n".join(code) + "\n```"); code = []

    head_buf, head_lvl = [], 0
    for l in seg:
        if l.mono:
            flush(); code.append(l.plain); continue
        flush_code()
        lvl = heading_level(l, body, hmap, len(seg))
        if lvl:
            flush()
            if head_buf and lvl == head_lvl:
                head_buf.append(l.plain); continue
            if head_buf:
                out.append("#" * head_lvl + " " + escape_md(" ".join(head_buf)))
            head_buf, head_lvl = [l.plain], lvl
            continue
        if head_buf:
            out.append("#" * head_lvl + " " + escape_md(" ".join(head_buf))); head_buf = []

        mb, mn = BULLET_RE.match(l.plain), NUMBERED_RE.match(l.plain)
        if mb or (mn and len(l.plain) < 400):
            flush()
            if mb:
                txt = re.sub(rf"^\s*(\*\*|\*)?\s*\\?[{BULLET_CHARS}]\s*", r"\1", l.text, count=1)
                para = [f"- {txt}"]
            else:
                num = mn.group(1)
                rest = l.text.split(num, 1)[1].strip() if num in l.text else mn.group(2)
                para = [f"{num[:-1]}. {rest}" if num[:-1].isdigit() else f"- **{num}** {rest}"]
            list_x0 = l.x0
            continue
        if list_x0 is not None:
            if l.x0 > list_x0 + 3:
                para.append(l.text); continue
            flush()
        text = l.text
        if not para and re.match(r"^(#|>|\d+\.\s|[-+]\s)", text):
            text = "\\" + text
        para.append(text)

    if head_buf:
        flush(); out.append("#" * head_lvl + " " + escape_md(" ".join(head_buf)))
    flush(); flush_code()
    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def convert(pdf_path: Path, out_path: Path, pages: str | None = None, page_breaks: bool = False,
            use_tables: bool = True, password: str | None = None) -> Path:
    doc = pymupdf.open(pdf_path)
    if doc.needs_pass and (not password or not doc.authenticate(password)):
        raise RuntimeError(f"{pdf_path.name} is password-protected (use --password)")

    ids = parse_page_ranges(pages, doc.page_count) if pages else list(range(doc.page_count))
    pages_blocks, heights = [], []
    for i in ids:
        pages_blocks.append(extract_page(doc[i], i, use_tables))
        heights.append(doc[i].rect.height)

    repeating = find_repeating_lines(pages_blocks, heights)
    blocks: list[Block] = []
    for pb, h in zip(pages_blocks, heights):
        blocks += strip_margins(pb, h, repeating)

    body, hmap = build_heading_map(blocks)
    meta_title = ((doc.metadata or {}).get("title") or "").strip()
    has_h1 = any(v == 1 for v in hmap.values())
    title = meta_title if meta_title and not has_h1 and len(meta_title) < 150 \
        and meta_title.lower() not in ("untitled", "sample") else None

    md = render(blocks, body, hmap, page_breaks, title)
    if not md.strip():
        md = f"<!-- No extractable text in {pdf_path.name}: it is probably a scanned PDF and needs OCR. -->\n"
    out_path.write_text(md, encoding="utf-8")
    doc.close()
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert PDF text into clean, readable Markdown (deterministic).")
    ap.add_argument("input", type=Path, help="PDF file, or a folder of PDFs")
    ap.add_argument("-o", "--output", type=Path, help="Output .md file (or folder when input is a folder)")
    ap.add_argument("--pages", help="Page selection, e.g. '1-5,8,10-'")
    ap.add_argument("--page-breaks", action="store_true", help="Insert a divider between pages")
    ap.add_argument("--no-tables", action="store_true", help="Disable ruled-table detection")
    ap.add_argument("--password", help="Password for encrypted PDFs")
    args = ap.parse_args()

    if args.input.is_dir():
        pdfs = sorted(p for p in args.input.iterdir() if p.suffix.lower() == ".pdf")
        if not pdfs:
            sys.exit(f"No PDFs found in {args.input}")
        out_dir = args.output or args.input
        out_dir.mkdir(parents=True, exist_ok=True)
        targets = [(p, out_dir / (p.stem + ".md")) for p in pdfs]
    else:
        if not args.input.exists():
            sys.exit(f"File not found: {args.input}")
        targets = [(args.input, args.output or args.input.with_suffix(".md"))]

    failures = 0
    for src, dst in targets:
        try:
            convert(src, dst, args.pages, args.page_breaks, not args.no_tables, args.password)
            print(f"OK   {src.name} -> {dst}")
        except Exception as e:
            failures += 1
            print(f"FAIL {src.name}: {e}", file=sys.stderr)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
