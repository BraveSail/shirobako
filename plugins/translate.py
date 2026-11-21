"""``#t`` —— 用 **X（Twitter）自己的翻译** 把推文译成用户语言。

## 触发方式（与 ``#剧透`` 同步）

用户原话：「把 /t 换成 #t，和 #剧透 同步一下」。两者本来就是同一层的开关
（都是「链接后面空一格的独立 token」），行为必须完全一致：

```
https://x.com/user/status/123 #t
https://x.com/user/status/123 #t #剧透     ← 可并存：既要译文又要打码
https://x.com/user/status/123 #T           ← 大小写不敏感
https://x.com/user/status/123 #翻译
```

- 独立 token（``#test`` 不算）
- 正文与 **inline query** 都识别
- 与 ``#剧透`` 可同时出现

## 接口怎么运作

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

## 只换正文，其它一律不动

``apply_translation`` 只改写解析结果的正文字段。作者行 / 发布时间 / 浏览量 /
点赞 / 标签 / 页脚 / 折叠 / **媒体** 全由项目既有链路（``send_rich_media``）
按统一形态生成 —— 所以 ``#t`` 的输出与不加 ``#t`` **同形**，唯一差别是正文语言。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from parsehub.errors import TranslationUnavailable
from parsehub.provider_api.twitter import Twitter
from parsehub.utils.helpers import SecretCookie

from core import pl_cfg
from log import logger

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
        可直接作为正文交给项目构建器的 markdown。
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


async def translate_tweet_markdown(url: str, lang: str) -> str | None:
    """取一条推文的译文 markdown；取不到返回 ``None``。

    **失败不抛**：``#t`` 是用户加的一个开关，平台不返回译文时应该照常发原文，
    而不是整条消息发不出去。
    """
    match = _TWEET_URL_RE.search(url)
    if not match:
        return None
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
        translated, entities = await client.translate_tweet(tweet_id, dst_lang)
    except TranslationUnavailable as e:
        logger.warning(f"#t 翻译不可用: tweet_id={tweet_id} lang={dst_lang} err={e}")
        return None
    except Exception as e:  # noqa: BLE001 - 翻译失败不能把整条解析弄挂
        logger.exception(f"#t 翻译异常: tweet_id={tweet_id} err={e}")
        return None
    return to_markdown(translated, entities)


def set_body(parse_result: object, markdown: str) -> None:
    """把正文写进解析结果。

    **两种结果类各有自己的正文字段，两个都要写**：

    - ``RichTextParseResult.markdown_content`` —— 纯文本/长文推文，渲染器优先读它
    - ``MultimediaParseResult.content`` —— 带图/视频的推文**没有**
      ``markdown_content`` 属性（实测那条 GTA6 推文就是这种），只写前者
      会让多媒体推文的 ``#t`` 完全不生效

    正文一换，原文里的 ``{{media:N}}`` 占位就不成立了；媒体仍由
    ``send_rich_media`` 从 ``parse_result.media`` 正常发出（挂在消息末尾）。
    """
    for field in ("markdown_content", "content"):
        try:
            object.__setattr__(parse_result, field, markdown)
        except (AttributeError, TypeError):
            continue


async def apply_translation(parse_result: object, url: str, lang: str) -> bool:
    """``#t`` 的实现：把 ``parse_result`` 的正文换成平台译文。

    Args:
        parse_result: 已解析的结果（媒体是否下载过无所谓 —— 只改正文）。
        url: 原推文链接。
        lang: 目标界面语言（``zh-hans`` / ``ja-jp`` …）。

    Returns:
        是否真的换成了译文。``False`` 表示平台没给译文，正文保持原文 ——
        调用方**不需要**额外处理，照常发原文即可（用户加的开关不该让消息发不出去）。
    """
    markdown = await translate_tweet_markdown(url, lang)
    if not markdown:
        return False
    set_body(parse_result, markdown)
    return True
