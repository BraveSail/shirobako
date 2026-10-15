"""长推文的局部加粗 (richtext_tags) 要还原成 Markdown 粗体标记。

背景（2026-10-13 用户报障）: 一条带粗体段落的长推文，解析后**没有任何样式**。
数据其实一直在响应里 —— `note_tweet...result.richtext.richtext_tags`:

    [{"from_index": 87, "to_index": 156, "richtext_types": ["Bold"]}, ...]

以前代码完全没读这个字段（连一次 grep 都命中不到），所以网页上带粗体的长文
解析出来是纯文本。

**索引纪律**: `from_index` / `to_index` 指向 `note_tweet.text` 的**原始区间**，
必须在 `_restore_short_urls` 展开短链**之前**应用 —— 展开会改变文本长度，
让后面的区间全部漂移。这里用一个带短链区间后的加粗案例钉死顺序。
"""

from parsehub.provider_api.twitter import Twitter

# 真实推文 (https://x.com/MaxForAI/status/2108035230003274189) 的前两段 +
# 一个合成区间 —— 保证测试不依赖联网、也不依赖那条推文还在。
TEXT = "开头普通文字。\n\n这一段是要加粗的内容。\n\n结尾普通文字。"


def _legacy(text: str = TEXT, **extra) -> dict:
    return {"full_text": text, "entities": {"urls": [], "media": []}, **extra}


def _note(text: str = TEXT, richtext: dict | None = None, **extra) -> dict:
    result: dict = {"id": "1", "text": text, "entity_set": {"urls": []}}
    if richtext is not None:
        result["richtext"] = richtext
    result.update(extra)
    return {"is_expandable": True, "note_tweet_results": {"result": result}}


def _wrapped(**tweet_fields) -> dict:
    tweet = {"rest_id": "1", "legacy": _legacy()}
    tweet.update(tweet_fields)
    return {"data": {"tweetResult": {"result": {"__typename": "Tweet", "tweet": tweet}}}}


def _rich(*tags: tuple[int, int, list[str]]) -> dict:
    return {
        "richtext_tags": [
            {"from_index": s, "to_index": e, "richtext_types": types} for s, e, types in tags
        ]
    }


def _parse(raw: dict) -> str:
    return Twitter().parse(raw).full_text


# ── 主路径 ────────────────────────────────────────────────


def test_a_bold_span_becomes_markdown():
    """核心用例: 一个 Bold 区间 → 那段被 ** 包住"""
    start = TEXT.index("这一段是要加粗的内容。")
    end = start + len("这一段是要加粗的内容。")
    got = _parse(_wrapped(note_tweet=_note(richtext=_rich((start, end, ["Bold"])))))
    assert "**这一段是要加粗的内容。**" in got
    # 区间外的文字不受影响
    assert "开头普通文字。" in got and "结尾普通文字。" in got


def test_multiple_bold_spans_all_apply():
    """多个区间各自独立 —— 常见于真实长文 (那条推文有 9 段)"""
    s1, e1 = TEXT.index("开头普通文字。"), TEXT.index("开头普通文字。") + len("开头普通文字。")
    s2, e2 = TEXT.index("结尾普通文字。"), TEXT.index("结尾普通文字。") + len("结尾普通文字。")
    got = _parse(_wrapped(note_tweet=_note(richtext=_rich((s1, e1, ["Bold"]), (s2, e2, ["Bold"])))))
    assert "**开头普通文字。**" in got
    assert "**结尾普通文字。**" in got
    assert "**这一段" not in got or "这一段是要加粗的内容。**" not in got


def test_no_richtext_leaves_the_text_untouched():
    """普通长推文 (响应里没有 richtext) 输出逐字不变 —— 回归防线"""
    got = _parse(_wrapped(note_tweet=_note()))
    assert got == TEXT


def test_an_empty_richtext_tag_list_is_a_noop():
    got = _parse(_wrapped(note_tweet=_note(richtext={"richtext_tags": []})))
    assert got == TEXT


# ── 边界与顺序 ─────────────────────────────────────────────


