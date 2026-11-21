"""/t —— 用 **X（Twitter）自己的翻译** 把一条推文译成发送者语言。

## 这个接口怎么运作（先搞清楚，再写代码）

请求**只发推文 id，不发正文**：

```json
{"id": "<推文id>", "content_type": "POST", "dst_lang": "zh-cn"}
```

服务端自己知道那条推文的原文，翻译后返回**译文 + 重算过的实体偏移**：

```json
{"result": {
  "text": "译好的正文（换行保留在字面量里）",
  "entities": {
    "urls":          [{"indices": [121, 144], "url": "https://t.co/…", "expanded_url": "…"}],
    "user_mentions": [{"indices": [27, 43], "screen_name": "…"}],
    "hashtags":      [{"indices": [146, 157], "text": "…"}]
  }
}}
```

## 所以「保住格式」= 按 indices 原地套，不拆不拼

``entities[*].indices`` 是**译文字符串里的字符区间**。实测（157 字符译文）：
``27,43`` 精确切出 ``@maruyono_kamome``，``121,144`` 切出 ``https://t.co/…``，
``146,157`` 切出 ``#FX戦士くるみちゃん``；同一批 indices 用在 180 字符的**原文**上
全错 —— 服务端翻译时重算了偏移。

正确做法：拿 ``text`` 原样当正文，按下标把那几段包成带链接的 RichText 片段，
**文本一个字都不改**（换行留在字面量里）。

## 为什么只发译文，不附原文

用户定案「不放」。附原文时：① 长推文撑爆块结构，服务端回
``400 MESSAGE_TOO_LONG``（报错说 text too long，实测总字符才 1500 出头）；
② 共享渲染层的 ``_quote_children`` 会把整段压成一个段落。都不值得为「对照原文」
去动共享层。

## 语言

目标语言取**发送者**的 Telegram 语言（``UserService.ensure_lang``），拿不到就用
项目默认语言（简中）。X 收 BCP-47 小写形式（``zh-cn`` / ``ja`` / ``en``），而项目
locale 是 ``zh-hans`` / ``ja-jp`` 这类，所以要过一次映射。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from parsehub.errors import TranslationUnavailable
from parsehub.provider_api.twitter import Twitter
from parsehub.utils.helpers import SecretCookie
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import (
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichMessage,
    Message,
    RichTextUrl,
)

from core import pl_cfg
from db import get_session
from i18n import t_
from log import logger
from plugins.context import get_config_target
from plugins.helpers import fold_for_show_more, visible_length
from plugins.parse.sender import MessageSender
from services import AdminUserService, SettingsService, UserService

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


def entity_spans(entities: dict) -> list[tuple[int, int, str]]:
    """把三类实体摊平成 ``(start, end, url)``，按起点排序。

    覆盖 X 返回的三种：``user_mentions`` / ``urls`` / ``hashtags``。
    ``urls`` 取 ``expanded_url`` —— ``t.co`` 短链现在会跳登录墙，展开后的真实
    地址才是用户想点的。
    """
    spans: list[tuple[int, int, str]] = []

    for item in entities.get("user_mentions") or []:
        idx = item.get("indices")
        handle = str(item.get("screen_name") or "").strip()
        if isinstance(idx, list) and len(idx) == 2 and handle:
            spans.append((int(idx[0]), int(idx[1]), f"https://x.com/{handle}"))

    for item in entities.get("urls") or []:
        idx = item.get("indices")
        url = str(item.get("expanded_url") or item.get("url") or "").strip()
        if isinstance(idx, list) and len(idx) == 2 and url:
            spans.append((int(idx[0]), int(idx[1]), url))

    for item in entities.get("hashtags") or []:
        idx = item.get("indices")
        tag = str(item.get("text") or "").strip()
        if isinstance(idx, list) and len(idx) == 2 and tag:
            spans.append((int(idx[0]), int(idx[1]), f"https://x.com/hashtag/{quote(tag)}"))

    spans.sort(key=lambda s: (s[0], -s[1]))
    return spans


def inline_entities(text: str, entities: dict) -> list:
    """按 ``indices`` 把译文切成 ``[str, RichTextUrl, str, …]``。

    **文本本身一个字都不改** —— 换行留在字面量里，只在实体区间上包一层链接。
    这才是「保住格式」：格式来自平台的 ``indices``，不是我们拆段落猜出来的。

    Args:
        text: 译文正文（原样，含换行）。
        entities: X 返回的 ``entities``。

    Returns:
        ``InputRichBlockParagraph`` 可直接吃的 text 列表；无实体时 ``[text]``。
    """
    spans = entity_spans(entities)
    if not spans:
        return [text]

    pieces: list = []
    cursor = 0
    for start, end, url in spans:
        # 越界或与前一段重叠就跳过 —— 宁可不加链接，也不能把文本切乱
        # （服务端偶尔会给越界下标，实测过 indices 落在字符串尾部之外的情况）。
        if start < cursor or end > len(text) or start >= end:
            continue
        if start > cursor:
            pieces.append(text[cursor:start])
        pieces.append(RichTextUrl(text[start:end], url))
        cursor = end
    if cursor < len(text):
        pieces.append(text[cursor:])
    return pieces or [text]


@Client.on_message(filters.command(["t"]))
async def translate(cli: Client, msg: Message) -> None:
    """/t <推文链接> —— 把那条推文译成发送者语言。"""
    if not msg.from_user:
        return

    async with get_session() as session:
        lang = await UserService(session).ensure_lang(msg.from_user.id, msg.from_user.language_code)
        config = await SettingsService(session).get_config(get_config_target(msg))
        # 与解析命令一致的鉴权口径：私聊属于非同群调用，只认本地库白名单。
        if msg.chat and msg.chat.type == ChatType.PRIVATE:
            allowed = await AdminUserService(session).is_whitelisted(msg.from_user.id)
            if not allowed:
                logger.info(f"/t 私聊被拦截(不在白名单): user_id={msg.from_user.id}")
                await MessageSender(cli, msg, config).text(t_[lang]("仅限白名单用户使用"))
                return

    _t = t_[lang]

    # ``/t <url>``：命令参数可能为空（只发了 ``/t``），那样没有东西可翻。
    parts = (msg.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    match = _TWEET_URL_RE.search(arg)
    if not match:
        await MessageSender(cli, msg, config).text(_t("用法：/t <推文链接>"))
        return

    tweet_id = match[1]
    url = match[0]
    dst_lang = x_lang_for(lang)

    # **必须走 `SecretCookie`**：``pl_cfg.roll_cookie`` 返回的是 ``MaskedSecretStr``，
    # 它自己的 ``get_value()`` 是脱敏显示（14 字符的 ``gues******znn.``），
    # 直接传给客户端会得到「缺少 ct0」。`SecretCookie.get_value()` 才返回
    # 真正的 ``dict[str, str]``（实测该 cookie 有 1259 字符、12 个字段）。
    raw_cookie = pl_cfg.roll_cookie("twitter")
    cookie = SecretCookie(raw_cookie.get_secret_value() if raw_cookie else None).get_value()
    client = Twitter(cookie=cookie, proxy=pl_cfg.roll_parser_proxy("twitter"))

    try:
        translated, entities = await client.translate_tweet(tweet_id, dst_lang)
    except TranslationUnavailable as e:
        logger.warning(f"/t 翻译不可用: tweet_id={tweet_id} lang={dst_lang} err={e}")
        await MessageSender(cli, msg, config).text(_t(str(e)))
        return
    except Exception as e:  # noqa: BLE001 - 翻译失败不能把命令打成未捕获异常
        logger.exception(f"/t 翻译异常: tweet_id={tweet_id} err={e}")
        await MessageSender(cli, msg, config).text(_t("翻译失败，请稍后再试"))
        return

    text_pieces = inline_entities(translated, entities)

    # 超长时按项目既有机制折叠（只折正文，页脚永远在折叠之外）。
    if visible_length(translated) > 6800:
        text_pieces = [fold_for_show_more(translated)]

    blocks = [
        InputRichBlockParagraph(text_pieces),
        InputRichBlockFooter([RichTextUrl(_t("来源"), url)]),
    ]

    # **必须走项目的 `MessageSender.rich_message`**：``msg.reply()`` 只接受文本，
    # 传 ``InputRichMessage`` 进去会被 pyrogram 转成字符串发出去 —— 生产上就是
    # 这样把 ``{'_': 'InputRichMessage', 'blocks': [...]}`` 当消息发给了用户。
    await MessageSender(cli, msg, config).rich_message(
        rich_message=InputRichMessage(blocks=blocks),
    )
