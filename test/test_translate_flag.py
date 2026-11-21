"""``#t`` —— 与 ``#剧透`` 同步的翻译开关。

## 为什么是 ``#t`` 不是 ``/t``

用户原话：「不支持 inline？把 /t 换成 #t，和 #剧透 同步一下」。

``/t`` 是命令，只在普通消息里工作，inline 输入框里发 ``/t`` 会被当成普通查询
（实测：inline 把 ``/t https://…`` 整串当分享文案去解析）。``#剧透`` 是
**独立 token**，正文与 inline 都识别 —— ``#t`` 必须照这个形状做，否则同一层
的两个开关行为不一致。

## 接口契约（实测，不是猜）

请求只发 ``{"id": ..., "content_type": "POST", "dst_lang": ...}``，**不发正文**；
服务端返回 ``{"result": {"text": "译文", "entities": {...}}}``。

``entities[*].indices`` 是**译文字符串里的字符区间**。实测一条 157 字符的译文：
``27,43`` → ``@maruyono_kamome``、``121,144`` → ``https://t.co/…``、
``146,157`` → ``#FX戦士くるみちゃん``；同一批 indices 用在 180 字符的原文上全错
（服务端翻译时重算了偏移）。
"""

from __future__ import annotations

import pytest

from parsehub.errors import TranslationUnavailable
from parsehub.provider_api.twitter import Twitter
from parsehub.utils.helpers import strip_translate_flag

COOKIE = {"auth_token": "fake-auth", "ct0": "fake-ct0"}

#: 真实译文 + entities（X 12.44.0 实测）
TEXT = (
    "📈片尾插画公开📈\n\n本周的片尾插画由\n丸代的海鸥老师（@maruyono_kamome）带来！"
    "\n\n请留意望月先生和胡桃酱的表情👀\n\n也请期待下周的胡桃酱哦♪"
    "\n\n《超爱大吃大喝！望月先生》\n最新话正在Young Animal Web免费公开！"
    "\nhttps://t.co/KcKGqar7Au\n\n#FX戦士くるみちゃん"
)
ENTITIES = {
    "urls": [
        {
            "indices": [121, 144],
            "url": "https://t.co/KcKGqar7Au",
            "expanded_url": "https://younganimal.com/episodes/b9a3ec5171c8c",
        }
    ],
    "user_mentions": [{"indices": [27, 43], "screen_name": "maruyono_kamome"}],
    "hashtags": [{"indices": [146, 157], "text": "FX戦士くるみちゃん"}],
}


class TestFlagParsing:
    """``#t`` 必须与 ``#剧透`` 逐条同形 —— 这是用户明确要求的「同步」。"""

    def test_strips_flag_and_reports_hit(self) -> None:
        text, hit = strip_translate_flag("https://x.com/a/status/1 #t")
        assert text == "https://x.com/a/status/1"
        assert hit is True

    def test_case_insensitive(self) -> None:
        for flag in ("#t", "#T", "#翻译", "#翻譯"):
            _, hit = strip_translate_flag(f"https://x.com/a/status/1 {flag}")
            assert hit is True, f"{flag} 应该被识别"

    def test_not_triggered_inside_other_tokens(self) -> None:
        """``#test`` / ``#tt`` 都不是开关 —— 与 ``#nsfwxxx`` 不算同理。"""
        for token in ("#test", "#tt", "#trans", "x#t"):
            _, hit = strip_translate_flag(f"https://x.com/a/status/1 {token}")
            assert hit is False, f"{token} 不该触发"

    def test_url_itself_is_untouched(self) -> None:
        """标记必须空一格再写，URL 本体完全不变（``#剧透`` 的同一规矩）。"""
        url = "https://x.com/a/status/1"
        text, _ = strip_translate_flag(f"{url} #t")
        assert url in text

    def test_can_coexist_with_spoiler(self) -> None:
        """``#t #剧透`` 既要译文又要打码 —— 两个开关不互斥。"""
        from parsehub.utils.helpers import strip_spoiler_flag

        text, hit_t = strip_translate_flag("https://x.com/a/status/1 #t #剧透")
        text, hit_s = strip_spoiler_flag(text)
        assert hit_t is True
        assert hit_s == "#剧透"
        assert text == "https://x.com/a/status/1"

    def test_empty_input(self) -> None:
        assert strip_translate_flag("") == ("", False)
        assert strip_translate_flag(None) == ("", False)

    def test_multiple_flags_reduce_to_one_hit(self) -> None:
        text, hit = strip_translate_flag("https://x.com/a/status/1 #t #T")
        assert hit is True
        assert text == "https://x.com/a/status/1"


