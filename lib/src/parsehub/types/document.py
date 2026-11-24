"""Structured post body shared by every platform and delivery target.

Only a source adapter interprets markup. TextRun.text is always literal; media
nodes point at the existing download list, never at a new download operation.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import groupby
from typing import Literal
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


@dataclass(frozen=True)
class TextRun:
    text: str
    url: str = ""
    styles: tuple[str, ...] = ()


BlockKind = Literal[
    "paragraph", "heading", "quote", "details", "list", "table", "code",
    "media", "poll", "rule", "collage", "localize",
]


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    runs: tuple[TextRun, ...] = ()
    children: tuple[Block, ...] = ()
    items: tuple[tuple[Block, ...], ...] = ()
    rows: tuple[tuple[tuple[TextRun, ...], ...], ...] = ()
    index: int = -1
    url: str = ""
    level: int = 1
    ordered: bool = False
    start: int = 1
    language: str = ""
    role: str = ""
    independent: bool = False
    header: bool = False


@dataclass(frozen=True)
class Document:
    blocks: tuple[Block, ...] = ()
    origin: tuple[TextRun, ...] = ()
    topic: tuple[TextRun, ...] = ()


def paragraph(text: str | Iterable[TextRun]) -> Block:
    return Block("paragraph", (TextRun(text),) if isinstance(text, str) else tuple(text))


def plain(text: str | None) -> Document:
    return Document((paragraph(text),)) if text else Document()


def document_from_dict(data: dict) -> Document:
    """Restore immutable records from the cache's JSON shape."""
    def runs(values: Iterable[dict] = ()) -> tuple[TextRun, ...]:
        return tuple(TextRun(v["text"], v.get("url", ""), tuple(v.get("styles", ()))) for v in values)

    def blocks(values: Iterable[dict] = ()) -> tuple[Block, ...]:
        return tuple(Block(**{
            **v,
            "runs": runs(v.get("runs", ())),
            "children": blocks(v.get("children", ())),
            "items": tuple(blocks(item) for item in v.get("items", ())),
            "rows": tuple(tuple(runs(cell) for cell in row) for row in v.get("rows", ())),
        }) for v in values)

    return Document(blocks(data.get("blocks", ())), runs(data.get("origin", ())), runs(data.get("topic", ())))


def visible_text(blocks: Iterable[Block], *, include_summary: bool = True) -> str:
    """Display/preview text, without markup stripping or entity reinterpretation."""
    lines = []
    for block in blocks:
        if include_summary or block.kind != "details":
            if text := "".join(run.text for run in block.runs):
                lines.append(text)
        if block.children:
            lines.append(visible_text(block.children, include_summary=include_summary))
        lines.extend(visible_text(item, include_summary=include_summary) for item in block.items)
        lines.extend(" | ".join("".join(run.text for run in cell) for cell in row) for row in block.rows)
    return "\n\n".join(filter(None, lines))


_STYLE = {
    "b": "bold", "strong": "bold", "i": "italic", "em": "italic", "s": "strike", "strike": "strike",
    "del": "strike", "u": "underline", "ins": "underline", "code": "code", "kbd": "code",
    "sup": "superscript", "sub": "subscript", "mark": "marked", "tg-spoiler": "spoiler",
}
_IGNORED = frozenset(("script", "style", "template", "noscript", "svg", "iframe"))
_WRAPPERS = frozenset(("html", "body", "main", "section", "article", "div", "figure", "figcaption", "address"))
_HEADING = {f"h{i}": i for i in range(1, 7)}


def _coalesce(runs: Sequence[TextRun]) -> tuple[TextRun, ...]:
    return tuple(
        TextRun(text, *key)
        for key, group in groupby((r for r in runs if r.text), key=lambda r: (r.url, r.styles))
        if (text := "".join(r.text for r in group))
    )


