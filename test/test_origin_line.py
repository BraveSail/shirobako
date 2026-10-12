"""归属行（``origin_line``）放在**标题与作者行之间**。

用户报「标题讨论那一行放标题和作者中间，现在插在正文和回复中间了」——
它原本由平台拼进正文，于是挤在引用块旁边（bgm 的图因此被插到引用块前面贴住）。
现在它是元信息的一部分：标题 → 归属行 → 作者行 → 内容。

**可翻译片段（2026-10-12）**：lib 不认识用户语言，归属行里的「讨论」以
``{{t:讨论}}`` 占位符输出，渲染层按 ``lang`` 替换（zh-hans 下即源文本）。
老缓存里的字面 ``» 讨论``（无占位符）逐字不变。
"""

import types

from plugins.helpers import build_rich_markdown, build_rich_markdown_by_str

#: lib 现行产出（可翻译片段形态）
ORIGIN = '**<a href="https://bgm.tv/group/fillgrids">补旧番</a>** » {{t:讨论}}'
#: 老缓存条目（字面文案，无占位符）
ORIGIN_LEGACY = '**<a href="https://bgm.tv/group/fillgrids">补旧番</a>** » 讨论'


class _Cfg(types.SimpleNamespace):
    hide_title = False
    hide_desc = False
    hide_source = True


def _result(**kw):
    result = types.SimpleNamespace(
        title="真的有人能看过3000+部番吗",
        content="> <i>引用块</i>\n\n本层正文",
        raw_url="https://bgm.tv/group/topic/472394#post_4062141",
        author_name="大夜宵",
        author_handle="dayexiao520",
        author_url="https://bgm.tv/user/dayexiao520",
        published_at=None,
        view_count=None,
        like_count=None,
        tags=None,
        platform=None,
        media=None,
        quote_roles=["quoted"],
        origin_line=ORIGIN,
    )
    for k, v in kw.items():
        setattr(result, k, v)
    return result


def _lines(md: str) -> list[str]:
    return [ln for ln in md.splitlines() if ln.strip()]


# ── 位置 ───────────────────────────────────────────────────────────────

def test_the_origin_line_sits_between_the_title_and_the_author():
    """**核心**: 标题 → 归属行 → 作者行（用户要求的位置）"""
    md = build_rich_markdown(_result(), config=_Cfg())
    lines = _lines(md)
    title_idx = next(i for i, ln in enumerate(lines) if ln.startswith("# "))
    origin_idx = next(i for i, ln in enumerate(lines) if "» 讨论" in ln)
    author_idx = next(i for i, ln in enumerate(lines) if "大夜宵" in ln)
    assert title_idx < origin_idx < author_idx, lines[:6]
    assert lines[title_idx] == "# 真的有人能看过3000+部番吗"
    assert lines[origin_idx] == ORIGIN_LEGACY, lines[origin_idx]


def test_the_translatable_fragment_is_expanded_for_the_user_language():
    """**核心（本地化）**: ``{{t:讨论}}`` 按 lang 替换 —— 英语用户看到 Discussion"""
    md = build_rich_markdown(_result(), config=_Cfg(), lang="en-us")
    assert "{{t:" not in md, md
    assert "» Discussion" in md, md


def test_a_legacy_origin_line_renders_verbatim():
    """老缓存里是字面文案（无占位符）—— 逐字不变，不报错"""
    md = build_rich_markdown(_result(origin_line=ORIGIN_LEGACY), config=_Cfg())
    assert "» 讨论" in md
    assert "{{t:" not in md


def test_the_origin_line_is_not_in_the_content_area():
    """**核心**: 归属行不能落在内容区（那里会挤到引用块旁边，把归位搅乱）"""
    md = build_rich_markdown(_result(), config=_Cfg())
    lines = _lines(md)
    quote_idx = next(i for i, ln in enumerate(lines) if ln.startswith("> "))
    origin_idx = next(i for i, ln in enumerate(lines) if "» 讨论" in ln)
    assert origin_idx < quote_idx, "归属行必须在引用块之前（元信息区）"


def test_without_an_origin_line_nothing_changes():
    """**回归**: 没有归属行的平台（twitter / linux.do / …）产物与以前逐字一致"""
    with_origin = build_rich_markdown(_result(origin_line=""), config=_Cfg())
    assert "» 讨论" not in with_origin
    lines = _lines(with_origin)
    assert lines[0].startswith("# ")
    assert "大夜宵" in lines[1], lines[:3]


def test_hide_title_still_drops_only_the_title():
    """隐藏标题时归属行与作者行照旧（它是元信息，不是标题）"""
    md = build_rich_markdown(_result(), config=_Cfg(hide_title=True))
    assert "# 真的有人能看过3000+部番吗" not in md
    assert "» 讨论" in md


# ── 缓存路径 ───────────────────────────────────────────────────────────

def test_the_cache_path_renders_the_same():
    """缓存路径（``build_rich_markdown_by_str``）也要带上归属行 —— 漏了就是

    "第一次发（现场）有归属行、第二次（缓存命中）没有"。
    片段替换同样要走这条路径（否则缓存命中时 ``{{t:讨论}}`` 字面泄漏）。
    """
    md = build_rich_markdown_by_str(
        "真的有人能看过3000+部番吗",
        "> <i>引用块</i>\n\n本层正文",
        "https://bgm.tv/group/topic/472394#post_4062141",
        config=_Cfg(),
        author_name="大夜宵",
        author_handle="dayexiao520",
        origin_line=ORIGIN,
    )
    lines = _lines(md)
    assert "» 讨论" in lines[1], lines[:4]
    assert "{{t:" not in md, md


def test_the_cache_path_without_the_argument_matches_before():
    """不传归属行时缓存路径的产物不变（老条目）"""
    md = build_rich_markdown_by_str(
        "标题", "正文", "https://x.com/a/status/1", config=_Cfg(), author_name="作者"
    )
    assert "» 讨论" not in md
    assert _lines(md)[0] == "# 标题"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
