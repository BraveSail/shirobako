"""bgm.tv **目录**（``/index/<id>``）—— 用户编纂的榜单 / 列表。

真实页面：``https://bgm.tv/index/77872``（「动画界史上最伟大的人物Top1000（视频榜单）」，
409 个人物条目 + 一条 7.5K 字简介）。

结构（实测 2026-10-21）::

    h1                          标题
    .line_detail                简介（作者 + 说明，含 <br/> 与外链）
    div[id^=item_person…]       条目：h3 a（名称/链接）、span.badge_job（角色）、
                                span.tip（性别/生日…）、div.text（名次）
"""

import asyncio
from pathlib import Path

from parsehub.parsers.parser.bangumi import BangumiParser, _render_index_markdown
from parsehub.provider_api.bangumi import BangumiIndex, _site_url

FIXTURE = Path(__file__).parent / "fixtures" / "bangumi_index_77872.html"
INDEX_ID = "77872"


def _index() -> BangumiIndex:
    return BangumiIndex._from_html(FIXTURE.read_text(encoding="utf-8"), INDEX_ID)


def _parse(index: BangumiIndex, monkeypatch):
    """跑真实的 ``_parse_index``，只把 provider 的联网调用换成 fixture 解析结果。"""

    async def _fake_parse(_url, proxy=None, cookie=None):
        return index

    monkeypatch.setattr(BangumiIndex, "parse", _fake_parse)
    return asyncio.run(BangumiParser()._parse_index(f"https://bgm.tv/index/{INDEX_ID}"))


# ---------------------------------------------------------------- 页面结构

def test_the_title_comes_from_h1():
    assert _index().title == "动画界史上最伟大的人物Top1000（视频榜单）"


def test_the_author_comes_from_the_user_link_in_the_summary_block():
    """页面上作者只有头像、没有名字文本 —— 用 ``/user/<标识>`` 里的标识"""
    assert _index().author_handle == "faumeplderr"


def test_every_entry_is_collected():
    """409 个条目（页面上 ``item_person…`` 节点数）"""
    assert len(_index().entries) == 409


def test_an_entry_carries_name_rank_jobs_and_info():
    first = _index().entries[0]
    assert first.name == "Émile Cohl"
    assert first.rank == "2"
    assert first.jobs == ["漫画家", "制作人员"]
    assert "生日 1857年1月4日" in first.info
    assert first.kind == "person"


def test_an_entry_link_is_absolute():
    """``/person/33229`` 要补成完整地址 —— 相对路径在消息里点开是 404"""
    assert _index().entries[0].url == "https://bgm.tv/person/33229"
    assert _site_url("/person/1") == "https://bgm.tv/person/1"
    assert _site_url("//lain.bgm.tv/x.jpg") == "https://lain.bgm.tv/x.jpg"
    assert _site_url("https://bgm.tv/person/2") == "https://bgm.tv/person/2"


# ---------------------------------------------------------------- 渲染

def test_the_body_is_summary_plus_a_ranked_list():
    md = _render_index_markdown(_index())
    assert "视频链接" in md, "简介要在"
    assert "\n\n---\n\n" in md, "简介与条目之间要分隔线"
    assert '<a href="https://bgm.tv/person/33229">Émile Cohl</a>' in md
    assert "**2.**" in md


def test_the_body_fits_the_rich_message_limit():
    """409 条 + 7.5K 字简介原始长度 38K > 富文本 32768 —— 必须按预算收，
    否则整条消息会被服务端拒掉（不是"发得长了点"）。"""
    md = _render_index_markdown(_index())
    assert len(md) < 32768, len(md)
    # 末尾的省略号说明"还有条目没列出来"
    assert md.rstrip().endswith("…")


# ---------------------------------------------------------------- 端到端

def test_the_parser_recognizes_index_urls():
    assert BangumiParser.__match__.startswith("^(http(s)?://)?(bgm|bangumi)\\.tv/")
    import re

    assert re.search(BangumiParser.__match__, "https://bgm.tv/index/77872")
    assert re.search(BangumiParser.__match__, "https://bangumi.tv/index/123")


def test_the_result_carries_title_author_and_body(monkeypatch):
    result = _parse(_index(), monkeypatch)
    assert result.title == "动画界史上最伟大的人物Top1000（视频榜单）"
    assert result.author_handle == "faumeplderr"
    assert result.markdown_content.startswith("视频链接")
    assert "Émile Cohl" in result.markdown_content
