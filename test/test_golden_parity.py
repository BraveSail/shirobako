"""Offline replay of the manifest's successful captures at the native Telegram boundary.

The original JSON is an unchanged audit reference, not a source document or a TL
snapshot. Bangumi replays the captured HTML; the other cases use small authored
source excerpts where no matching capture is checked in. The assertions below
pin visible text, order, and entities independently of the native serializer.

Intentional differences: native dates carry an epoch and display flags; source
paragraphs need no Markdown escapes or hard-break spaces, and literal X stars
remain visible rather than being interpreted as emphasis. Bangumi ranks are
individual linked paragraphs inside the fold. PTT pushes are independent quotes
in one disclosure, without the old Markdown-driven inner prose fold. Bilibili
photos use synthetic cached IDs: the historical text-only capture did not retain
media IDs. The excerpt fixtures do not claim to replay omitted source content.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from parsehub import ParseHub
from parsehub.parsers.parser.bangumi import BangumiParser
from parsehub.provider_api.bangumi import BangumiIndex
from parsehub.types import ImageFile, ImageRef, MultimediaParseResult, Platform
from parsehub.types.document import Block, Document, TextRun, paragraph
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.raw import types as tl
from pyrogram.raw.core import TLObject
from pyrogram.types import InputRichMessage

from plugins import helpers
from plugins.parse import inline_rich, rich_blocks
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG
from services.media import ProcessedMedia

GOLDEN = Path(__file__).with_name("golden")
SOURCE = GOLDEN.parents[1] / "lib" / "test" / "fixtures"
CONFIG = DEFAULT_SETTINGS_CONFIG.model_copy(update={"hide_source": False, "hide_title": False, "hide_desc": False})


def _load(case):
    return json.loads((GOLDEN / f"{case}.json").read_text(encoding="utf-8"))


def _find(value, kind):
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _find(item, kind)
    elif isinstance(value, TLObject):
        if isinstance(value, kind):
            yield value
        for key in value.__slots__:
            yield from _find(getattr(value, key, None), kind)


def _text(value):
    return "".join(node.text for node in _find(value, tl.TextPlain))


def _links(value):
    return [(_text(node), node.url) for node in _find(value, tl.TextUrl)]


def _shape(blocks, *types):
    assert tuple(map(type, blocks)) == types


def _footer(block, golden, label, *, date=None):
    assert isinstance(block, tl.PageBlockFooter)
    assert _links(block) == [(label, golden["url"])]
    dates = list(_find(block, tl.TextDate))
    assert [item.date for item in dates] == ([] if date is None else [date])
    if dates:
        assert dates[0].long_date and dates[0].short_time
    assert _text(block) == golden["blocks"][-1]["text"]


def _byline(block, visible, *, links=(), fixed=()):
    assert isinstance(block, tl.PageBlockParagraph)
    assert _text(block) == visible
    assert _links(block) == list(links)
    assert [_text(node) for node in _find(block, tl.TextFixed)] == list(fixed)
    assert len(list(_find(block, tl.TextBold))) == 1


def _result(url, platform, *body, date=None, topic=(), **fields):
    result = MultimediaParseResult(
        document=Document(body, topic=topic),
        content="Legacy text must not be rendered",
        published_at=datetime.fromtimestamp(date, UTC) if date is not None else None,
        **fields,
    )
    result.raw_url, result.platform = url, platform
    return result


def _wire(result, *, photo_ids=()):
    assert result.document is not None
    assert len(result.media or ()) == len(photo_ids)
    media_args = {}
    if photo_ids:
        processed = []
        for photo_id in photo_ids:
            file_id = FileId(
                file_type=FileType.PHOTO, dc_id=2, media_id=photo_id, access_hash=42, volume_id=0,
                local_id=0, thumbnail_source=ThumbnailSource.THUMBNAIL,
                thumbnail_file_type=FileType.PHOTO, thumbnail_size="x",
            ).encode()
            processed.append(ProcessedMedia(ImageFile(path=file_id, width=8, height=8)))
        _, placeholders, media_blocks, _, _, _ = inline_rich.build_rich_media(result.media, processed)
        media_args = {"media_placeholders": placeholders, "media_blocks": media_blocks}

    async def encode():
        blocks = inline_rich.build_delivery_blocks(result, config=CONFIG, lang="zh-hans", **media_args)
        return await InputRichMessage(blocks=blocks).write(client=None)

    stream = BytesIO(asyncio.run(encode()).write())
    decoded = TLObject.read(stream)
    assert isinstance(decoded, tl.InputRichMessage) and stream.read() == b""
    return decoded.blocks


def _bgm_index(golden, monkeypatch):
    url = golden["url"]
    html = (SOURCE / "bangumi_index_77872.html").read_text(encoding="utf-8")
    replay = AsyncMock(return_value=BangumiIndex._from_html(html, "77872"))
    monkeypatch.setattr(BangumiIndex, "parse", replay)
    result = asyncio.run(BangumiParser().parse(url))
    replay.assert_awaited_once_with(url, proxy=None, cookie=None)
    blocks = _wire(result)
    assert isinstance(blocks[0], tl.PageBlockHeading1)
    assert _text(blocks[0]) == "动画界史上最伟大的人物Top1000（视频榜单）"
    _byline(blocks[1], "@faumeplderr", links=(("@faumeplderr", "https://bgm.tv/user/faumeplderr"),))
    assert isinstance(blocks[2], tl.PageBlockDivider) and isinstance(blocks[-2], tl.PageBlockDivider)
    assert ("https://b23.tv/G1VPROs", "https://b23.tv/G1VPROs") in _links(blocks[3:-2])
    (fold,) = _find(blocks[3:-2], tl.PageBlockDetails)
    assert _text(fold.title) == "展开全文" and not fold.open
    ranked = [p for p in fold.blocks if _text(p).startswith(("2. · Émile Cohl", "3. · Winsor McCay"))]
    assert [_text(list(_find(p, tl.TextBold))) for p in ranked] == ["2.", "3."]
    assert [_links(p) for p in ranked] == [
        [("Émile Cohl", "https://bgm.tv/person/33229")], [("Winsor McCay", "https://bgm.tv/person/2615")],
    ]
    _footer(blocks[-1], golden, "来源（Bangumi）")


def _bilibili_opus(golden, _monkeypatch):
    url = golden["url"]
    images = [ImageRef(url=f"https://images.example/opus-chart-{i}.jpg") for i in range(4)]
    blocks = _wire(_result(
        url, Platform.BILIBILI,
        paragraph("七月新番《魔法少女奈叶 EXCEEDS Gun Blaze Vengeance》首卷首周销量为2613。"),
        paragraph("上周动画销量周榜与近期新番销量统计（数据来源Oricon）："),
        Block("collage", children=tuple(Block("media", index=i, url=ref.url) for i, ref in enumerate(images))),
        paragraph((TextRun("封面图片引用自上周本土作品动画映像单卷周冠 怪化猫 第三章 官方网站 "),
                   TextRun("https://www.mononoke-movie.com/", "https://www.mononoke-movie.com/"))),
        title="2026年10月19日-10月25日日本动画销量情况", author_name="名作之壁吧", author_handle="2859372",
        author_url="https://space.bilibili.com/2859372", date=1793546637, like_count=391, media=images,
    ), photo_ids=(41, 42, 43, 44))
    title, author, _, first, chart_intro, gallery, credits, _, footer = blocks
    _shape(blocks, tl.PageBlockHeading1, tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockParagraph,
           tl.PageBlockParagraph, tl.PageBlockCollage, tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockFooter)
    assert _text(title) == golden["blocks"][0]["text"]
    _byline(author, "名作之壁吧 @2859372", links=(("名作之壁吧", "https://space.bilibili.com/2859372"),),
            fixed=("@2859372",))
    assert _text(first) == golden["blocks"][3]["text"]
    legacy_tail = golden["blocks"][-3]["blocks"]
    assert _text(chart_intro) == legacy_tail[1]["text"]
    assert [photo.photo_id for photo in _find(gallery, tl.PageBlockPhoto)] == [41, 42, 43, 44]
    assert _links(credits) == [("https://www.mononoke-movie.com/", "https://www.mononoke-movie.com/")]
    assert _text(credits) == legacy_tail[-1]["text"]
    _footer(footer, golden, "来源（Bilibili）", date=1793546637)


def _linuxdo(golden, _monkeypatch):
    url = golden["url"]
    blocks = _wire(_result(
        url, Platform.LINUXDO, paragraph("哥几个这下真动摇了，当时配的时候想的是不管涨跌自用无所谓"),
        title="这下真动摇了，手里的5090要出吗", author_handle="Donquixote", author_url="https://linux.do/u/Donquixote",
        position_label="#1", tags=["纯水", "人工智能", "显卡"], date=1792741936, view_count=1871, like_count=6,
    ))
    title, author, _, body, tags, _, footer = blocks
    _shape(blocks, tl.PageBlockHeading1, tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockParagraph,
           tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockFooter)
    assert [_text(title), _text(body), _text(tags)] == [golden["blocks"][i]["text"] for i in (0, 3, 5)]
    _byline(author, "@Donquixote · #1", links=(("@Donquixote", "https://linux.do/u/Donquixote"),))
    assert _links(tags) == [
        ("#纯水", "https://linux.do/tag/%E7%BA%AF%E6%B0%B4"),
        ("#人工智能", "https://linux.do/tag/%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD"),
        ("#显卡", "https://linux.do/tag/%E6%98%BE%E5%8D%A1"),
    ]
    _footer(footer, golden, "来源（linux.do）", date=1792741936)


def _ptt(golden, _monkeypatch):
    url = golden["url"]
    image = "https://i.imgur.com/lp3lXzL.jpeg"
    blocks = _wire(_result(
        url, Platform.PTT, paragraph((TextRun(image, image),)),
        paragraph("這一波滿傷的吧，至少很多有廣告價值的V應該都不敢玩了"), paragraph((TextRun(url, url),)),
        Block("details", runs=(TextRun("推文（309 條）"),), independent=True, children=(
            Block("quote", children=(paragraph("推 Teng0615: 感覺有些人刪掉PC版後 就順勢退坑了@@"),)),
            Block("quote", children=(paragraph("→ Muilie: 電子菜花事件"),)),
            Block("quote", children=(paragraph("推 fash5896: 20抽+素材"),)),
        )),
        title="[情報] 妮姬PC啟動器安全事件官方補償", author_name="RF", author_handle="stevenchiang",
        tags=["C_Chat"], date=1793834463, reply_count=309,
    ))
    title, author, _, image_link, body, article_link, pushes, tags, _, footer = blocks
    _shape(blocks, tl.PageBlockHeading1, tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockParagraph,
           tl.PageBlockParagraph, tl.PageBlockParagraph, tl.PageBlockDetails, tl.PageBlockParagraph,
           tl.PageBlockDivider, tl.PageBlockFooter)
    assert [_text(title), _text(body), _text(tags)] == [golden["blocks"][i]["text"] for i in (0, 4, 7)]
    _byline(author, "RF @stevenchiang")
    assert _links((image_link, article_link)) == [(image, image), (url, url)]
    legacy_pushes = golden["blocks"][6]
    assert _text(pushes.title) == "推文（309 條）" and not pushes.open
    _shape(pushes.blocks, *(tl.PageBlockBlockquoteBlocks,) * 3)
    for quote in pushes.blocks:
        _shape(quote.blocks, tl.PageBlockParagraph)
    assert [_text(quote) for quote in pushes.blocks] == [
        legacy_pushes["blocks"][0]["blocks"][0]["text"],
        *(quote["blocks"][0]["text"] for quote in legacy_pushes["blocks"][1]["blocks"][:2]),
    ]
    _footer(footer, golden, "来源（PTT）", date=1793834463)


def _threads(golden, _monkeypatch):
    url = golden["url"]
    profile = "https://www.threads.com/@snowmaple_official"
    topic = "https://www.threads.com/search?q=%E7%94%B3%E8%AB%8B%E5%AF%A9%E5%88%A4&serp_type=tags"
    blocks = _wire(_result(
        url, Platform.THREADS, paragraph("沒想到還能看到「申請審判」\n先分享再說"),
        author_name="雪楓Yuki", author_handle="snowmaple_official", author_url=profile,
        topic=(TextRun("申請審判", topic),), date=1792416784, like_count=8,
    ))
    author, _, body, _, footer = blocks
    _shape(blocks, tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockParagraph,
           tl.PageBlockDivider, tl.PageBlockFooter)
    _byline(author, golden["blocks"][0]["text"], links=(("雪楓Yuki", profile), ("申請審判", topic)),
            fixed=("@snowmaple_official",))
    assert _text(body) == golden["blocks"][2]["text"].replace("  \n", "\n")
    _footer(footer, golden, "来源（Threads）", date=1792416784)


def _tieba(golden, _monkeypatch):
    url = golden["url"]
    blocks = _wire(_result(
        url, Platform.TIEBA, paragraph("他那个所谓教学只说了超级基本的系统，实际打起来需要懂得干货是几乎没有"),
        title="玩了铁拳8demo的小人教学模式后发现原田是真能忽悠", author_name="塔塔卡次",
    ))
    title, author, _, body, _, footer = blocks
    _shape(blocks, tl.PageBlockHeading1, tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockParagraph,
           tl.PageBlockDivider, tl.PageBlockFooter)
    assert _text(title) == golden["blocks"][0]["text"]
    _byline(author, "塔塔卡次")
    assert _text(body) == golden["blocks"][3]["text"].partition("，然后")[0]
    _footer(footer, golden, "来源（贴吧）")


def _x_article(golden, _monkeypatch):
    url = golden["url"]
    profile = "https://x.com/TheFluke999"
    blocks = _wire(_result(
        url, Platform.TWITTER,
        Block("heading", (TextRun("一：如何区分奶龙和奶蛙", styles=("bold",)),), level=2),
        paragraph("1.奶龙（图一）身材矮小，密度极大，出没时通常带有地震的感觉。奶蛙（图二）身材高大，密度较大，出没时通常带有微弱笑声."),
        Block("heading", (TextRun("二：奶龙和奶蛙之间的关系", styles=("bold",)),), level=2),
        Block("heading", (TextRun("三：无论是奶龙还是奶蛙，都是不可亵渎与侵犯的！！！", styles=("bold",)),), level=2),
        title="奶龙和奶蛙如何区分以及二者之间的关系", author_name="Fluke | 侥幸哥", author_handle="TheFluke999",
        author_url=profile, date=1778464645, view_count=3693, like_count=14,
    ))
    title, author, _, h1, first, h2, h3, _, footer = blocks
    _shape(blocks, tl.PageBlockHeading1, tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockHeading2,
           tl.PageBlockParagraph, tl.PageBlockHeading2, tl.PageBlockHeading2, tl.PageBlockDivider, tl.PageBlockFooter)
    assert [_text(title), _text(first)] == [golden["blocks"][i]["text"] for i in (0, 4)]
    assert [_text(list(_find(h, tl.TextBold))) for h in (h1, h2, h3)] == [
        golden["blocks"][i]["text"] for i in (3, 9, 12)
    ]
    _byline(author, "Fluke | 侥幸哥 @TheFluke999", links=(("Fluke | 侥幸哥", profile),), fixed=("@TheFluke999",))
    _footer(footer, golden, "来源（Twitter）", date=1778464645)


def _x_status(golden, _monkeypatch):
    url = golden["url"]
    profile = "https://x.com/SugoiLITE"
    video = "https://x.com/spiritcircle_pr/status/2118267072325235109/video/1"
    blocks = _wire(_result(
        url, Platform.TWITTER,
        paragraph('*ICYMI: "Spirit Circle" TV Anime is confirmed, by the staff, '
                  'to be a ‘Complete Adaptation’ with 18 episodes planned.'),
        paragraph("The *final* episode count is still subjected to change, but somewhere between those of "
                  "1 Cour and 2 Cours, to delicate balance and amount of details required."),
        Block("quote", role="quoted", runs=(TextRun("Sugoi LITE", profile), TextRun(" "),
                                           TextRun("@SugoiLITE", styles=("code",))), children=(
            paragraph((TextRun('✅ Official Announcement: "Spirit Circle" TV Anime confirmed in production!\n\n'
                               '• Broadcasting starts December 2026\n\n'), TextRun(video, video))),
        )),
        author_name="Sugoi LITE", author_handle="SugoiLITE", author_url=profile,
        date=1793884009, view_count=11612, like_count=188,
    ))
    author, _, main, second, quote, _, footer = blocks
    _shape(blocks, tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockParagraph, tl.PageBlockParagraph,
           tl.PageBlockBlockquoteBlocks, tl.PageBlockDivider, tl.PageBlockFooter)
    _byline(author, "Sugoi LITE @SugoiLITE", links=(("Sugoi LITE", profile),), fixed=("@SugoiLITE",))
    assert _text(main) == golden["blocks"][2]["text"]
    assert _text(second) == golden["blocks"][3]["text"].replace("The final ", "The *final* ", 1)
    assert not list(_find((main, second), (tl.TextBold, tl.TextItalic)))
    _shape(quote.blocks, tl.PageBlockParagraph, tl.PageBlockDivider, tl.PageBlockParagraph)
    signer, _, announcement = quote.blocks
    assert _text(list(_find(signer, tl.TextItalic))) == "Sugoi LITE @SugoiLITE"
    assert _links(signer) == [("Sugoi LITE", profile)]
    assert _text(list(_find(announcement, tl.TextItalic))) == (
        '✅ Official Announcement: "Spirit Circle" TV Anime confirmed in production!\n\n'
        f'• Broadcasting starts December 2026\n\n{video}'
    )
    assert _links(announcement) == [(video, video)]
    _footer(footer, golden, "来源（Twitter）", date=1793884009)


_CASES = {
    "bgm_index": (Platform.BANGUMI, _bgm_index),
    "bilibili_opus": (Platform.BILIBILI, _bilibili_opus),
    "linuxdo": (Platform.LINUXDO, _linuxdo),
    "ptt": (Platform.PTT, _ptt),
    "threads": (Platform.THREADS, _threads),
    "tieba": (Platform.TIEBA, _tieba),
    "x_article": (Platform.TWITTER, _x_article),
    "x_status": (Platform.TWITTER, _x_status),
}


@pytest.mark.parametrize("case", tuple(_CASES))
def test_native_delivery_matches_historical_semantics(case, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("native delivery entered legacy Markdown rendering")

    monkeypatch.setattr(helpers, "build_rich_markdown", forbidden)
    monkeypatch.setattr(rich_blocks, "markdown_to_blocks", forbidden)
    monkeypatch.setattr(rich_blocks, "parse_inline", forbidden)
    platform, replay = _CASES[case]
    golden = _load(case)
    assert ParseHub().get_platform(golden["url"]) is platform
    replay(golden, monkeypatch)


def test_user_urls_are_a_corpus_not_render_snapshots():
    manifest = _load("manifest")
    assert {case for case, entry in manifest.items() if entry["status"] == "ok"} == _CASES.keys()
    corpus = _load("user_urls")
    assert corpus.keys() == {"all", "by_host"}
    entries = corpus["all"]
    by_host = [entry for rows in corpus["by_host"].values() for entry in rows]
    for entry in entries:
        assert entry.keys() == {"url", "count"}
        assert isinstance(entry["url"], str) and entry["url"].startswith(("http://", "https://"))
        assert type(entry["count"]) is int and entry["count"] > 0
    counts = {entry["url"]: entry["count"] for entry in entries}
    assert len(counts) == len(entries) == 646
    assert len(by_host) == len(entries) and {entry["url"]: entry["count"] for entry in by_host} == counts

    # Tieba was a separately collected golden; only the known corpus examples
    # are routed, not unrelated historical sites or pasted shell/Markdown text.
    hub = ParseHub()
    variants = {
        "bilibili_opus": ("https://t.bilibili.com/1265762239826100230?spm_id_from=333.1365.list.card_time.click", 3),
        "linuxdo": ("https://linux.do/t/topic/3064284/6?u=libc.so.6", 3),
    }
    expected_counts = {"bgm_index": 5, "ptt": 3, "threads": 3, "x_article": 3, "x_status": 3}
    assert variants.keys() | expected_counts.keys() == _CASES.keys() - {"tieba"}
    for case, (platform, _) in _CASES.items():
        if case == "tieba":
            continue
        source = _load(case)["url"]
        url, count = variants[case] if case in variants else (source, expected_counts[case])
        assert counts[url] == count
        assert hub.get_platform(url) is platform
        golden_path, corpus_path = urlsplit(source).path, urlsplit(url).path
        assert corpus_path == golden_path or (case == "linuxdo" and corpus_path == f"{golden_path}/6")