class TestEndpointContract:
    """端点与请求体是反向工程的产物，改动必须有新证据。"""

    def test_endpoint_is_the_grok_translation_path(self) -> None:
        assert Twitter._TRANSLATE_ENDPOINT == "https://x.com/i/api/2/grok/translation.json"

    def test_endpoint_is_not_graphql(self) -> None:
        """它不在 GraphQL 里 —— 这就是在 GraphQL 文档里搜不到的原因。"""
        assert "/graphql/" not in Twitter._TRANSLATE_ENDPOINT

    def test_endpoint_carries_the_internal_api_prefix(self) -> None:
        """前缀必须是 /i/api。

        实测三种写法：``x.com/2/...`` → 401（路径不存在，被当匿名）；
        ``x.com/i/api/2/...`` → 500（路由存在、认证已过）；
        ``api.twitter.com/i/api/2/...`` → 404（另一套 host）。
        """
        assert "/i/api/" in Twitter._TRANSLATE_ENDPOINT


class TestRequiresLogin:
    def test_no_cookie_raises_before_any_request(self) -> None:
        import asyncio

        with pytest.raises(TranslationUnavailable, match="登录态"):
            asyncio.run(Twitter(cookie=None).translate_tweet("20", "zh-cn"))

    def test_incomplete_cookie_raises(self) -> None:
        import asyncio

        with pytest.raises(TranslationUnavailable, match="登录态"):
            asyncio.run(Twitter(cookie={"auth_token": "x"}).translate_tweet("20", "zh-cn"))


def _patch_client(monkeypatch, *, status: int = 200, payload: dict | None = None, captured: dict | None = None):
    """装一个假 HTTP 客户端，记录请求并回固定响应。"""
    captured = captured if captured is not None else {}

    class _Resp:
        status_code = status
        text = ""

        @staticmethod
        def json() -> dict:
            return payload or {}

        @staticmethod
        def raise_for_status() -> None:
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")

    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def post(self, url, json=None, headers=None, cookies=None):
            captured.update(url=url, body=json, headers=headers)
            return _Resp()

    monkeypatch.setattr("parsehub.provider_api.twitter.http.AsyncClient", _Client)
    return captured


class TestRequestShape:
    def test_body_uses_apk_field_names(self, monkeypatch) -> None:
        import asyncio

        captured = _patch_client(monkeypatch, payload={"result": {"text": "译好的正文"}})
        text, _ = asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1795284023", "zh-tw"))

        assert text == "译好的正文"
        assert captured["body"] == {
            "id": "1795284023",
            "content_type": "POST",
            "dst_lang": "zh-tw",
        }

    def test_content_type_must_be_uppercase(self, monkeypatch) -> None:
        """``"post"`` / ``"Post"`` 都会 500 code 131 —— 服务端对大小写敏感。

        错值给的是「内部错误」而不是参数错误，所以曾经 8 种组合全 500，
        看起来像接口没启用。
        """
        import asyncio

        captured = _patch_client(monkeypatch, payload={"result": {"text": "ok"}})
        asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))
        assert captured["body"]["content_type"] == "POST"
        assert captured["body"]["content_type"].isupper()

    def test_csrf_header_carries_ct0(self, monkeypatch) -> None:
        import asyncio

        captured = _patch_client(monkeypatch, payload={"result": {"text": "ok"}})
        asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "ja"))
        assert captured["headers"]["x-csrf-token"] == "fake-ct0"


class TestResponses:
    def test_401_becomes_translation_unavailable(self, monkeypatch) -> None:
        import asyncio

        _patch_client(monkeypatch, status=401)
        with pytest.raises(TranslationUnavailable, match="登录态可能已失效"):
            asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))

    def test_5xx_is_reported_as_service_error(self, monkeypatch) -> None:
        """500/131 是 X 侧内部错误，不能报成「登录态失效」。"""
        import asyncio

        _patch_client(monkeypatch, status=500, payload={"errors": [{"code": 131}]})
        with pytest.raises(TranslationUnavailable, match="服务异常"):
            asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))

    def test_reads_translation_from_result_key(self, monkeypatch) -> None:
        """实测响应顶层是 ``result``（APK 的 Retrofit 声明写的是 value）。"""
        import asyncio

        _patch_client(monkeypatch, payload={"result": {"text": "译好的", "entities": {}}})
        text, ents = asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))
        assert text == "译好的"
        assert ents == {}

    def test_returns_entities_alongside_text(self, monkeypatch) -> None:
        """**entities 必须一起返回** —— 只返回 text 就丢了格式信息。"""
        import asyncio

        _patch_client(monkeypatch, payload={"result": {"text": "译好的", "entities": ENTITIES}})
        _, ents = asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))
        assert ents["user_mentions"][0]["screen_name"] == "maruyono_kamome"

    def test_empty_text_is_rejected(self, monkeypatch) -> None:
        import asyncio

        _patch_client(monkeypatch, payload={"result": {"text": "  "}})
        with pytest.raises(TranslationUnavailable, match="没有返回译文"):
            asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))