def test_out_of_range_spans_are_skipped_not_crashed():
    """越界区间宁可少裹一段, 不能让整条解析炸掉"""
    got = _parse(
        _wrapped(
            note_tweet=_note(
                richtext=_rich(
                    (5, len(TEXT) + 50, ["Bold"]),  # to 超长 → clamp 到结尾
                    (len(TEXT) + 10, len(TEXT) + 20, ["Bold"]),  # from 越界 → skip
                )
            )
        )
    )
    # 第一个区间 clamp 后从 index 5 包到结尾: "开头普通文**字。…结尾普通文字。**"
    assert got.startswith("开头普通文**")
    assert got.endswith("**")
    assert "**字。" in got  # index 5 = "字"


def test_short_url_expansion_after_bold_does_not_shift_spans():
    """短链展开发生在加粗之后 —— 展开的链接插在**已标记**的文本里, 不破坏区间。

    构造: 正文含 t.co 短链, 且短链**后面**有一段加粗。若顺序反了 (先展开再加粗),
    加粗区间会指向错误位置。

    ⚠️ 长推文的短链实体在 ``note_tweet...entity_set.urls`` (不是 legacy 层) ——
    解析时 url_entities 也优先取那一层, fixture 要放在同一层才符合真实形态。
    """
    text = "看这个 https://t.co/abc 然后这里是加粗内容。"
    start = text.index("然后这里是加粗内容。")
    end = start + len("然后这里是加粗内容。")
    raw = {"data": {"tweetResult": {"result": {"__typename": "Tweet", "tweet": {
        "rest_id": "1",
        "legacy": _legacy(text),
        "note_tweet": _note(
            text,
            richtext=_rich((start, end, ["Bold"])),
            entity_set={
                "urls": [{"url": "https://t.co/abc", "expanded_url": "https://example.com/a/very/long/url"}],
                "hashtags": [], "symbols": [], "user_mentions": [],
            },
        ),
    }}}}}
    got = Twitter().parse(raw).full_text
    assert "https://example.com/a/very/long/url" in got
    assert "**然后这里是加粗内容。**" in got


def test_italic_and_strikethrough_are_supported_by_the_mapping():
    """结构上支持多类型 (本次数据只有 Bold, 但映射表是全的)"""
    start = TEXT.index("这一段是要加粗的内容。")
    end = start + len("这一段是要加粗的内容。")
    got = _parse(_wrapped(note_tweet=_note(richtext=_rich((start, end, ["Italic"])))))
    assert "*这一段是要加粗的内容。*" in got


def test_unknown_types_are_ignored():
    """平台加了新类型 (如 Mention) 时跳过, 不产生坏标记"""
    start = TEXT.index("这一段是要加粗的内容。")
    end = start + len("这一段是要加粗的内容。")
    got = _parse(_wrapped(note_tweet=_note(richtext=_rich((start, end, ["Mention"])))))
    assert got == TEXT


def test_the_flat_shape_applies_richtext_too():
    """平铺形态 (字段直接在 result 上) 同样处理"""
    start = TEXT.index("这一段是要加粗的内容。")
    end = start + len("这一段是要加粗的内容。")
    result = {"rest_id": "1", "legacy": _legacy(), "note_tweet": _note(richtext=_rich((start, end, ["Bold"])))}
    raw = {"data": {"tweetResult": {"result": result}}}
    assert "**这一段是要加粗的内容。**" in _parse(raw)


def test_a_quoted_tweet_with_bold_keeps_it():
    """被引用推文递归走同一条解析 —— 它自己的加粗也要在"""
    start = TEXT.index("这一段是要加粗的内容。")
    end = start + len("这一段是要加粗的内容。")
    quoted_result = {
        "rest_id": "2",
        "legacy": _legacy(),
        "note_tweet": _note(richtext=_rich((start, end, ["Bold"]))),
    }
    tweet = {
        "rest_id": "1",
        "legacy": _legacy("正文", quoted_status_id_str="2"),
        "quoted_status_result": {"result": quoted_result},
    }
    raw = {"data": {"tweetResult": {"result": {"__typename": "Tweet", "tweet": tweet}}}}
    got = Twitter().parse(raw)
    assert got.quoted_status is not None
    assert "**这一段是要加粗的内容。**" in got.quoted_status.full_text


