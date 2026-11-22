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
        """``#t #剧透`` 既要译文又要打码 —— 两个开关不互斥。

        ``strip_flags`` 一次剥完全部开关并按组返回，所以两个包装函数
        各自都能看到自己的命中（它们各自遍历同一个注册表）。
        """
        from parsehub.utils.helpers import strip_flags, strip_spoiler_flag

        raw = "https://x.com/a/status/1 #t #剧透"
        kept, hits = strip_flags(raw)
        assert kept == "https://x.com/a/status/1"
        assert hits == {"translate": "#t", "spoiler": "#剧透"}

        # 包装函数仍按各自的语义返回
        assert strip_translate_flag(raw) == ("https://x.com/a/status/1", True)
        assert strip_spoiler_flag(raw) == ("https://x.com/a/status/1", "#剧透")

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


class TestGroupAutoParse:
    """群里的自动解析要求「整条消息就是链接」—— ``#t`` 不算链接，会让它整个失效。

    用户报「群里自动回复不会识别」：``<链接> #t`` 因为多了一个 token，
    ``url_only_message_urls`` 判定失败 ⇒ 自动解析根本不触发（不是翻译没生效，
    是消息压根没进解析流程）。
    """

    def test_flag_is_stripped_before_url_check(self) -> None:
        from parsehub.utils.helpers import url_only_message_urls

        url = "https://x.com/AlertaNews24/status/2124094722234892461"
        assert url_only_message_urls(f"{url} #t") == [url]

    def test_flag_before_url_also_works(self) -> None:
        from parsehub.utils.helpers import url_only_message_urls

        url = "https://x.com/AlertaNews24/status/2124094722234892461"
        assert url_only_message_urls(f"#t {url}") == [url]

    def test_coexists_with_spoiler_flag(self) -> None:
        from parsehub.utils.helpers import url_only_message_urls

        url = "https://x.com/a/status/1"
        assert url_only_message_urls(f"{url} #t #剧透") == [url]

    def test_text_around_url_still_disqualifies(self) -> None:
        """剥开关不能放松「纯链接」这条硬规则 —— 闲聊里提链接仍不该被解析。"""
        from parsehub.utils.helpers import url_only_message_urls

        assert url_only_message_urls("看看这个 https://x.com/a/status/1 #t") == []

    def test_bare_url_unaffected(self) -> None:
        from parsehub.utils.helpers import url_only_message_urls

        url = "https://x.com/a/status/1"
        assert url_only_message_urls(url) == [url]

    def test_trailing_punctuation_still_trimmed(self) -> None:
        from parsehub.utils.helpers import url_only_message_urls

        url = "https://x.com/a/status/1"
        assert url_only_message_urls(f"{url}。 #t") == [url]


class TestFlagRegistry:
    """所有 ``#`` 开关由一张注册表驱动 —— 这是「做个列表，列表里的都触发」的落地。

    以前每个开关一个独立 strip 函数、每个调用点按顺序手动调一遍。代价：
    漏一处就出现「群里不认 / inline 不认 / 纯链接判定不认」的割裂，
    ``#t`` 的两次故障（inline 无效、群自动解析不触发）都是这么来的。
    """

    def test_both_groups_are_registered(self) -> None:
        from parsehub.utils.helpers import FLAG_GROUPS

        assert set(FLAG_GROUPS) == {"spoiler", "translate"}

    def test_every_flag_in_the_table_is_recognised(self) -> None:
        """**列表里的每一个写法都必须真的触发** —— 否则表就是摆设。"""
        from parsehub.utils.helpers import FLAG_GROUPS, strip_flags

        for group, flags in FLAG_GROUPS.items():
            for flag in flags:
                kept, hits = strip_flags(f"https://x.com/a/status/1 {flag}")
                assert hits.get(group) == flag, f"{flag} 应归入 {group}"
                assert kept == "https://x.com/a/status/1", f"{flag} 应被剥掉"

    def test_table_is_case_insensitive(self) -> None:
        from parsehub.utils.helpers import FLAG_GROUPS, strip_flags

        for group, flags in FLAG_GROUPS.items():
            for flag in flags:
                _, hits = strip_flags(f"https://x.com/a/status/1 {flag.upper()}")
                assert group in hits, f"{flag.upper()} 应被识别"

    def test_multiple_groups_in_one_message(self) -> None:
        from parsehub.utils.helpers import strip_flags

        kept, hits = strip_flags("https://x.com/a/status/1 #t #nsfw")
        assert kept == "https://x.com/a/status/1"
        assert hits == {"translate": "#t", "spoiler": "#nsfw"}

    def test_unknown_hash_token_is_not_stripped(self) -> None:
        """不在表里的 ``#`` 词保持原样 —— 不能把用户正文吃掉。"""
        from parsehub.utils.helpers import strip_flags

        kept, hits = strip_flags("https://x.com/a/status/1 #python")
        assert kept == "https://x.com/a/status/1 #python"
        assert hits == {}
