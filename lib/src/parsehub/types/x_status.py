"""Native X status text and card data, independent of markup and delivery."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from .document import TextRun as TextRun


@dataclass(frozen=True)
class XPost:
    segments: tuple[TextRun, ...]
    author_name: str = ""
    author_handle: str = ""
    author_url: str = ""
    poll_index: int | None = None
    youtube: TextRun | None = None


@dataclass(frozen=True)
class XTrending:
    title: str = ""
    subtitle: str = ""
    url: str = ""
    cover_index: int | None = None


@dataclass(frozen=True)
class XStatus:
    main: XPost
    reply: XPost | None = None
    quoted: XPost | None = None
    trending: XTrending | None = None


_STYLES = {"Bold": "bold", "Italic": "italic", "Strikethrough": "strike"}
_ESCAPES = {"&amp;": "&", "&lt;": "<", "&gt;": ">"}
_ESCAPE = re.compile(r"&(?:amp|lt|gt);")
_SHORT_URL = re.compile(r"https?://t\.co/[A-Za-z0-9]+")
_WORD = re.compile(r"\S+")
_HANDLE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    url: str = ""
    label: str | None = None  # None retains the source slice; "" removes the attachment token.


_Bounds = tuple[int, int]


@dataclass(frozen=True)
class _EntitySpan:
    codepoints: _Bounds | None
    utf16: _Bounds | None
    url: str
    label: str | None = None

    def resolve(self, prefer_utf16: bool | None) -> _Span | None:
        if prefer_utf16 is None and self.codepoints and self.utf16 and self.codepoints != self.utf16:
            return None  # No evidence to choose between different occurrences of the same token.
        bounds = (self.utf16 or self.codepoints) if prefer_utf16 else (self.codepoints or self.utf16)
        return _Span(*bounds, self.url, self.label) if bounds else None


def _prefer_utf16(entities: Sequence[_EntitySpan]) -> bool | None:
    # Only verified ranges that distinguish the two units count as evidence.
    # Duplicate metadata (e.g. several photos at a single URL) contributes one vote.
    codepoints = {e.codepoints for e in entities if e.codepoints and not e.utf16}
    utf16 = {e.utf16 for e in entities if e.utf16 and not e.codepoints}
    return len(utf16) > len(codepoints) if len(utf16) != len(codepoints) else None


def _items(value: object) -> Iterator[Mapping[str, object]]:
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, Mapping):
                yield item


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _range(start: object, end: object) -> tuple[int, int] | None:
    if type(start) is int and type(end) is int and 0 <= start < end:
        return start, end
    return None


def _utf16_boundaries(text: str) -> dict[int, int]:
    char_at = {0: 0}
    offset = 0
    for index, char in enumerate(text, 1):
        offset += 2 if ord(char) > 0xFFFF else 1
        char_at[offset] = index
    return char_at


def _token_ranges(
    text: str,
    indices: object,
    tokens: tuple[str, ...],
    char_at: Mapping[int, int],
    *,
    ignore_case: bool = False,
    whole_word: bool = False,
) -> tuple[_Bounds | None, _Bounds | None]:
    if not isinstance(indices, (list, tuple)) or len(indices) != 2:
        return None, None
    raw = _range(*indices)
    if raw is None:
        return None, None
    start, end = raw
    utf16 = (char_at[start], char_at[end]) if start in char_at and end in char_at else None

    def verified(bounds: _Bounds | None) -> _Bounds | None:
        if bounds is None:
            return None
        a, b = bounds
        label = text[a:b]
        if (label.lower() if ignore_case else label) not in tokens:
            return None
        if (short := _SHORT_URL.match(text, a)) and short.end() > b:
            return None  # An exact substring is still not a t.co token if its ID was cut short.
        if whole_word and ((a > 0 and not text[a - 1].isspace()) or (b < len(text) and not text[b].isspace())):
            return None
        return bounds

    codepoints = verified(raw) if end <= len(text) else None
    return codepoints, codepoints if utf16 == raw else verified(utf16)


def _web_url(value: str) -> str:
    if not value.startswith(("https://", "http://")) or any(c.isspace() for c in value):
        return ""
    try:
        return value if urlsplit(value).hostname else ""
    except ValueError:
        return ""


def _link_candidates(
    text: str,
    entities: Mapping[str, object],
    char_at: Mapping[int, int],
    *,
    translation: bool,
) -> Iterator[_EntitySpan]:
    for item in _items(entities.get("urls")):
        raw = _string(item.get("url"))
        target = _web_url(_string(item.get("expanded_url"))) or _web_url(raw)
        if not target:
            continue
        tokens: tuple[str, ...] = (raw, _string(item.get("display_url")), target) if translation else (raw,)
        bounds = _token_ranges(text, item.get("indices"), tokens, char_at)
        yield _EntitySpan(*bounds, target, None if translation or raw == target else target)

    for kind, field, prefixes, base in (
        ("hashtags", "text", "#＃", "https://x.com/hashtag/"),
        ("user_mentions", "screen_name", "@＠", "https://x.com/"),
        ("symbols", "text", "$", "https://x.com/search?q=%24"),
    ):
        mention = kind == "user_mentions"
        for item in _items(entities.get(kind)):
            name = _string(item.get(field))
            if not name or any(c.isspace() for c in name) or (mention and not _HANDLE.fullmatch(name)):
                continue
            expected = name.lower() if mention else name
            tokens = tuple(prefix + expected for prefix in prefixes)
            bounds = _token_ranges(text, item.get("indices"), tokens, char_at, ignore_case=mention)
            try:
                yield _EntitySpan(*bounds, url=base + quote(name, safe=""))
            except UnicodeEncodeError:
                continue


def _media_candidates(
    text: str,
    entities: Mapping[str, object],
    char_at: Mapping[int, int],
) -> Iterator[_EntitySpan]:
    for item in _items(entities.get("media")):
        url = _string(item.get("url"))
        if _SHORT_URL.fullmatch(url):
            bounds = _token_ranges(text, item.get("indices"), (url,), char_at, whole_word=True)
            yield _EntitySpan(*bounds, url)


def _media_spans(text: str, resolved: Iterable[_Span], media_urls: Iterable[str]) -> list[_Span]:
    indexed = {(span.start, span.end): span.url for span in resolved}

    # Note's entity_set can lack media. A verified legacy/extended media URL then identifies
    # only the appended, whole token at the tail, never earlier occurrences in the prose.
    fallback = {url for url in media_urls if isinstance(url, str) and _SHORT_URL.fullmatch(url)}
    fallback.difference_update(indexed.values())
    tail = []
    if fallback or indexed:
        for token in reversed(list(_WORD.finditer(text))):
            bounds, url = token.span(), token.group()
            if bounds not in indexed and url not in fallback:
                break
            start, end = bounds
            if start > 0 and text[start - 1] == " ":
                start -= 1  # Remove only the injected separator, not authored whitespace.
            tail.append(_Span(start, end, label=""))
            indexed.pop(bounds, None)
            fallback.discard(url)
    return tail + [_Span(*bounds, label="") for bounds in indexed]


def _style_ranges(
    tags: Sequence[object] | None,
    char_at: Mapping[int, int],
    length: int,
) -> tuple[list[int], list[tuple[str, ...]]]:
    changes: defaultdict[int, Counter[str]] = defaultdict(Counter)
    for tag in _items(tags):
        bounds = _range(tag.get("from_index"), tag.get("to_index"))
        kinds = tag.get("richtext_types")
        if bounds is None or not isinstance(kinds, (list, tuple)):
            continue
        start, end = bounds
        if start not in char_at or end not in char_at:
            continue
        for kind in kinds:
            if isinstance(kind, str) and (style := _STYLES.get(kind)):
                changes[char_at[start]][style] += 1
                changes[char_at[end]][style] -= 1

    boundaries = sorted(changes.keys() | {0, length})
    active: Counter[str] = Counter()
    styles = []
    for boundary in boundaries[:-1]:
        active.update(changes.get(boundary, {}))
        styles.append(tuple(style for style in _STYLES.values() if active[style] > 0))
    return boundaries, styles


def text_runs(
    text: str,
    entities: Mapping[str, object] | None = None,
    *,
    richtext_tags: Sequence[object] | None = (),
    media_urls: Iterable[str] = (),
    translation: bool = False,
) -> tuple[TextRun, ...]:
    """Turn raw X text into literal runs with verified link and style metadata.

    Saved legacy and note entities use code points, with UTF-16 compatibility
    for originals. Prefer the unit supported by the entity set; only fall back
    if the preferred slice does not match the token. Translation entities use
    code points only and preserve the label. Note styles are UTF-16.
    Supply only metadata-backed attachment short URLs in ``media_urls``.
    """
    if not text:
        return ()
    entities = entities if isinstance(entities, Mapping) else {}
    char_at = _utf16_boundaries(text) if richtext_tags or (entities and not translation) else {}
    entity_offsets = {} if translation else char_at
    link_candidates = list(_link_candidates(text, entities, entity_offsets, translation=translation))
    media_candidates = list(_media_candidates(text, entities, entity_offsets))
    prefer_utf16 = False if translation else _prefer_utf16(link_candidates + media_candidates)
    media = _media_spans(text, (span for e in media_candidates if (span := e.resolve(prefer_utf16))), media_urls)
    links = (span for e in link_candidates if (span := e.resolve(prefer_utf16)))
    spans = media + [link for link in links if not any(m.start < link.end and link.start < m.end for m in media)]
    boundaries, style_ranges = _style_ranges(richtext_tags, char_at, len(text))
    runs: list[TextRun] = []

    def append(label: str, url: str, styles: tuple[str, ...]) -> None:
        if runs and (runs[-1].url, runs[-1].styles) == (url, styles):
            label = runs.pop().text + label
        runs.append(TextRun(label, url, styles))

    def emit(start: int, end: int, url: str = "", label: str | None = None) -> None:
        if start == end or label == "":
            return
        first, last = bisect_right(boundaries, start) - 1, bisect_left(boundaries, end)
        if label is not None:
            # The expansion does not share offsets with the source. Only styles covering
            # the whole link have an unambiguous meaning on the replacement label.
            whole_link = tuple(
                style for style in style_ranges[first] if all(style in group for group in style_ranges[first:last])
            )
            append(label, url, whole_link)
            return
        for index in range(first, last):
            value = text[max(start, boundaries[index]) : min(end, boundaries[index + 1])]
            if not translation:
                # Decode X's three escaped characters, once, after indexing the raw text.
                # HTML5's broader named/numeric entity table is not a source-text parser.
                value = _ESCAPE.sub(lambda m: _ESCAPES[m.group()], value)
            append(value, url, style_ranges[index])

    cursor = 0
    for span in sorted(spans, key=lambda item: (item.start, -item.end)):
        if span.start < cursor:
            continue
        emit(cursor, span.start)
        emit(span.start, span.end, span.url, span.label)
        cursor = span.end
    emit(cursor, len(text))
    return tuple(runs)
