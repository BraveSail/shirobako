import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from parsehub.types.x_status import TextRun, XPost, XStatus, XTrending, text_runs


def test_status_dtos_expose_the_frozen_contract():
    run = TextRun("文")
    post = XPost((run,))
    trending = XTrending(cover_index=0)
    status = XStatus(post, quoted=XPost(()), trending=trending)
    assert tuple(field.name for field in fields(post)) == (
        "segments",
        "author_name",
        "author_handle",
        "author_url",
        "poll_index",
        "youtube",
    )
    assert (run.url, run.styles) == ("", ())
    assert (post.author_name, post.author_handle, post.author_url, post.poll_index, post.youtube) == (
        "",
        "",
        "",
        None,
        None,
    )
    assert (trending.title, trending.subtitle, trending.url, trending.cover_index) == ("", "", "", 0)
    assert (status.main, status.reply, status.quoted, status.trending) == (post, None, XPost(()), trending)
    for obj, field, value in (
        (run, "text", "other"),
        (post, "segments", ()),
        (trending, "cover_index", None),
        (status, "main", XPost(())),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, value)


def test_plain_text_preserves_whitespace_and_literal_markup():
    source = "  **bold**\n> quote\n    || <a> {{media:0}} \\_ #untagged @nobody  \n"
    assert text_runs(source) == (TextRun(source),)
    assert text_runs("") == ()


