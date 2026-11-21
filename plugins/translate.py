"""/t —— 用 **X（Twitter）自己的翻译** 把一条推文译成发送者语言。

## 接口怎么运作（先搞清楚再写代码）

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

``entities[*].indices`` 是**译文字符串里的字符区间**（实测：157 字符译文里
``27,43`` 精确切出 ``@maruyono_kamome``；同一批 indices 用在 180 字符原文上全错）。

## 输出必须和「不加 /t」一致

用户报「你确定 /t 和不加 /t 的输出一样？」—— 之前只发译文一段裸文字，丢掉了
普通解析路径会给出的一切：作者名、作者主页、发布时间、浏览量、点赞、配图/视频、
标签、页脚。

正确做法：**复用项目的构建器** ``build_rich_markdown``，把
- 译文（按 indices 套好链接后的 markdown）当作 ``markdown_content``
- 原推文解析结果里的作者/时间/计数/平台/标签 原样传进去

这样 ``/t`` 与普通解析**同形**，只是正文换成译文。

## 为什么只发译文不附原文

用户定案「不放」。附原文会让块结构膨胀（长推文实测 ``400 MESSAGE_TOO_LONG``），
而且要动共享渲染层才能压扁引用块段间距 —— 不值得为次要需求动共享层。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from parsehub import ParseHub
from parsehub.errors import TranslationUnavailable
from parsehub.provider_api.twitter import Twitter
from parsehub.utils.helpers import SecretCookie
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import InputRichMessage, Message

from core import pl_cfg
from db import get_session
from i18n import t_
from log import logger
from plugins.context import get_config_target
from plugins.helpers import build_rich_markdown
from plugins.parse.rich_blocks import markdown_to_blocks
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


def to_markdown(text: str, entities: dict) -> str:
    """按 ``indices`` 把译文转成 markdown：**文本一字不改，只给实体包链接**。

    这才是「保住格式」：格式信息来自平台的 ``indices``，不是我们拆段落猜出来的。
    换行原样保留（markdown 里 ``\\n\\n`` 就是分段）。

    Args:
        text: 译文正文（原样，含换行）。
        entities: X 返回的 ``entities``。

    Returns:
        可直接作为 ``markdown_content`` 交给项目构建器的 markdown。
    """
    spans = entity_spans(entities)
    if not spans:
        return text

    out: list[str] = []
    cursor = 0
    for start, end, url in spans:
        # 越界或与前一段重叠就跳过 —— 宁可不加链接，也不能把文本切乱
        # （服务端偶尔会给越界下标）。
        if start < cursor or end > len(text) or start >= end:
            continue
        if start > cursor:
            out.append(text[cursor:start])
        # 链接文字里的 ``[`` / ``]`` 会断 markdown 语法，转义掉
        label = text[start:end].replace("[", "\\[").replace("]", "\\]")
        out.append(f"[{label}]({url})")
        cursor = end
    if cursor < len(text):
        out.append(text[cursor:])
    return "".join(out) or text


@Client.on_message(filters.command(["t"]))
async def translate(cli: Client, msg: Message) -> None:
    """/t <推文链接> —— 把那条推文译成发送者语言，**输出形态与普通解析一致**。"""
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
    proxy = pl_cfg.roll_parser_proxy("twitter")

    client = Twitter(cookie=cookie, proxy=proxy)
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

    # **原推文照常解析**：作者/时间/浏览量/点赞/标签/平台/配图全在这一步，
    # 少了它 ``/t`` 的输出就和普通解析不一样（用户报的正是这个差异）。
    parse_result = None
    try:
        parse_result = await ParseHub().parse(url, cookie=cookie, proxy=proxy)
    except Exception as e:  # noqa: BLE001 - 解析失败仍要能把译文发出去
        logger.warning(f"/t 解析原推文失败（退化为纯译文）: tweet_id={tweet_id} err={e}")

    markdown = to_markdown(translated, entities)

    if parse_result is None:
        # 兜底：没有元信息时至少给出译文 + 来源
        body = f'{markdown}\n\n---\n\n<footer><a href="{url}">{_t("来源")}</a></footer>'
        await MessageSender(cli, msg, config).rich_message(
            rich_message=InputRichMessage(blocks=markdown_to_blocks(body)),
        )
        return

    # **把正文换成译文**：``build_rich_markdown`` 通过
    # ``rich_content()`` 读 ``parse_result.markdown_content``（有 markdown 源就用它），
    # 所以直接覆盖这个字段即可 —— 作者行、发布时间、浏览量/点赞、标签、页脚、
    # 折叠全由构建器按统一形态生成，``/t`` 与普通解析**同形**，只是正文是译文。
    #
    # 注意 ``MultimediaParseResult`` 没有 ``markdown_content`` 属性（实测那条
    # GTA6 推文是带视频的），所以要**动态补上**而不是假设它存在。
    try:
        parse_result.markdown_content = markdown
    except AttributeError:
        # dataclass 但字段在子类上：用 object.__setattr__ 绕过 slots/frozen
        object.__setattr__(parse_result, "markdown_content", markdown)
    # 正文已换成译文，原解析结果里的 ``{{media:N}}`` 占位不再成立，
    # 但媒体仍按原样挂到消息末尾（构建器自己处理 media_placeholders）。

    rich_markdown = build_rich_markdown(parse_result, config=config, lang=lang)

    await MessageSender(cli, msg, config).rich_message(
        rich_message=InputRichMessage(blocks=markdown_to_blocks(rich_markdown)),
    )
