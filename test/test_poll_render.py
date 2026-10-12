"""投票（poll）渲染：lib 只出数据，表格文案由渲染层按**用户语言**拼。

协议（双端冻结，见计划文件）：

- lib 在正文里 poll 的原位置放独立一行 ``{{poll:N}}``（引用块内是 ``> {{poll:N}}``），
  N = ``result.polls`` 里的索引（按占位符出现顺序从 0 递增）；
- bot 在 ``build_rich_markdown`` 里 ``rich_content()`` 之后、一切结构处理之前替换；
- ``polls`` 为空 → 跳过替换（老缓存/普通帖子零变化）；索引越界 → 整行删除 + warning
  （**占位符字面量绝不泄漏**）；带 ``> `` 前缀时替换产物每行都加前缀。

测试数据：优先用 lib 真正的 ``Poll`` / ``PollOption`` 构造（协议已落地）；lib 不可用时
退回 ``SimpleNamespace``（字段名相同）—— ``_render_poll`` 对两者都吃（``getattr``）。
"""

import types

import pytest

from plugins.helpers import _render_poll, build_rich_markdown, build_rich_markdown_by_str, replace_poll_placeholders

try:  # lib 侧的 Poll 类（phase1）；没有就退回等价 duck-type
    from parsehub.types import Poll, PollOption

    def _poll(options, **over):
        return Poll(options=[PollOption(text=text, count=count) for text, count in options], **over)

except ImportError:  # pragma: no cover - lib 未同步时

    def _poll(options, **over):
        return types.SimpleNamespace(
            options=[types.SimpleNamespace(text=text, count=count) for text, count in options],
            total_votes=over.get("total_votes"),
            voters=over.get("voters"),
            title=over.get("title", ""),
            is_finished=over.get("is_finished", False),
        )


#: 选项带票数 → 三列（twitter / threads / linux.do 的常规形态）
OFFICIAL_POLL = _poll([("青い", 51), ("赤い", 49)], total_votes=100)

#: 官方数据 + linux.do 头部（title + voters 组合）
LINUXDO_POLL = _poll(
    [("还在用a/感觉良好", 71), ("没用过a/", 31), ("一直觉得a/比o/恶心", 80)],
    total_votes=222,
    voters=222,
    title="你被封过号吗",
)

#: youtube：匿名拿不到单项票数，只有选项与总票数 → 单列 + 「投票 · 共 N 票」头部
YOUTUBE_POLL = _poll([("选项A", None), ("选项B", None)], total_votes=1_800_000)


def _config():
    return types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False)