def _bounds(text: str, token: str, *, utf16: bool = False) -> list[int]:
    start = text.index(token)
    ends = [start, start + len(token)]
    return [len(text[:i].encode("utf-16-le")) // 2 for i in ends] if utf16 else ends


def test_entities_link_real_tokens_without_inventing_links():
    source = "😀 @MiXeD  ＃日本語」$X   #untagged"
    entities = {
        "symbols": [{"indices": _bounds(source, "$X"), "text": "X"}],
        "hashtags": [{"indices": _bounds(source, "＃日本語"), "text": "日本語"}],
        "user_mentions": [{"indices": _bounds(source, "@MiXeD"), "screen_name": "mixed"}],
    }
    assert text_runs(source, entities) == (
        TextRun("😀 "),
        TextRun("@MiXeD", "https://x.com/mixed"),
        TextRun("  "),
        TextRun("＃日本語", "https://x.com/hashtag/%E6%97%A5%E6%9C%AC%E8%AA%9E"),
        TextRun("」"),
        TextRun("$X", "https://x.com/search?q=%24X"),
        TextRun("   #untagged"),
    )


def test_fallback_unit_requires_a_verified_token_when_the_group_prefers_code_points():
    source = "🐈‍⬛ @name #tag $X"
    entities = {
        "hashtags": [{"indices": _bounds(source, "#tag"), "text": "tag"}],
        "symbols": [{"indices": _bounds(source, "$X"), "text": "X"}],
        "user_mentions": [{"indices": _bounds(source, "@name", utf16=True), "screen_name": "name"}],
    }
    assert text_runs(source, entities) == (
        TextRun("🐈‍⬛ "),
        TextRun("@name", "https://x.com/name"),
        TextRun(" "),
        TextRun("#tag", "https://x.com/hashtag/tag"),
        TextRun(" "),
        TextRun("$X", "https://x.com/search?q=%24X"),
    )


@pytest.mark.parametrize(
    "indices", [([7, 9], [4, 6]), ([7, 9], [10, 12])], ids=["codepoints", "utf16"]
)
def test_repeated_hashtags_use_the_unit_supported_by_the_group(indices):
    # [7, 9] matches either occurrence; the other entity disambiguates it even if it comes later.
    source = "😀😀😀 #x #x"
    entities = {"hashtags": [{"indices": bounds, "text": "x"} for bounds in indices]}
    tag = TextRun("#x", "https://x.com/hashtag/x")
    assert text_runs(source, entities) == (TextRun("😀😀😀 "), tag, TextRun(" "), tag)


def test_ambiguous_offset_without_group_evidence_does_not_guess():
    source = "😀😀😀 #x #x"
    assert text_runs(source, {"hashtags": [{"indices": [7, 9], "text": "x"}]}) == (TextRun(source),)


@pytest.mark.parametrize("utf16", [False, True], ids=["codepoints", "utf16"])
def test_ambiguous_short_url_uses_the_same_unit_as_other_entity_kinds(utf16):
    short = "https://t.co/abc"
    target = "https://example.com/expanded"
    prefix = "😀" * (len(short) + 1) + " "
    source = f"{prefix}{short} {short} #unit"
    # The same indices point at the first URL in UTF-16 and the second in code points.
    entities = {
        "urls": [{"indices": _bounds(source, short, utf16=True), "url": short, "expanded_url": target}],
        "hashtags": [{"indices": _bounds(source, "#unit", utf16=utf16), "text": "unit"}],
    }
    leading = prefix if utf16 else f"{prefix}{short} "
    trailing = f" {short} " if utf16 else " "
    assert text_runs(source, entities) == (
        TextRun(leading),
        TextRun(target, target),
        TextRun(trailing),
        TextRun("#unit", "https://x.com/hashtag/unit"),
    )


def test_bad_entity_offsets_are_not_relocated_by_search():
    source = "😀 yes #tag @ok https://t.co/abc"
    entities = {
        "hashtags": [None, {"indices": [2, 6], "text": "tag"}, {"text": "tag"}],
        "user_mentions": [{"indices": _bounds(source, "@ok"), "screen_name": "ok"}],
        "urls": [
            {"indices": [False, 3], "url": "😀 y"},
            {"indices": [-1, 0], "url": "https://t.co/abc"},
            {"indices": [7, 999], "url": "https://t.co/abc"},
            {"indices": [15, "31"], "url": "https://t.co/abc"},
            {"indices": _bounds(source, "yes"), "url": "https://t.co/abc"},
            {"indices": _bounds(source, "#tag"), "expanded_url": "https://example.com"},
        ],
        "symbols": "not a list",
    }
    assert text_runs(source, entities) == (
        TextRun("😀 yes #tag "),
        TextRun("@ok", "https://x.com/ok"),
        TextRun(" https://t.co/abc"),
    )


def test_truncated_short_link_entity_does_not_replace_a_longer_token():
    short = "https://t.co/abc"
    source = f"read {short}def"
    entities = {"urls": [{"indices": _bounds(source, short), "url": short, "expanded_url": "https://example.com"}]}
    assert text_runs(source, entities) == (TextRun(source),)


@pytest.mark.parametrize("translation", [False, True])
def test_url_uses_official_expansion_and_only_translation_keeps_the_label(translation):
    short = "https://t.co/abc"
    target = "https://example.com/long?a=1&b=2"
    source = f"😀  {short}\n  "
    entities = {"urls": [{"indices": _bounds(source, short), "url": short, "expanded_url": target}]}
    assert text_runs(source, entities, translation=translation) == (
        TextRun("😀  "),
        TextRun(short if translation else target, target),
        TextRun("\n  "),
    )


def test_translation_only_interprets_code_point_indices():
    source = "😀 @name #tag"
    entities = {
        "hashtags": [{"indices": _bounds(source, "#tag"), "text": "tag"}],
        "user_mentions": [{"indices": _bounds(source, "@name", utf16=True), "screen_name": "name"}],
    }
    assert text_runs(source, entities, translation=True) == (
        TextRun("😀 @name "),
        TextRun("#tag", "https://x.com/hashtag/tag"),
    )


def test_translation_preserves_verified_readable_url_label():
    label = "example.com/a[b]"
    target = "https://example.com/a%5Bb%5D"
    source = f"😀  看 {label} 吧"
    entities = {
        "urls": [
            {"indices": _bounds(source, label), "url": "https://t.co/abc", "display_url": label, "expanded_url": target}
        ]
    }
    assert text_runs(source, entities, translation=True) == (
        TextRun("😀  看 "),
        TextRun(label, target),
        TextRun(" 吧"),
    )


def test_platform_escaping_is_decoded_once_after_entity_indexing():
    source = "&lt;a&gt; &amp; &amp;lt; &copy; &#62; &notit; #tag"
    entities = {"hashtags": [{"text": "tag", "indices": _bounds(source, "#tag")}]}
    assert text_runs(source, entities) == (
        TextRun("<a> & &lt; &copy; &#62; &notit; "),
        TextRun("#tag", "https://x.com/hashtag/tag"),
    )
    assert text_runs(source, entities, translation=True) == (
        TextRun(source[:-4]),
        TextRun("#tag", "https://x.com/hashtag/tag"),
    )


def test_overlapping_note_styles_are_combined_on_utf16_boundaries():
    source = "😀abcde"
    tags = [
        {"from_index": 2, "to_index": 5, "richtext_types": ["Bold", "Bold"]},
        {"from_index": 3, "to_index": 6, "richtext_types": ["Italic", "Strikethrough"]},
        {"from_index": 4, "to_index": 7, "richtext_types": ["Unsupported"]},
    ]
    assert text_runs(source, richtext_tags=tags) == (
        TextRun("😀"),
        TextRun("a", styles=("bold",)),
        TextRun("bc", styles=("bold", "italic", "strike")),
        TextRun("d", styles=("italic", "strike")),
        TextRun("e"),
    )


def test_note_styles_with_invalid_utf16_boundaries_are_not_clamped():
    source = "😀abc"
    tags = [
        None,
        {"from_index": 1, "to_index": 3, "richtext_types": ["Italic"]},
        {"from_index": 2, "to_index": 6, "richtext_types": ["Bold"]},
        {"from_index": 2, "to_index": 3, "richtext_types": "Bold"},
    ]
    assert text_runs(source, richtext_tags=tags) == (TextRun(source),)


def test_note_styles_remain_attached_to_the_expanded_link():
    short = "https://t.co/abc"
    target = "https://example.com/long"
    source = f"😀 {short} **after**"
    start, end = _bounds(source, short, utf16=True)
    entities = {"urls": [{"indices": _bounds(source, short), "url": short, "expanded_url": target}]}
    tags = [{"from_index": start, "to_index": end + 3, "richtext_types": ["Bold"]}]
    assert text_runs(source, entities, richtext_tags=tags) == (
        TextRun("😀 "),
        TextRun(target, target, ("bold",)),
        TextRun(" **", styles=("bold",)),
        TextRun("after**"),
    )


def test_link_with_unchanged_label_keeps_partial_note_styles():
    source = "😀 #name"
    entities = {"hashtags": [{"indices": _bounds(source, "#name"), "text": "name"}]}
    start, end = _bounds(source, "na", utf16=True)
    tags = [{"from_index": start, "to_index": end, "richtext_types": ["Italic"]}]
    target = "https://x.com/hashtag/name"
    assert text_runs(source, entities, richtext_tags=tags) == (
        TextRun("😀 "),
        TextRun("#", target),
        TextRun("na", target, ("italic",)),
        TextRun("me", target),
    )


def test_metadata_backed_media_suffix_does_not_strip_authored_links():
    media = "https://t.co/pic"
    regular = "https://t.co/picture"
    source = f"{media}  \n{regular}   {media}"
    assert text_runs(source, media_urls=(media,)) == (TextRun(f"{media}  \n{regular}  "),)
    assert text_runs(f"image attached but real link {regular}", media_urls=(media,)) == (
        TextRun(f"image attached but real link {regular}"),
    )
    assert text_runs(media, media_urls=(media,)) == ()


def test_indexed_media_uses_its_exact_span_and_preserves_internal_whitespace():
    media = "https://t.co/pic"
    source = f"😀  {media}\n  end"
    indices = _bounds(source, media)
    entities = {
        "media": [{"indices": indices, "url": media}],
        "urls": [{"indices": indices, "url": media, "expanded_url": "https://x.com/u/status/1/photo/1"}],
    }
    assert text_runs(source, entities) == (TextRun("😀  \n  end"),)


def test_saved_note_response_uses_code_point_entities_and_utf16_styles():
    fixture = Path(__file__).with_name("fixtures") / "twitter_richtext_emoji.json"
    node = json.loads(fixture.read_text())["data"]["tweetResult"]["result"]
    note = node["note_tweet"]["note_tweet_results"]["result"]
    runs = text_runs(note["text"], note["entity_set"], richtext_tags=note["richtext"]["richtext_tags"])
    assert TextRun("2027年1月7日(木)", styles=("bold",)) in runs
    assert TextRun("https://youtu.be/9MXzPz2cOYk", "https://youtu.be/9MXzPz2cOYk") in runs
    assert TextRun("#浦和希", "https://x.com/hashtag/%E6%B5%A6%E5%92%8C%E5%B8%8C") in runs
    assert "".join(run.text for run in runs) == note["text"].replace(
        "https://t.co/K2GIQtykKh", "https://youtu.be/9MXzPz2cOYk"
    )
