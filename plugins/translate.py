"""/t —— 用 **X（Twitter）自己的翻译** 把一条推文译成发送者语言。

## 为什么是"平台自己的翻译"

用户明确要求取 **X 平台返回的译文**，不是接外部翻译服务。X 的译文由 Grok 提供，
端点是 ``POST /i/api/2/grok/translation.json``。这个端点**不在 GraphQL 里**，
是从 X 12.44.0 的 APK（``com.twitter.android``）反编译出来的：

- ``com.x.http.translation.a``: ``@POST("/2/grok/translation.json")``
- 请求体 ``com.x.ai.grok.c``：``id`` / ``content_type`` / ``dst_lang``
  （snake_case，来自 kotlinx.serialization 描述符）
- **``content_type`` 必须是全大写 ``"POST"``** —— 真实调用点
  ``com/x/urt/items/post/translate/grok/q.smali`` 里就是 ``const-string v11, "POST"``；
  写 ``"post"`` 会得到 500 code 131（服务端拿内部错误回参数错，极易误判成接口没启用）
- 响应：``{"result": {"content_type": "POST", "text": "...", "entities": {}}}``
  —— **顶层键是 ``result``**，不是 App 侧 Retrofit 声明的 ``value``

## 输出形态：整条推文，不是一段裸文字

用户报「怎么只有译文？格式呢？」—— 只发 ``result.text`` 会把作者、来源链接、
发布时间、话题标签、配图、原文全部丢掉。所以这里**复用项目自己的推文解析**
（``ParseHub.parse``）拿到完整元信息，再把译文当作正文正文段：

```
作者 · @handle
        <- 空行
译文正文（保留原文换行与段落）
        <- 空行
> 原文（引用块，保留原语言，可对照）
        <- 空行
来源：<原推文链接>
```

配图照常下载并发送（``ParseHub`` 的 media 走项目统一下载链路）。

## 语言

目标语言取**发送者**的 Telegram 语言（``UserService.ensure_lang``），拿不到就用
项目默认语言（简中）。X 那边要 BCP-47 小写形式（``zh-cn`` / ``ja`` / ``en``），
而我们的 ``locale_for`` 出的是 ``zh-hans`` / ``ja-jp`` 这类，所以要过一次映射。
"""

from __future__ import annotations

import re

from parsehub import ParseHub
from parsehub.errors import TranslationUnavailable
from parsehub.provider_api.twitter import Twitter
from parsehub.types import RichTextParseResult
from parsehub.utils.helpers import SecretCookie
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import LinkPreviewOptions, Message

from core import pl_cfg
from db import get_session
from i18n import t_
from log import logger
from plugins.helpers import fold_for_show_more, visible_length
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


def build_translated_body(translated: str, original: str) -> str:
    """拼出译文正文：**译文在前、原文引用块在后**。

    为什么不只发译文：用户对照原文是常见需求，而且译文是机器生成的，留原文
    才能判断译得对不对。原文用引用块（``> ``）是项目既有形态 —— 引用块在
    富文本里是一个独立块，不会和正文混成一片。

    Args:
        translated: X 返回的译文正文。
        original: 原推文全文（可为空 —— 取不到原文时只发译文）。

    Returns:
        可直接作为正文字符串的 markdown。
    """
    translated = (translated or "").strip()
    original = (original or "").strip()
    if not original or original == translated:
        return translated
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in original.splitlines())
    return f"{translated}\n\n{quoted}"


def build_header(parse_result: object, fallback_id: str) -> str:
    """作者行 + 来源链接。作者名/句柄来自项目解析结果。"""
    name = str(getattr(parse_result, "author_name", "") or "").strip()
    handle = str(getattr(parse_result, "author_handle", "") or "").strip()
    url = str(getattr(parse_result, "raw_url", "") or "").strip()
    if not url:
        url = f"https://x.com/i/status/{fallback_id}"

    if name and handle:
        author = f"**{name}** ({handle})"
    elif handle:
        author = f"({handle})"
    elif name:
        author = f"**{name}**"
    else:
        author = ""
    # 来源用 markdown 链接，Telegram 富文本会渲染成可点文本而不是裸 URL 预览
    source = f"[{t_source_label()}]({url})"
    return f"{author}\n{source}" if author else source


def t_source_label() -> str:
    """``来源`` 的标签（用项目既有词条，不新造硬编码文案）。"""
    return t_("来源")


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
    parts = (msg.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    match = _TWEET_URL_RE.search(arg)
    if not match:
        await msg.reply(_t("用法：/t <推文链接>"))
        return

    tweet_id = match[1]
    dst_lang = x_lang_for(lang)
    url = match[0]

    # **必须走 `SecretCookie`**：``pl_cfg.roll_cookie`` 返回的是 ``MaskedSecretStr``，
    # 它自己的 ``get_value()`` 是脱敏显示（14 字符的 ``gues******znn.``），
    # 直接传给客户端会得到「缺少 ct0」。`SecretCookie.get_value()` 才返回
    # 真正的 ``dict[str, str]``（实测该 cookie 有 1259 字符、12 个字段）。
    raw_cookie = pl_cfg.roll_cookie("twitter")
    cookie = SecretCookie(raw_cookie.get_secret_value() if raw_cookie else None).get_value()
    proxy = pl_cfg.roll_parser_proxy("twitter")

    # ① 翻译（平台自己的译文）
    client = Twitter(cookie=cookie, proxy=proxy)
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

    # ② 完整解析（作者 / 来源 / 原文 / 配图）—— **复用项目解析链**，不自己拼
    original = ""
    parse_result = None
    try:
        parse_result = await ParseHub().parse(url, cookie=cookie, proxy=proxy)
        # **正文有两个来源，按结果类型取**：带图/视频的推文走
        # ``MultimediaParseResult``，它的正文在 ``content``；纯文本推文才是
        # ``RichTextParseResult`` 的 ``markdown_content``。只看后者会让带图推文
        # 的原文整段丢失（实测那条 GTA6 推文就有 1 张图）。
        if isinstance(parse_result, RichTextParseResult):
            original = (parse_result.markdown_content or "").strip()
        if not original:
            original = str(getattr(parse_result, "content", "") or "").strip()
    except Exception as e:  # noqa: BLE001 - 解析失败仍要能把译文发出去
        logger.warning(f"/t 取原推文失败（只发译文）: tweet_id={tweet_id} err={e}")

    header = build_header(parse_result, tweet_id) if parse_result else f"[{t_source_label()}]({url})"
    body = build_translated_body(translated, original)

    # 正文超过客户端折叠阈值时仍要保证页脚（来源）可见：先把来源摘出来，
    # 只折正文（与项目其它长文一致）。
    full = "\n".join([header, "", body])
    if visible_length(full) > 6800:
        full = f"{header}\n\n{fold_for_show_more(body)}"

    # **走 blocks 通道**：``ParseMode.DISABLED`` 会把 ``**粗体**`` 和
    # ``[来源](url)`` 当字面量发出去，用户看到的是满屏星号与方括号。
    # blocks 的每块携带字面量、服务端不解析 —— 我们拼的是**自己的 markdown**，
    # 而 ``markdown_to_blocks`` 正是把它转成结构化块的那条通道。
    from pyrogram.types import InputRichMessage

    from plugins.parse.rich_blocks import markdown_to_blocks

    await msg.reply(
        InputRichMessage(blocks=markdown_to_blocks(full)),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