# ── UTF-16 索引（emoji 在前面时标记会整体左移）─────────────────────
#
# 用户报障「加粗偏掉了」(2026-10-15, x.com/chihara_anime/status/2110672031205511204):
# 该推文的 tag 是 [42:54], 但区间**前面有 5 个 emoji** ——
#
#   按 code point 切: '27年1月7日(木)毎週'   ← 丢了开头的 "20"，偏掉
#   按 UTF-16   切: '2027年1月7日(木)'      ← 正确
#
# X 的前端是 JS（字符串按 UTF-16 索引），而 Python 按 code point 索引；
# 每个非 BMP 字符在 UTF-16 占 2 个 unit ⇒ 差出的正是 emoji 个数。


def _utf16_len(s: str) -> int:
    """该字符串在 UTF-16 下的 code unit 数（非 BMP 字符算 2）。"""
    return len(s.encode("utf-16-le")) // 2


def test_emoji_before_the_span_does_not_shift_it():
    """**核心回归**: 区间前的 emoji 会让 UTF-16 索引与 code point 下标分叉。"""
    prefix = "放送日解禁!!!🐈‍⬛🐾\n"          # 含非 BMP 字符
    target = "2027年1月7日(木)"
    text = f"{prefix}{target}毎週木曜深夜0:26～"
    # 服务端给的是 **UTF-16** 偏移
    start = _utf16_len(prefix)
    end = start + _utf16_len(target)
    assert _utf16_len(prefix) != len(prefix), "前提: 该前缀的两种长度必须不同"

    got = _parse(_wrapped(note_tweet=_note(text, richtext=_rich((start, end, ["Bold"])))))
    assert f"**{target}**" in got
    assert "**27年" not in got, "不能把开头的 20 切掉（这正是原 bug 的形态）"


def test_utf16_index_helper_matches_utf16_lengths():
    """换算函数与 UTF-16 长度精确一致（含多个 emoji 与 BMP 混合）。"""
    text = "a🐈b🎥c📡d"          # 3 个非 BMP
    for cp_index, char in enumerate(text):
        utf16_index = _utf16_len(text[:cp_index])
        assert Twitter._utf16_index_to_codepoint(text, utf16_index) == cp_index, char
    # 末尾/越界
    assert Twitter._utf16_index_to_codepoint(text, _utf16_len(text)) == len(text)
    assert Twitter._utf16_index_to_codepoint(text, 10_000) == len(text)
    assert Twitter._utf16_index_to_codepoint(text, 0) == 0
    assert Twitter._utf16_index_to_codepoint(text, -5) == 0


def test_plain_text_indices_are_unchanged():
    """没有非 BMP 字符时两种索引相同 —— 老行为不能变。"""
    assert Twitter._utf16_index_to_codepoint(TEXT, 5) == 5
    assert Twitter._utf16_index_to_codepoint(TEXT, len(TEXT)) == len(TEXT)


def test_the_real_emoji_tweet_from_the_bug_report():
    """用户报障的真实响应（落盘 fixture）: 加粗必须正好是 ``2027年1月7日(木)``。

    之前切出的是 ``27年1月7日(木)毎週``（整段左移 5 个字符）。
    """
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "twitter_richtext_emoji.json"
    if not fixture.exists():
        return
    tweet = Twitter().parse(json.loads(fixture.read_text(encoding="utf-8")))
    assert "**2027年1月7日(木)**" in tweet.full_text
    assert "27年1月7日(木)毎週**" not in tweet.full_text


# ── 真实推文数据 (落盘 fixture) ─────────────────────────────


def test_the_real_tweet_from_the_bug_report():
    """用户报障的那条推文: 9 个 Bold 区间, 逐一对上原文区间内容。

    数据来自当时抓下并落盘的原始响应 (fixture), 不联网。
    """
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "twitter_richtext_note.json"
    if not fixture.exists():
        return  # fixture 不在时跳过 (发布检查不强制带大数据)
    raw = json.loads(fixture.read_text())
    tweet = Twitter().parse(raw)
    text = tweet.full_text

    # 应为粗体包裹的片段 (取原文中三段可辨认的)
    for fragment in (
        "人类的语言其实根本不适合描述这个世界。",
        "他们直接让 AI 自己发明一门语言。",
        "他离开字节之后，他还作为一作拿了 NeurIPS 2024 Best Paper。",
    ):
        assert f"**{fragment}**" in text, f"没有加粗: {fragment}"