def _parse_result(content, polls, **overrides):
    fields = {
        "title": "タイトル",
        "content": content,
        "raw_url": "https://x.com/a/status/1",
        "author_name": "作者",
        "author_handle": "handle",
        "author_url": "",
        "published_at": None,
        "view_count": None,
        "like_count": None,
        "tags": [],
        "hashtags": [],
        "platform": None,
        "polls": polls,
        "is_sensitive": False,
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


# ── ① 三列表格 + 语言切换 ─────────────────────────────────────────────


def test_three_column_table_in_chinese():
    """官方数据 → 「选项 / 票数 / 占比」三列, 占比按总票数算。"""
    md = _render_poll(OFFICIAL_POLL, "zh-hans")
    assert md == (
        "| 选项 | 票数 | 占比 |\n"
        "| --- | --- | --- |\n"
        "| 青い | 51 | 51% |\n"
        "| 赤い | 49 | 49% |"
    )


def test_three_column_table_follows_the_user_language():
    """同一份数据跟着语言换表头 —— 英语用户看 Option/Votes/Share, 不落中文。"""
    assert _render_poll(OFFICIAL_POLL, "en-us") == (
        "| Option | Votes | Share |\n"
        "| --- | --- | --- |\n"
        "| 青い | 51 | 51% |\n"
        "| 赤い | 49 | 49% |"
    )
    # 另有几种语言顺带钉一下 (抽样: 日语/韩语/俄语) —— 译文表在计划里冻结
    assert "| 選択肢 | 票数 | 割合 |" in _render_poll(OFFICIAL_POLL, "ja-jp")
    assert "| 항목 | 표 | 비율 |" in _render_poll(OFFICIAL_POLL, "ko-kr")
    assert "| Вариант | Голоса | Доля |" in _render_poll(OFFICIAL_POLL, "ru-ru")


def test_percent_counts_voters_as_the_base_when_present():
    """有参与人数时占比按**人数**算 (linux.do 的旧口径), 没有才按选项票数求和。"""
    md = _render_poll(LINUXDO_POLL, "zh-hans")
    assert "| 还在用a/感觉良好 | 71 | 32% |" in md  # 71/222 = 31.98% → 32%
    assert "| 一直觉得a/比o/恶心 | 80 | 36% |" in md  # 80/222 = 36.03% → 36%


def test_zero_total_does_not_crash():
    """一票都还没有: 占比记 0%, 不抛错。"""
    md = _render_poll(_poll([("A", 0), ("B", 0)], total_votes=0), "zh-hans")
    assert "| A | 0 | 0% |" in md
    assert "| B | 0 | 0% |" in md


def test_option_text_with_pipe_is_escaped():
    """选项文案里的 ``|`` 必须转义, 否则会把表格列切断。"""
    md = _render_poll(_poll([("A | B", 1)], total_votes=1), "zh-hans")
    assert "| A \\| B | 1 | 100% |" in md


# ── ② 引用块内的占位符 ─────────────────────────────────────────────────


def test_placeholder_inside_a_quote_block_gets_the_prefix_on_every_line():
    """``> {{poll:N}}`` → 替换产物每行带 ``> `` (空行写 ``>``), 表格行**不包** <i>。"""
    content = "引用正文\n\n> {{poll:0}}"
    out = replace_poll_placeholders(content, [OFFICIAL_POLL], "zh-hans")
    assert "{{poll:0}}" not in out, "占位符不能泄漏"
    lines = [line for line in out.split("\n") if line.startswith(">")]
    assert "> | 选项 | 票数 | 占比 |" in lines
    assert "> | --- | --- | --- |" in lines
    assert "> | 青い | 51 | 51% |" in lines
    assert "<i>" not in out, "表格行包 <i> 会让服务端不再认成表格"


def test_quote_block_placeholder_survives_build_rich_markdown():
    """全链路: 引用块里的占位符在 build_rich_markdown 里被替换、前缀保留。"""
    pr = _parse_result("正文\n\n> 引用文字\n> {{poll:0}}", [OFFICIAL_POLL])
    md = build_rich_markdown(pr, config=_config(), lang="zh-hans")
    assert "{{poll:" not in md
    assert "> | 选项 | 票数 | 占比 |" in md
    assert "> | 青い | 51 | 51% |" in md


# ── ③ youtube 单列 + 头部 ──────────────────────────────────────────────


def test_youtube_single_column_with_total_header():
    """youtube 匿名只有选项与总票数 → 「投票 · 共 N 票」+ 单列表格 (总票数带千分位)。"""
    md = _render_poll(YOUTUBE_POLL, "zh-hans")
    assert md == (
        "投票 · 共 1,800,000 票\n"
        "\n"
        "| 选项 |\n"
        "| --- |\n"
        "| 选项A |\n"
        "| 选项B |"
    )
    en = _render_poll(YOUTUBE_POLL, "en-us")
    assert en.startswith("Poll · 1,800,000 votes total\n\n| Option |\n| --- |")
    assert "| 选项A |" in en, "选项文案不翻译 (是用户内容)"


def test_youtube_without_total_votes_has_no_header():
    """总票数为 None/0 时不显示头部 —— 与 lib 旧形态一致 (只有「投票」两个字也不显示)。"""
    md = _render_poll(_poll([("A", None)], total_votes=None), "zh-hans")
    assert md == "| 选项 |\n| --- |\n| A |"


# ── ④ linux.do 头部 ────────────────────────────────────────────────────


def test_linuxdo_header_combines_title_and_voters():
    md = _render_poll(LINUXDO_POLL, "zh-hans")
    assert md.startswith("📊 投票（「你被封过号吗」 · 222 人参与）\n\n| 选项 | 票数 | 占比 |")
    assert "📊 Poll（\"你被封过号吗\" · 222 voters）" not in md  # 中文就用中文形态


def test_linuxdo_header_is_localized():
    """linux.do 头部的三段 (📊 投票 / 「title」 / N 人参与 / 括号) 都走 i18n。"""
    md = _render_poll(LINUXDO_POLL, "en-us")
    assert md.startswith('📊 Poll("你被封过号吗" · 222 voters)\n\n| Option | Votes | Share |')


def test_linuxdo_header_without_title():
    """没有标题时只拼「N 人参与」; 人数为 0 时连它也没有 → 只剩「📊 投票」。"""
    only_voters = _render_poll(_poll([("A", 1)], total_votes=1, voters=5), "zh-hans")
    assert only_voters.startswith("📊 投票（5 人参与）")
    bare = _render_poll(_poll([("A", 1)], total_votes=1, voters=0), "zh-hans")
    assert bare.startswith("📊 投票\n\n| 选项 | 票数 | 占比 |")


def test_linuxdo_legacy_degrade_has_no_header():
    """老站点退化 (全 None + 无 voters): 单列表格 + 无头部, 不显示假的 0。"""
    md = _render_poll(_poll([("选项一", None), ("选项二", None)], total_votes=None, voters=None), "zh-hans")
    assert md == "| 选项 |\n| --- |\n| 选项一 |\n| 选项二 |"
    assert "人参与" not in md


# ── ⑤ 越界 / 空数据 ────────────────────────────────────────────────────


def test_out_of_range_index_deletes_the_line():
    """索引越界 → 整行删除 (占位符**绝不泄漏**), 并留一条 warning。"""
    out = replace_poll_placeholders("前文\n{{poll:7}}\n后文", [OFFICIAL_POLL], "zh-hans")
    assert out == "前文\n后文"
    assert "{{poll" not in out


class _LogCapture:
    """抓 loguru 的 warning (项目用 loguru, pytest 的 caplog 抓不到)。"""

    def __init__(self):
        self.messages: list[str] = []
        self._id = None

    def __enter__(self):
        from log import logger

        self._id = logger.add(lambda message: self.messages.append(str(message)), level="WARNING")
        return self.messages

    def __exit__(self, *exc):
        from log import logger

        logger.remove(self._id)
        return False


def test_out_of_range_index_logs_a_warning():
    with _LogCapture() as logs:
        replace_poll_placeholders("{{poll:9}}", [OFFICIAL_POLL], "zh-hans")
    assert any("越界" in line for line in logs), logs


def test_empty_poll_data_deletes_the_line():
    """命中但数据为空 (选项列表空) → 同样整行删除, 不留空段。"""
    out = replace_poll_placeholders("前文\n{{poll:0}}\n后文", [_poll([], total_votes=0)], "zh-hans")
    assert out == "前文\n后文"


# ── ⑥ 空 polls / 非占位符 → 零变化 ─────────────────────────────────────


def test_no_polls_leaves_content_untouched():
    """``polls=[]`` (老缓存/普通帖子) → 内容**逐字**不变。"""
    content = "正文\n\n{{poll:0}}\n\n结尾"
    assert replace_poll_placeholders(content, [], "zh-hans") == content
    assert replace_poll_placeholders(content, None, "zh-hans") == content


def test_non_placeholder_lines_are_untouched():
    """不是占位符的 ``{{poll:...}}`` 形态 (缺花括号/带空格/非数字) 原样保留。"""
    for line in ("{{poll:abc}}", " {{poll:0}}", "{{poll:0}} ", "x {{poll:0}}", "{{ poll:0 }}"):
        assert replace_poll_placeholders(line, [OFFICIAL_POLL], "zh-hans") == line
    assert replace_poll_placeholders("", [OFFICIAL_POLL], "zh-hans") == ""


def test_ordinary_content_is_byte_identical():
    """没有占位符的普通正文: 替换后与输入逐字相同。"""
    content = "# 标题\n\n正文 **加粗** #tag\n\n> 引用\n> 引用二"
    assert replace_poll_placeholders(content, [OFFICIAL_POLL], "zh-hans") == content


# ── 两条渲染路径 (现场 / 缓存) 都要接上 ─────────────────────────────────


def test_build_rich_markdown_replaces_placeholders():
    pr = _parse_result("正文\n\n{{poll:0}}", [OFFICIAL_POLL])
    md = build_rich_markdown(pr, config=_config(), lang="en-us")
    assert "| Option | Votes | Share |" in md
    assert "{{poll" not in md


def test_build_rich_markdown_without_polls_keeps_legacy_content():
    """老结果对象 (没有 polls 属性) → 行为与改动前一致 (占位符形态的文本原样留着)。"""
    bare = types.SimpleNamespace(
        title="",
        content="正文",
        raw_url="https://x.com/a/status/1",
        platform=None,
        hide_title=False,
    )
    md = build_rich_markdown(bare, config=_config(), lang="zh-hans")
    assert "正文" in md


def test_cached_path_renders_polls_too():
    """缓存路径 (``_RichFields``) 也要能渲染 —— 否则"第二次发同一条链接"表格消失。"""
    live = build_rich_markdown(
        _parse_result("正文\n\n{{poll:0}}", [OFFICIAL_POLL]), config=_config(), lang="en-us"
    )
    cached = build_rich_markdown_by_str(
        "タイトル",
        "正文\n\n{{poll:0}}",
        "https://x.com/a/status/1",
        config=_config(),
        lang="en-us",
        author_name="作者",
        author_handle="handle",
        polls=[OFFICIAL_POLL],
    )
    assert "| Option | Votes | Share |" in cached
    assert live == cached, "现场与缓存两条路径必须逐字一致"


def test_cached_path_accepts_cache_dicts():
    """缓存里存的是 dict 列表 (``asdict(Poll)`` 形态) —— 渲染层同样要吃。"""
    as_dict = {
        "options": [{"text": "青い", "count": 51}, {"text": "赤い", "count": 49}],
        "total_votes": 100,
        "voters": None,
        "title": "",
        "is_finished": False,
    }
    md = build_rich_markdown_by_str(
        "",
        "{{poll:0}}",
        "https://x.com/a/status/1",
        config=_config(),
        lang="ja-jp",
        polls=[as_dict],
    )
    assert "| 選択肢 | 票数 | 割合 |" in md
    assert "| 青い | 51 | 51% |" in md


# ── 缓存写读闭环 ───────────────────────────────────────────────────────


def test_rich_cache_entry_carries_polls():
    """写缓存: ``rich_cache_entry`` 必须把 polls 存成 dict 列表 (字段名与 lib asdict 一致)。"""
    from plugins.parse.inline_rich import rich_cache_entry

    result = _parse_result("正文\n\n{{poll:0}}", [OFFICIAL_POLL])
    entry = rich_cache_entry(result, [])
    polls = entry.parse_result.polls
    assert len(polls) == 1
    assert polls[0]["total_votes"] == 100
    assert polls[0]["options"] == [{"text": "青い", "count": 51}, {"text": "赤い", "count": 49}]
    assert polls[0]["voters"] is None
    assert polls[0]["title"] == ""
    assert polls[0]["is_finished"] is False


def test_cache_entry_without_polls_defaults_to_empty():
    """没有 polls 的结果 (老对象) → 缓存条目里是空列表, 不炸。"""
    from plugins.parse.inline_rich import rich_cache_entry

    bare = types.SimpleNamespace(
        title="t", content="c", author_name="a", is_sensitive=False,
        author_handle="", author_url="", published_at=None,
        view_count=None, like_count=None, tags=None,
    )
    assert rich_cache_entry(bare, []).parse_result.polls == []


def test_cached_render_roundtrip_from_cache_entry():
    """写缓存 → 读缓存 → 渲染: 表格还在 (缓存命中路径的端到端)。"""
    from plugins.parse.inline_rich import build_cached_rich_content, rich_cache_entry
    from services.cache import CacheEntry, CacheParseResult

    result = _parse_result("正文\n\n{{poll:0}}\n\n> 引用\n> {{poll:1}}", [OFFICIAL_POLL, YOUTUBE_POLL])
    entry = rich_cache_entry(result, [])
    # 模拟缓存往返 (Redis 里存的是 JSON)
    entry = CacheEntry.model_validate(
        CacheEntry(
            parse_result=CacheParseResult.model_validate(entry.parse_result.model_dump(mode="json"))
        ).model_dump(mode="json")
    )
    markdown, _media, _blocks = build_cached_rich_content(
        entry, "https://x.com/a/status/1", lang="ja-jp", config=_config()
    )
    assert "{{poll" not in markdown
    assert "| 選択肢 | 票数 | 割合 |" in markdown
    assert "> 投票 · 合計 1,800,000 票" in markdown, markdown
    assert "> | 選択肢 |" in markdown


def test_old_cache_entry_without_polls_renders_the_legacy_way():
    """**旧缓存** (没有 polls 字段) → 不替换、不报错, 行为不变。"""
    from services.cache import CacheEntry, CacheParseResult

    entry = CacheEntry(parse_result=CacheParseResult(title="", content="正文 {{poll:0}}"))
    assert entry.parse_result.polls == []
    from plugins.parse.inline_rich import build_cached_rich_content

    markdown, _media, _blocks = build_cached_rich_content(
        entry, "https://x.com/a/status/1", lang="zh-hans", config=_config()
    )
    # 旧缓存里本来就不会有占位符 (lib 新版才有); 内容原样透传, 不炸
    assert "正文" in markdown


# ── 老 caption 路径 (raw/zip) 同样不泄漏 ───────────────────────────────


def test_caption_path_replaces_placeholders():
    from plugins.helpers import build_caption_by_str

    caption = build_caption_by_str(
        "标题",
        "正文\n{{poll:0}}",
        "https://x.com/a/status/1",
        lang="zh-hans",
        hide_source=True,
        polls=[OFFICIAL_POLL],
    )
    assert "{{poll" not in caption
    assert "| 选项 | 票数 | 占比 |" in caption


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