class _SourceHTML:
    def __init__(self, base_url: str, media: Mapping[str, int] | None):
        self.base_url = base_url
        self.media = media

    def url(self, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            return ""
        try:
            url = urljoin(self.base_url, value.strip())
            parsed = urlsplit(url)
            if parsed.scheme in {"mailto", "tel"} or (parsed.scheme in {"http", "https"} and parsed.netloc):
                return url
        except ValueError:
            pass
        return ""

    @staticmethod
    def integer(tag: Tag, key: str, default: int) -> int:
        try:
            return int(str(tag.get(key, default)))
        except ValueError:
            return default

    def scan(self, nodes: Iterable, url: str = "", styles: tuple[str, ...] = (), depth: int = 0) -> tuple[Block, ...]:
        if depth >= 64:
            return ()
        blocks: list[Block] = []
        pending: list[TextRun] = []

        def flush() -> None:
            if pending:
                runs = _coalesce(pending)
                pending.clear()
                if any(run.text.strip() for run in runs):
                    blocks.append(paragraph(runs))

        def absorb(children: Iterable[Block]) -> None:
            for child in children:
                if child.kind == "paragraph" and not child.role:
                    pending.extend(child.runs)
                else:
                    flush()
                    blocks.append(child)

        for node in nodes:
            if isinstance(node, Comment):
                continue
            if isinstance(node, NavigableString):
                pending.append(TextRun(str(node), url, styles))
                continue
            if not isinstance(node, Tag) or node.name in _IGNORED or node.has_attr("hidden"):
                continue
            name = node.name
            if name == "br":
                pending.append(TextRun("\n", url, styles))
            elif name == "img":
                img = self.image(node)
                if img is not None:
                    flush()
                    blocks.append(img)
                elif node.get("alt") and set(node.get("class", ())) & {"emoji", "emoticon", "smilies"}:
                    pending.append(TextRun(str(node["alt"]), url, styles))
            elif name == "input" and node.get("type") == "checkbox":
                pending.append(TextRun("☑ " if node.has_attr("checked") else "☐ "))
            elif name in (*_HEADING, *_WRAPPERS, "p", "hr", "pre", "ol", "ul", "blockquote", "details", "table"):
                flush()
                blocks.extend(self.block(node, url, styles, depth + 1))
            else:
                style = _STYLE.get(name)
                if set(node.get("class", ())) & {"spoiler", "spoiler-text", "spoiler_blurred"}:
                    style = "spoiler"
                next_styles = (*styles, style) if style and style not in styles else styles
                href = self.url(node.get("href")) if name == "a" else url
                absorb(self.scan(node.children, href, next_styles, depth + 1))
        flush()
        return tuple(blocks)

    def image(self, node: Tag) -> Block | None:
        for attr in ("data-original", "data-src", "data-actualsrc", "src"):
            if not (url := self.url(node.get(attr))):
                continue
            if self.media is None:
                return Block("media", url=url)
            if url in self.media:
                return Block("media", index=self.media[url], url=url)
        return None

    @staticmethod
    def text_runs(blocks: Iterable[Block]) -> tuple[TextRun, ...]:
        runs: list[TextRun] = []
        for block in blocks:
            if runs and block.runs:
                runs.append(TextRun("\n"))
            runs.extend(block.runs)
        return tuple(runs)

    def block(self, tag: Tag, url: str, styles: tuple[str, ...], depth: int) -> tuple[Block, ...]:
        name = tag.name
        if name == "hr":
            return (Block("rule"),)
        if name == "pre":
            code = tag.find("code", recursive=False)
            classes = (code or tag).get("class", ())
            language = next((c.partition("-")[2] for c in classes if c.startswith("language-")), "")
            return (Block("code", (TextRun(tag.get_text()),), language=language),)
        if name in {"ol", "ul"}:
            items = tuple(self.scan(li.children, url, styles, depth) for li in tag.find_all("li", recursive=False))
            if not items:
                return ()
            return (Block("list", items=items, ordered=name == "ol", start=max(1, self.integer(tag, "start", 1))),)
        if name == "table":
            rows = []
            header = False
            for tr in tag.find_all("tr"):
                if tr.find_parent("table") is not tag:
                    continue
                cells = tr.find_all(("td", "th"), recursive=False)
                if cells:
                    if not rows:
                        header = any(cell.name == "th" for cell in cells)
                    rows.append(tuple(self.text_runs(self.scan(cell.children, url, styles, depth)) for cell in cells))
            return (Block("table", rows=tuple(rows), header=header),) if rows else ()
        if name == "details":
            summary = tag.find("summary", recursive=False)
            summary_runs = self.text_runs(self.scan(summary.children, url, styles, depth)) if summary else ()
            children = self.scan((node for node in tag.children if node is not summary), url, styles, depth)
            return (Block("details", runs=summary_runs, children=children),) if children else ()
        children = self.scan(tag.children, url, styles, depth)
        if name == "blockquote":
            return (Block("quote", children=children),) if children else ()
        if name in _HEADING:
            return tuple(
                replace(b, kind="heading", level=_HEADING[name]) if b.kind == "paragraph" else b for b in children
            )
        return children


def html_blocks(
    source: str | Tag | BeautifulSoup, *, base_url: str = "", media: Mapping[str, int] | None = None,
) -> tuple[Block, ...]:
    """Convert source DOM once. A media map restricts images to selected attachments."""
    root = BeautifulSoup(source, "lxml") if isinstance(source, str) else source
    return _SourceHTML(base_url, media).scan(root.children)
