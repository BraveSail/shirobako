"""`/t` 命令：用 X 自己的翻译（Grok）译推文，目标语言跟随发送者。

## 契约来源

端点与请求体**不是猜的**，是从 X 12.44.0 的 APK（``com.twitter.android``）
反编译出来的：

- ``com.x.http.translation.a``: ``@POST("/2/grok/translation.json")``
- 请求体 ``com.x.ai.grok.c``，``toString`` 直接给出字段名：
  ``GrokTranslateRequest(id=…, contentType=…, dstLang=…)``
- 响应 ``ApiGrokTranslateResult``: ``text`` / ``entities`` /
  ``rich_text_entities`` / ``index`` / ``content_type``

## 为什么要在这里钉住

这个端点在 web 端 GraphQL 文档里**搜不到**（它是独立的 REST 接口），
下次有人想加翻译功能时，若不知道这个结论就会重走一遍「猜 operation 名 →
404 → 猜 REST 路径 → 404」的死路。
"""

from __future__ import annotations

import pytest

from parsehub.errors import TranslationUnavailable
from parsehub.provider_api.twitter import Twitter

COOKIE = {"auth_token": "fake-auth", "ct0": "fake-ct0"}


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


class TestRequestShape:
    """请求体的三个字段名来自 APK 里的 ``GrokTranslateRequest``。"""

    def test_body_uses_apk_field_names(self, monkeypatch) -> None:
        import asyncio
        import json

        captured: dict = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> dict:
                return {"value": {"text": "译好的正文"}}

            @staticmethod
            def raise_for_status() -> None:
                return None

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
        out = asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1795284023", "zh-tw"))

        assert out == "译好的正文"
        assert captured["url"] == Twitter._TRANSLATE_ENDPOINT
        assert captured["body"] == {
            "id": "1795284023",
            "content_type": "post",
            "dst_lang": "zh-tw",
        }

    def test_body_uses_snake_case_not_kotlin_property_names(self, monkeypatch) -> None:
        """``toString`` 显示的 ``contentType``/``dstLang`` 是 Kotlin 属性名。

        真实传输键名在 kotlinx.serialization 的描述符里（``com.x.ai.grok.a``）：
        ``id`` / ``content_type`` / ``dst_lang``。照着 toString 写会得到 500/131。
        """
        import asyncio

        captured: dict = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> dict:
                return {"value": {"text": "ok"}}

            @staticmethod
            def raise_for_status() -> None:
                return None

        class _Client:
            def __init__(self, *a, **kw) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a) -> None:
                return None

            async def post(self, url, json=None, headers=None, cookies=None):
                captured["body"] = json
                return _Resp()

        monkeypatch.setattr("parsehub.provider_api.twitter.http.AsyncClient", _Client)
        asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))

        body = captured["body"]
        assert "contentType" not in body, "Kotlin 属性名不是传输键名"
        assert "dstLang" not in body, "Kotlin 属性名不是传输键名"
        assert body["content_type"] == "post"
        assert body["dst_lang"] == "zh-cn"

    def test_csrf_header_carries_ct0(self, monkeypatch) -> None:
        """X 的写类请求要 ``x-csrf-token`` 且必须与 cookie 里的 ct0 一致。"""
        import asyncio

        captured: dict = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> dict:
                return {"value": {"text": "ok"}}

            @staticmethod
            def raise_for_status() -> None:
                return None

        class _Client:
            def __init__(self, *a, **kw) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a) -> None:
                return None

            async def post(self, url, json=None, headers=None, cookies=None):
                captured["headers"] = headers
                return _Resp()

        monkeypatch.setattr("parsehub.provider_api.twitter.http.AsyncClient", _Client)
        asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "ja"))
        assert captured["headers"]["x-csrf-token"] == "fake-ct0"


class TestResponses:
    def _run(self, monkeypatch, *, status: int, payload: dict | None):
        import asyncio

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

            async def post(self, *a, **kw):
                return _Resp()

        monkeypatch.setattr("parsehub.provider_api.twitter.http.AsyncClient", _Client)
        return asyncio.run(Twitter(cookie=dict(COOKIE)).translate_tweet("1", "zh-cn"))

    def test_401_becomes_translation_unavailable(self, monkeypatch) -> None:
        with pytest.raises(TranslationUnavailable, match="登录态可能已失效"):
            self._run(monkeypatch, status=401, payload=None)

    def test_error_field_is_surfaced(self, monkeypatch) -> None:
        with pytest.raises(TranslationUnavailable, match="翻译失败"):
            self._run(monkeypatch, status=200, payload={"error": "not_translatable"})

    def test_5xx_is_reported_as_service_error(self, monkeypatch) -> None:
        """500/131 是 X 侧内部错误，不能报成「登录态失效」。"""
        with pytest.raises(TranslationUnavailable, match="服务异常"):
            self._run(monkeypatch, status=500, payload={"errors": [{"code": 131}]})

    def test_empty_text_is_rejected(self, monkeypatch) -> None:
        """别把空串当译文发出去 —— 用户会收到一条空消息。"""
        with pytest.raises(TranslationUnavailable, match="没有返回译文"):
            self._run(monkeypatch, status=200, payload={"value": {"text": "  "}})


class TestLanguageMapping:
    """项目 locale → X 的 dstLang（X 收小写 BCP-47，且繁简按地区区分）。"""

    def test_simplified_and_traditional_differ(self) -> None:
        from plugins.translate import x_lang_for

        assert x_lang_for("zh-hans") == "zh-cn"
        assert x_lang_for("zh-hant") == "zh-tw"

    def test_japanese_uses_bare_code(self) -> None:
        from plugins.translate import x_lang_for

        assert x_lang_for("ja-jp") == "ja"

    def test_unknown_locale_falls_back_to_english(self) -> None:
        """宁可给英文，也不要发一个 X 不认的码导致 400。"""
        from plugins.translate import x_lang_for

        assert x_lang_for("xx-yy") == "en"

    @pytest.mark.parametrize("locale", ["zh-hans", "zh-hant", "en-us", "ja-jp", "ko-kr"])
    def test_mapping_is_lowercase(self, locale: str) -> None:
        from plugins.translate import x_lang_for

        assert x_lang_for(locale) == x_lang_for(locale).lower()


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