class TestQuotePreservationAndTranslation:
    """``#t`` 不能抹掉引用块 / 回复块 / 投票 / 话题卡片，且同时翻译引用推文。"""

    def test_set_body_preserves_quoted_block_when_only_main_translated(self) -> None:
        from plugins.translate import set_body

        quoted = (
            '> <i><a href="https://x.com/DaewonMovie">대원무비</a> <code>@DaewonMovie</code></i>\n'
            "> <i>&lt;극장판 치이카와&gt; 흥행 질주!</i>"
        )

        class _R:
            content = f"원문 본문\n\n{quoted}"
            quote_roles = ["quoted"]

        r = _R()
        set_body(r, "翻译后的正文")
        assert r.content == f"翻译后的正文\n\n{quoted}"

    def test_set_body_translates_quoted_block_preserving_author_and_struct(self) -> None:
        from plugins.translate import set_body

        quoted = (
            '> <i><a href="https://x.com/DaewonMovie">대원무비</a> <code>@DaewonMovie</code></i>\n'
            "> <i>&lt;극장판 치이카와&gt; 흥행 질주!</i>\n"
            "> <i>300만 관객 돌파💖</i>\n"
            ">\n"
            "> {{poll:0}}"
        )

        class _R:
            content = f"원문 본문\n\n{quoted}"
            quote_roles = ["quoted"]

        r = _R()
        set_body(r, "主帖译文", quoted_markdown="&lt;剧场版 吉伊卡哇&gt; 票房狂飙！\n突破300万观众💖")
        assert r.content == (
            "主帖译文\n\n"
            '> <i><a href="https://x.com/DaewonMovie">대원무비</a> <code>@DaewonMovie</code></i>\n'
            "> <i>&lt;剧场版 吉伊卡哇&gt; 票房狂飙！</i>\n"
            "> <i>突破300万观众💖</i>\n"
            ">\n"
            "> {{poll:0}}"
        )

    def test_set_body_preserves_reply_and_quoted_and_main_poll(self) -> None:
        from plugins.translate import set_body

        reply = '> <i><a href="https://x.com/a">A</a> <code>@a</code></i>\n> <i>reply orig</i>'
        quoted = '> <i><a href="https://x.com/b">B</a> <code>@b</code></i>\n> <i>quoted orig</i>'

        class _R:
            content = f"{reply}\n\nmain orig\n\n{{{{poll:0}}}}\n\n{quoted}"
            quote_roles = ["reply", "quoted"]

        r = _R()
        set_body(r, "主帖译文", reply_markdown="回复译文", quoted_markdown="引用译文")
        assert r.content == (
            '> <i><a href="https://x.com/a">A</a> <code>@a</code></i>\n'
            "> <i>回复译文</i>\n\n"
            "主帖译文\n\n"
            "{{poll:0}}\n\n"
            '> <i><a href="https://x.com/b">B</a> <code>@b</code></i>\n'
            "> <i>引用译文</i>"
        )

    def test_set_body_keeps_sign_only_image_quote_untouched(self) -> None:
        from plugins.translate import replace_quote_block_body

        sign_only = '> <i><a href="https://x.com/img">Img</a> <code>@img</code></i>'
        assert replace_quote_block_body(sign_only, "不该塞进来的文字") == sign_only

    def test_set_body_translates_quote_even_when_main_is_none(self) -> None:
        """中文主帖引用外语推文：主帖无需翻译（None），引用块仍译成中文。"""
        from plugins.translate import set_body

        quoted = '> <i><a href="https://x.com/b">B</a> <code>@b</code></i>\n> <i>hello world</i>'

        class _R:
            content = f"笑死\n\n{quoted}"
            quote_roles = ["quoted"]

        r = _R()
        set_body(r, None, quoted_markdown="你好世界")
        assert r.content == (
            "笑死\n\n"
            '> <i><a href="https://x.com/b">B</a> <code>@b</code></i>\n'
            "> <i>你好世界</i>"
        )

    def test_apply_translation_translates_main_and_quoted_concurrently(self, monkeypatch) -> None:
        import asyncio
        from plugins import translate as tr_mod

        calls: list[tuple[str, str]] = []

        class _FakeClient:
            async def translate_tweet(self, tweet_id: str, dst_lang: str):
                calls.append((tweet_id, dst_lang))
                if tweet_id == "100":
                    return "主帖译文", {}
                if tweet_id == "200":
                    return "引用译文", {}
                raise TranslationUnavailable("none")

        monkeypatch.setattr(tr_mod, "_build_twitter_client", lambda: _FakeClient())
        # 隔离 Redis，避免单测读写本机共享 Redis
        async def _no_cache(_k: str):
            return None

        async def _no_set(_k: str, _v: str, _ttl: int):
            return True

        monkeypatch.setattr(tr_mod, "get_optional", _no_cache)
        monkeypatch.setattr(tr_mod, "set_with_ttl", _no_set)

        class _R:
            content = (
                "원문\n\n"
                '> <i><a href="https://x.com/q">Q</a> <code>@q</code></i>\n'
                "> <i>인용 원문</i>"
            )
            quote_roles = ["quoted"]
            quote_ids = {"quoted": "200"}

        r = _R()
        changed = asyncio.run(tr_mod.apply_translation(r, "https://x.com/u/status/100", "zh-hans"))
        assert changed is True
        assert ("100", "zh-cn") in calls
        assert ("200", "zh-cn") in calls
        assert "主帖译文" in r.content
        assert "> <i>引用译文</i>" in r.content
        assert 'href="https://x.com/q"' in r.content

    def test_translation_cache_hits_avoid_second_api_call(self, monkeypatch) -> None:
        """同一条推文第二次 ``#t`` 命中译文缓存，不再请求 X 接口。"""
        import asyncio
        from plugins import translate as tr_mod

        store: dict[str, str] = {}
        api_calls: list[str] = []

        async def _get(k: str) -> str | None:
            return store.get(k)

        async def _set(k: str, v: str, _ttl: int) -> bool:
            store[k] = v
            return True

        class _FakeClient:
            async def translate_tweet(self, tweet_id: str, dst_lang: str):
                api_calls.append(tweet_id)
                return f"译文-{tweet_id}", {}

        monkeypatch.setattr(tr_mod, "get_optional", _get)
        monkeypatch.setattr(tr_mod, "set_with_ttl", _set)
        monkeypatch.setattr(tr_mod, "_build_twitter_client", lambda: _FakeClient())

        class _R:
            def __init__(self) -> None:
                self.content = "orig"

        r1, r2 = _R(), _R()
        assert asyncio.run(tr_mod.apply_translation(r1, "https://x.com/u/status/999", "zh-hans")) is True
        assert asyncio.run(tr_mod.apply_translation(r2, "https://x.com/u/status/999", "zh-hans")) is True
        assert r1.content == "译文-999"
        assert r2.content == "译文-999"
        assert api_calls == ["999"]

    def test_translate_cache_entry_reuses_media_without_mutating_original(self, monkeypatch) -> None:
        """``#t`` 命中 file_id 缓存时克隆并翻译正文与引用，原缓存条目保持原文、媒体 file_id 原样保留。"""
        import asyncio
        from plugins import translate as tr_mod
        from services.cache import CacheEntry, CacheMedia, CacheMediaType, CacheParseResult

        async def _no_cache(_k: str):
            return None

        async def _no_set(_k: str, _v: str, _ttl: int):
            return True

        class _FakeClient:
            async def translate_tweet(self, tweet_id: str, dst_lang: str):
                return ("主帖译文" if tweet_id == "100" else "引用译文"), {}

        monkeypatch.setattr(tr_mod, "get_optional", _no_cache)
        monkeypatch.setattr(tr_mod, "set_with_ttl", _no_set)
        monkeypatch.setattr(tr_mod, "_build_twitter_client", lambda: _FakeClient())

        orig_entry = CacheEntry(
            parse_result=CacheParseResult(
                content=(
                    "원문\n\n"
                    '> <i><a href="https://x.com/q">Q</a> <code>@q</code></i>\n'
                    "> <i>인용 원문</i>"
                ),
                quote_roles=["quoted"],
                quote_ids={"quoted": "200"},
                quoted_media_count=1,
            ),
            media=[
                CacheMedia(type=CacheMediaType.PHOTO, file_id="fid_main"),
                CacheMedia(type=CacheMediaType.PHOTO, file_id="fid_quote"),
            ],
            rich=True,
        )

        translated = asyncio.run(tr_mod.translate_cache_entry(orig_entry, "https://x.com/u/status/100", "zh-hans"))
        assert translated is not orig_entry
        assert "主帖译文" in translated.parse_result.content
        assert "> <i>引用译文</i>" in translated.parse_result.content
        assert [m.file_id for m in translated.media or []] == ["fid_main", "fid_quote"]
        # 原缓存条目未被污染
        assert "원문" in orig_entry.parse_result.content
        assert "主帖译文" not in orig_entry.parse_result.content

    def test_pipeline_reports_translated_display_result_during_download_and_process(self, tmp_path) -> None:
        """下载中 / 处理中进度编辑必须全程展示译文，不能从译文闪回原文。"""
        import asyncio
        from parsehub.types import DownloadResult, ImageFile, ImageRef, MultimediaParseResult
        from plugins.translate import set_body
        from services.pipeline import ParsePipeline

        reported_contents: list[tuple[str, str]] = []

        class _FakeReporter:
            async def report(self, text: str) -> None:
                pass

            async def report_progress(self, text: str) -> None:
                pass

            async def report_result(self, parse_result, text: str) -> None:
                reported_contents.append((text, parse_result.content))

            async def report_error(self, stage: str, error: Exception) -> None:
                raise error

            async def dismiss(self) -> None:
                pass

        from PIL import Image

        img_path = tmp_path / "a.jpg"
        Image.new("RGB", (2, 2)).save(img_path)

        class _FakeResult(MultimediaParseResult):
            async def download(self, *_a, **_kw):
                return DownloadResult(
                    media=[ImageFile(path=str(img_path))],
                    output_dir=tmp_path,
                )

        orig = _FakeResult(content="원문 본문", media=[ImageRef(url="https://example.com/a.jpg")])

        async def _prepare(pr):
            import copy

            cloned = copy.copy(pr)
            set_body(cloned, "译文正文")
            return cloned

        class _FakeT:
            locale = "zh-hans"

            def __call__(self, s: str) -> str:
                return s

        async def _run():
            with ParsePipeline(
                "https://x.com/u/status/100",
                "https://x.com/u/status/100",
                _FakeReporter(),
                parse_result=orig,
                singleflight=False,
                skip_media_processing=True,
                prepare_display=_prepare,
                t=_FakeT(),
            ) as p:
                return await p.run()

        res = asyncio.run(_run())
        assert res is not None
        # 下载中与处理中两帧全都是译文，绝不出现原文
        assert len(reported_contents) == 2
        assert all(content == "译文正文" for _, content in reported_contents)
        # 原始 parse_result 仍保持原文，供写缓存使用
        assert res.parse_result.content == "원문 본문"
        assert res.display_result is not None
        assert res.display_result.content == "译文正文"

    def test_author_greentext_paragraphs_are_fully_replaced_and_stay_above_media(self) -> None:
        """作者正文里自写的 ``> `` 列表段落（无 quote_roles、非斜体引用卡）：

        1. ``set_body`` 必须整段替换成译文，绝不能把原文的 ``> `` 段落残留在译文后面；
        2. ``build_rich_markdown`` 必须把所有 ``> `` 段落留在图片上方，不能把最后一段单独扯到图片下面。
        """
        from parsehub.types import MultimediaParseResult, Platform
        from plugins.helpers import build_rich_markdown
        from plugins.translate import set_body
        from repo.settings import SettingsConfig

        orig = (
            "Rockstar intro:\n\n"
            "> Paragraph one in Russian\n\n"
            "> Paragraph two in Russian\n\n"
            "> Paragraph three in Russian"
        )
        translated = (
            "Rockstar 开场：\n\n"
            "> 中文第一段\n\n"
            "> 中文第二段\n\n"
            "> 中文第三段"
        )

        r = MultimediaParseResult(content=orig, quote_roles=[])
        r.platform = Platform.TWITTER
        r.raw_url = "https://x.com/ogivus/status/2124448710637547789"
        set_body(r, translated)

        assert r.content == translated
        assert "Russian" not in r.content

        md = build_rich_markdown(
            r,
            config=SettingsConfig(),
            lang="zh-hans",
            media_placeholders=["![](tg://photo?id=m0)", "![](tg://photo?id=m1)"],
        )
        assert md.index("中文第三段") < md.index("tg://photo?id=m0")

    def test_link_hashtags_does_not_double_link_markdown_links(self) -> None:
        from parsehub.types import Platform
        from plugins.helpers import link_hashtags

        md = "[#치이카와](https://x.com/hashtag/%EC%B9%98%EC%9D%B4%EC%B9%B4%EC%99%80) #일반"
        out = link_hashtags(md, Platform.TWITTER, ["치이카와", "일반"])
        assert out.startswith("[#치이카와](https://x.com/hashtag/%EC%B9%98%EC%9D%B4%EC%B9%B4%EC%99%80)")
        assert "[<a " not in out
        assert '<a href="https://x.com/hashtag/' in out

    def test_quote_ids_round_trips_through_result_cache(self) -> None:
        from parsehub.types import MultimediaParseResult
        from parsehub.types.serialize import result_from_cache_dict, result_to_cache_dict

        orig = MultimediaParseResult(
            content="hi",
            quote_roles=["quoted"],
            quote_ids={"quoted": "2123929422529286639"},
        )
        data = result_to_cache_dict(orig)
        assert data["quote_ids"] == {"quoted": "2123929422529286639"}
        rebuilt = result_from_cache_dict(data)
        assert rebuilt.quote_ids == {"quoted": "2123929422529286639"}