class TestIndicesAreOnTranslatedText:
    """indices 按译文算 —— 最容易搞错、且搞错就全盘错位的地方。"""

    def test_mention_slice_is_exact(self) -> None:
        assert TEXT[27:43] == "@maruyono_kamome"

    def test_url_slice_is_exact(self) -> None:
        assert TEXT[121:144] == "https://t.co/KcKGqar7Au"

    def test_hashtag_slice_is_exact(self) -> None:
        assert TEXT[146:157] == "#FX戦士くるみちゃん"

    def test_original_text_would_give_different_slices(self) -> None:
        """原文 180 字符、译文 157 字符；把 indices 当原文位置就会切错。"""
        original = "📈エンドカードイラスト公開📈" + "あ" * 157
        assert original[146:157] != TEXT[146:157]


def _strip_links(md: str) -> str:
    """把 markdown 链接还原成纯文字，用于验证「文字没变」。"""
    import re as _re

    return _re.sub(
        r"\[((?:[^\]\\]|\\.)*)\]\([^)]*\)",
        lambda m: m.group(1).replace("\\[", "[").replace("\\]", "]"),
        md,
    )


class TestToMarkdown:
    def test_text_is_unchanged_after_inlining(self) -> None:
        """最重要的一条：加链接不能改动任何正文字符。"""
        from plugins.translate import to_markdown

        assert _strip_links(to_markdown(TEXT, ENTITIES)) == TEXT

    def test_all_three_entity_kinds_become_links(self) -> None:
        from plugins.translate import to_markdown

        md = to_markdown(TEXT, ENTITIES)
        assert "https://x.com/maruyono_kamome" in md
        assert "https://younganimal.com/episodes/b9a3ec5171c8c" in md
        assert "hashtag" in md

    def test_url_uses_expanded_not_tco(self) -> None:
        """``t.co`` 短链现在跳登录墙，必须用 expanded_url。"""
        from plugins.translate import to_markdown

        assert "(https://t.co/KcKGqar7Au)" not in to_markdown(TEXT, ENTITIES)

    def test_newlines_survive(self) -> None:
        """换行必须保留 —— 一旦被吃掉，段落就糊成一片。"""
        from plugins.translate import to_markdown

        assert "\n\n" in to_markdown(TEXT, ENTITIES)

    def test_no_entities_returns_text_verbatim(self) -> None:
        from plugins.translate import to_markdown

        assert to_markdown("纯文本", {}) == "纯文本"

    def test_entities_are_ordered_by_position(self) -> None:
        from plugins.translate import to_markdown

        shuffled = {
            "hashtags": ENTITIES["hashtags"],
            "user_mentions": ENTITIES["user_mentions"],
            "urls": ENTITIES["urls"],
        }
        assert _strip_links(to_markdown(TEXT, shuffled)) == TEXT

    def test_out_of_range_span_is_skipped(self) -> None:
        """越界下标必须跳过而不是抛异常 —— 宁可少一个链接，不能整条失败。"""
        from plugins.translate import to_markdown

        assert to_markdown("短文本", {"hashtags": [{"indices": [999, 1000], "text": "x"}]}) == "短文本"

    def test_overlapping_span_is_skipped(self) -> None:
        from plugins.translate import to_markdown

        bad = {
            "user_mentions": [{"indices": [0, 5], "screen_name": "a"}],
            "hashtags": [{"indices": [3, 8], "text": "b"}],
        }
        assert _strip_links(to_markdown("0123456789", bad)) == "0123456789"


class TestSetBody:
    """两种结果类的正文字段不同，**两个都要写**。"""

    def test_writes_both_fields(self) -> None:
        from plugins.translate import set_body

        class _R:
            markdown_content = ""
            content = "原文"

        r = _R()
        set_body(r, "译文")
        assert r.markdown_content == "译文"
        assert r.content == "译文"

    def test_multimedia_without_markdown_content_still_works(self) -> None:
        """``MultimediaParseResult`` 没有 ``markdown_content`` —— 只写前者会失效。"""
        from plugins.translate import set_body

        class _R:
            __slots__ = ("content",)

            def __init__(self) -> None:
                self.content = "原文"

        r = _R()
        set_body(r, "译文")
        assert r.content == "译文"


class TestLanguageMapping:
    """项目 locale → X 的 dstLang（X 收小写 BCP-47，且繁简按地区区分）。"""

    @pytest.mark.parametrize(
        ("locale", "expected"),
        [("zh-hans", "zh-cn"), ("zh-hant", "zh-tw"), ("ja-jp", "ja"), ("en-us", "en")],
    )
    def test_mapping(self, locale: str, expected: str) -> None:
        from plugins.translate import x_lang_for

        assert x_lang_for(locale) == expected

    def test_traditional_is_not_simplified(self) -> None:
        """繁简在 X 那边是**地区**差异，不能互退。"""
        from plugins.translate import x_lang_for

        assert x_lang_for("zh-hant") != x_lang_for("zh-hans")

    def test_unknown_falls_back_to_english(self) -> None:
        """宁可给英文，也不要发一个 X 不认的码导致 400。"""
        from plugins.translate import x_lang_for

        assert x_lang_for("xx-yy") == "en"
