"""/t —— 用 **X（Twitter）自己的翻译** 把一条推文译成发送者语言。

## 为什么是"平台自己的翻译"

用户明确要求取 **X 平台返回的译文**，不是接外部翻译服务。X 的译文由 Grok 提供，
端点是 ``POST /2/grok/translation.json`` —— 这个端点**不在 GraphQL 里**，是从
X 12.44.0 的 APK（``com.twitter.android``）反编译出来的：

- ``com.x.http.translation.a``: ``@POST("/2/grok/translation.json")``
- 请求体 ``com.x.ai.grok.c``: ``GrokTranslateRequest(id, contentType, dstLang)``
- 响应 ``ApiGrokTranslateResult``: ``text`` / ``entities`` / ``rich_text_entities``

## 语言

目标语言取**发送者**的 Telegram 语言（``UserService.ensure_lang``），拿不到就用
项目默认语言（简中）。X 那边要 BCP-47 小写形式（``zh-cn`` / ``ja`` / ``en``），
而我们的 ``locale_for`` 出的是 ``zh-hans`` / ``ja-jp`` 这类，所以要过一次映射。
"""

from __future__ import annotations

import re

from parsehub.errors import TranslationUnavailable
from parsehub.provider_api.twitter import Twitter
from parsehub.utils.helpers import SecretCookie
from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import LinkPreviewOptions, Message

from core import pl_cfg
from db import get_session
from i18n import t_
from log import logger
from services import AdminUserService, UserService

logger = logger.bind(name="Translate")

#: 我们的界面语言 → X 的 ``dstLang``。
#:
#: X 收的是 BCP-47 **小写**形式，且它自己只区分到「语言-地区」这一层
#: （``zh-cn`` / ``zh-tw`` / ``pt-br``）。项目的 locale 是 ``zh-hans`` /
#: ``zh-hant`` / ``ja-jp`` 这种，繁简在 X 那边对应的是地区而不是文字系统，
#: 所以这里必须显式映射 —— 直接把 ``zh-hans`` 发过去 X 不认。
_LANG_TO_X: dict[str, str] = {
    "zh-hans": "zh-cn",
    "zh-hant": "zh-tw",
    "ja-jp": "ja",
    "en-us": "en",
    "ko-kr": "ko",
    "fr-fr": "fr",
    "de-de": "de",
    "es-es": "es",
    "pt-br": "pt",
    "ru-ru": "ru",
    "it-it": "it",
    "id-id": "id",
    "vi-vn": "vi",
    "th-th": "th",
    "tr-tr": "tr",
    "ar-sa": "ar",
}

#: 推文链接。``status`` 与 ``article`` 共用同一个 id 空间。
_TWEET_URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/[^/\s]+/(?:status|article)/(\d+)")


def x_lang_for(locale: str) -> str:
    """把项目 locale 转成 X 的 ``dstLang``。未知语言退到 ``en``。"""
    return _LANG_TO_X.get((locale or "").lower(), "en")


@Client.on_message(filters.command(["t"]))
async def translate(cli: Client, msg: Message) -> None:
    """/t <推文链接> —— 把那条推文译成发送者语言。"""
    if not msg.from_user:
        return

    async with get_session() as session:
        lang = await UserService(session).ensure_lang(msg.from_user.id, msg.from_user.language_code)
        # 与解析命令一致的鉴权口径：私聊属于非同群调用，只认本地库白名单。
        if msg.chat and msg.chat.type == ChatType.PRIVATE:
            allowed = await AdminUserService(session).is_whitelisted(msg.from_user.id)
            if not allowed:
                logger.info(f"/t 私聊被拦截(不在白名单): user_id={msg.from_user.id}")
                await msg.reply(t_[lang]("仅限白名单用户使用"))
                return

    _t = t_[lang]

    # ``/t <url>``：命令参数可能为空（只发了 ``/t``），那样没有东西可翻。
    text = (msg.text or "").split(maxsplit=1)
    arg = text[1].strip() if len(text) > 1 else ""
    match = _TWEET_URL_RE.search(arg)
    if not match:
        await msg.reply(_t("用法：/t <推文链接>"))
        return

    tweet_id = match[1]
    dst_lang = x_lang_for(lang)

    # **必须走 `SecretCookie`**：``pl_cfg.roll_cookie`` 返回的是 ``MaskedSecretStr``，
    # 它自己的 ``get_value()`` 是脱敏显示（14 字符的 ``gues******znn.``），
    # 直接传给客户端会得到「缺少 ct0」。`SecretCookie.get_value()` 才返回
    # 真正的 ``dict[str, str]``（实测该 cookie 有 1259 字符、12 个字段）。
    raw_cookie = pl_cfg.roll_cookie("twitter")
    cookie = SecretCookie(raw_cookie.get_secret_value() if raw_cookie else None).get_value()
    client = Twitter(cookie=cookie, proxy=pl_cfg.roll_parser_proxy("twitter"))
    try:
        translated = await client.translate_tweet(tweet_id, dst_lang)
    except TranslationUnavailable as e:
        logger.warning(f"/t 翻译不可用: tweet_id={tweet_id} lang={dst_lang} err={e}")
        await msg.reply(_t(str(e)))
        return
    except Exception as e:  # noqa: BLE001 - 翻译失败不能把命令打成未捕获异常
        logger.exception(f"/t 翻译异常: tweet_id={tweet_id} err={e}")
        await msg.reply(_t("翻译失败，请稍后再试"))
        return

    await msg.reply(
        translated,
        parse_mode=ParseMode.DISABLED,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
