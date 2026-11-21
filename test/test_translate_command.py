"""`/t` 命令：用 X 自己的翻译（Grok）译推文，格式靠 entities.indices 保住。

## 接口契约（实测，不是猜）

请求**只发推文 id，不发正文**：

```json
{"id": "<推文id>", "content_type": "POST", "dst_lang": "zh-cn"}
```

服务端自己知道那条推文的原文，翻译后返回**译文 + 重算过的实体偏移**：

```json
{"result": {"text": "译文", "entities": {"urls": [...], "user_mentions": [...], "hashtags": [...]}}}
```

端点与请求体来自 X 12.44.0 的 APK（``com.twitter.android``）反编译：
``com.x.http.translation.a`` 上的 ``@POST("/2/grok/translation.json")``，
请求体字段在 kotlinx.serialization 描述符 ``com.x.ai.grok.a`` 里，
``content_type`` 的真实值 ``"POST"`` 在调用点
``com/x/urt/items/post/translate/grok/q.smali``。

## 格式怎么保住

``entities[*].indices`` 是**译文字符串里的字符区间**。实测一条 157 字符的译文：
``27,43`` → ``@maruyono_kamome``、``121,144`` → ``https://t.co/…``、
``146,157`` → ``#FX戦士くるみちゃん``；同一批 indices 用在 180 字符的原文上全错。

所以正确做法是拿 ``text`` 原样当正文、按下标把实体包成链接，**文本一字不改**。
曾经为「格式」去拆段落再拼回 blocks，既丢了平台给的真格式，又让块结构超限
（``400 MESSAGE_TOO_LONG``）。
"""

from __future__ import annotations

import pytest

from parsehub.errors import TranslationUnavailable
from parsehub.provider_api.twitter import Twitter

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
        漏掉前缀会让人误判成「cookie 失效」。
        """
        assert "/i/api/" in Twitter._TRANSLATE_ENDPOINT


class TestRequiresLogin:
    """匿名调用一律 401，所以必须在发出请求前就拒绝。"""

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
    import asyncio

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
    """请求体三个字段，名字与取值都来自 APK。"""

    def test_body_uses_apk_field_names(self, monkeypatch) -> None:
        import asyncio

        captured = _patch_client(monkeypatch, payload={"result": {"text": "译好的正文"}})
        text, _ = asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1795284023", "zh-tw"))

        assert text == "译好的正文"
        assert captured["url"] == Twitter._TRANSLATE_ENDPOINT
        assert captured["body"] == {
            "id": "1795284023",
            "content_type": "POST",
            "dst_lang": "zh-tw",
        }

    def test_body_uses_snake_case_not_kotlin_property_names(self, monkeypatch) -> None:
        """``toString`` 显示的 ``contentType``/``dstLang`` 是 Kotlin 属性名。

        真实传输键名在 kotlinx.serialization 描述符里：``id`` / ``content_type`` /
        ``dst_lang``。照着 toString 写会得到 500/131。
        """
        import asyncio

        captured = _patch_client(monkeypatch, payload={"result": {"text": "ok"}})
        asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))

        body = captured["body"]
        assert "contentType" not in body, "Kotlin 属性名不是传输键名"
        assert "dstLang" not in body, "Kotlin 属性名不是传输键名"
        assert body["content_type"] == "POST"
        assert body["dst_lang"] == "zh-cn"

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
        """X 的写类请求要 ``x-csrf-token`` 且必须与 cookie 里的 ct0 一致。"""
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

    def test_error_field_is_surfaced(self, monkeypatch) -> None:
        import asyncio

        _patch_client(monkeypatch, payload={"error": "not_translatable"})
        with pytest.raises(TranslationUnavailable, match="翻译失败"):
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
        text, ents = asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))
        assert text == "译好的"
        assert ents["user_mentions"][0]["screen_name"] == "maruyono_kamome"

    def test_missing_entities_becomes_empty_dict(self, monkeypatch) -> None:
        import asyncio

        _patch_client(monkeypatch, payload={"result": {"text": "无实体"}})
        _, ents = asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))
        assert ents == {}

    def test_empty_text_is_rejected(self, monkeypatch) -> None:
        """别把空串当译文发出去 —— 用户会收到一条空消息。"""
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

    return _re.sub(r"\[((?:[^\]\\]|\\.)*)\]\([^)]*\)", lambda m: m.group(1).replace("\\[", "[").replace("\\]", "]"), md)


class TestToMarkdown:
    def test_text_is_unchanged_after_inlining(self) -> None:
        """最重要的一条：加链接不能改动任何正文字符。"""
        from plugins.translate import to_markdown

        md = to_markdown(TEXT, ENTITIES)
        assert _strip_links(md) == TEXT

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
        """乱序输入也要按出现顺序切，否则文本会被切碎。"""
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

        bad = {"hashtags": [{"indices": [999, 1000], "text": "x"}]}
        assert to_markdown("短文本", bad) == "短文本"

    def test_overlapping_span_is_skipped(self) -> None:
        from plugins.translate import to_markdown

        bad = {
            "user_mentions": [{"indices": [0, 5], "screen_name": "a"}],
            "hashtags": [{"indices": [3, 8], "text": "b"}],
        }
        assert _strip_links(to_markdown("0123456789", bad)) == "0123456789"


class TestEntitySpans:
    def test_mention_url_shape(self) -> None:
        from plugins.translate import entity_spans

        assert entity_spans({"user_mentions": [{"indices": [1, 2], "screen_name": "abc"}]}) == [
            (1, 2, "https://x.com/abc")
        ]

    def test_hashtag_is_url_encoded(self) -> None:
        """日文话题名必须 URL 编码，否则链接打不开。"""
        from plugins.translate import entity_spans

        spans = entity_spans({"hashtags": [{"indices": [0, 3], "text": "戦士"}]})
        assert spans[0][2].startswith("https://x.com/hashtag/")
        assert "%" in spans[0][2]

    def test_malformed_entries_are_ignored(self) -> None:
        from plugins.translate import entity_spans

        spans = entity_spans(
            {
                "user_mentions": [{"indices": None, "screen_name": "x"}, {"indices": [1]}],
                "urls": [{"indices": [0, 1]}],
                "hashtags": None,
            }
        )
        assert spans == []


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


class TestCommandParsing:
    def test_extracts_tweet_id_from_status_url(self) -> None:
        from plugins.translate import _TWEET_URL_RE

        assert _TWEET_URL_RE.search("https://x.com/NASA/status/20")[1] == "20"

    def test_extracts_tweet_id_from_twitter_url(self) -> None:
        from plugins.translate import _TWEET_URL_RE

        assert _TWEET_URL_RE.search("https://twitter.com/user/status/1795284023")[1] == "1795284023"

    def test_extracts_article_id(self) -> None:
        """``/article/<id>`` 与 ``/status/<id>`` 同一个 id 空间。"""
        from plugins.translate import _TWEET_URL_RE

        assert _TWEET_URL_RE.search("https://x.com/u/article/1234567890")[1] == "1234567890"

    def test_non_tweet_url_does_not_match(self) -> None:
        from plugins.translate import _TWEET_URL_RE

        assert _TWEET_URL_RE.search("https://x.com/NASA") is None
