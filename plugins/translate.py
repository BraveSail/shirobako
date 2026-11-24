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

## 只换正文文本，保留完整排版结构（含引用块 / 回复块 / 投票 / 话题卡片）

``apply_translation`` 只替换主帖文本，以及（若存在 ``quote_ids``）并发翻译被回复 /
被引用推文块内的文本。作者行 / 引用署名行 / 引用块结构 / 投票占位符 / trending 卡片 /
发布时间 / 浏览量 / 点赞 / 标签 / 页脚 / 折叠 / **媒体归位** 全部保持原形态，
所以 ``#t`` 的输出与不加 ``#t`` **同形**，唯一差别是正文与引用内容换成了目标语言。
"""

from __future__ import annotations

import asyncio
import copy
import re
from typing import TypeVar
from urllib.parse import quote

from parsehub.provider_api.twitter import Twitter, XTranslationFailure
from parsehub.utils import http
from parsehub.utils.helpers import SecretCookie, _quote_line

from core import pl_cfg
from log import logger
from plugins.helpers import split_quote_blocks
from services.redis_client import cache_key, get_optional, set_with_ttl

logger = logger.bind(name="Translate")

#: 译文缓存 TTL（7 天）—— 推文内容与译文稳定，缓存后重复 ``#t`` 请求零外部 API 往返
_TRANSLATE_CACHE_TTL = 7 * 24 * 60 * 60

_T = TypeVar("_T")

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

#: 主帖 / 引用块里需要原样保留的结构占位符（投票 / 媒体）
_POLL_LINE_RE = re.compile(r"^\{\{poll:\d+\}\}$")
_QUOTE_STRUCT_LINE_RE = re.compile(r"^>\s*(?:\{\{(?:poll|media):\d+\}\}|>.*)$")


def _is_formatted_quote_block(block: str) -> bool:
    """是否为解析器通过 ``format_quote_block`` 产出的引用/回复块（首行 ``> <i>``）。"""
    first = next((ln.lstrip() for ln in block.splitlines() if ln.strip()), "")
    return first.startswith("> <i>")


def _is_trending_card(block: str) -> bool:
    """是否为 ``TwitterParser._trending_block`` 产出的话题卡片引用块。"""
    if not block:
        return False
    return "/i/trending/" in block or "> [查看原話題](" in block or "> {{media:" in block


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


def _build_twitter_client() -> Twitter:
    # **必须走 `SecretCookie`**：``pl_cfg.roll_cookie`` 返回的是 ``MaskedSecretStr``，
    # 它自己的 ``get_value()`` 是脱敏显示（14 字符的 ``gues******znn.``），
    # 直接传给客户端会得到「缺少 ct0」。`SecretCookie.get_value()` 才返回
    # 真正的 ``dict[str, str]``（实测该 cookie 有 1259 字符、12 个字段）。
    raw_cookie = pl_cfg.roll_cookie("twitter")
    cookie = SecretCookie(raw_cookie.get_secret_value() if raw_cookie else None).get_value()
    return Twitter(cookie=cookie, proxy=pl_cfg.roll_parser_proxy("twitter"))


def _safe_tweet_id(tweet_id: str) -> str:
    return tweet_id if re.fullmatch(r"[0-9]{1,20}", tweet_id) else "unknown"


def _tweet_id_for_log(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    match = _TWEET_URL_RE.match(path)
    if match and (match.end() == len(path) or path[match.end()] == "/"):
        return _safe_tweet_id(match[1])
    return "unknown"


def _log_failure(tweet_id: str, dst_lang: str, error: Exception, *, stage: str, source: str | None = None) -> None:
    details: dict[str, str | int]
    if isinstance(error, XTranslationFailure):
        stage, reason, details = error.stage, error.reason, error.details
    elif stage == "translate_tweet" and isinstance(error, http.RequestError):
        stage = "request"
        reason = "request_timeout" if isinstance(error, http.TimeoutException) else "transport_error"
        details = {"error_type": type(error).__name__}
        if error.code == 28:  # CURLE_OPERATION_TIMEDOUT
            details["curl_code"] = 28
    else:
        reason, details = "unexpected_error", {"error_type": type(error).__name__}
    summary = " ".join(f"{name}={value}" for name, value in details.items())
    # Never log exception objects or chains here: a decoder's doc and request locals can contain secrets.
    logger.warning(
        "#t 翻译失败: tweet_id={} lang={} stage={} reason={} fallback=original{}{}",
        _safe_tweet_id(tweet_id), dst_lang, stage, reason, f" {summary}" if summary else "",
        f" source={source}" if source else "",
    )


def log_translation_fallback(url: str, lang: str, error: Exception, *, source: str) -> None:
    """Log a presentation failure without echoing the URL or exception."""
    _log_failure(_tweet_id_for_log(url), x_lang_for(lang), error, stage="apply_translation", source=source)


def log_translation_outcome(url: str, lang: str, changed: bool, *, label: str) -> None:
    logger.info("{}: changed={} tweet_id={} lang={}", label, changed, _tweet_id_for_log(url), x_lang_for(lang))


async def _translate_id_markdown(client: Twitter, tweet_id: str, dst_lang: str) -> str | None:
    """用既有 Twitter 客户端翻译单条推文 id（优先读 Redis 译文缓存）；不可用或失败返回 ``None``。"""
    key = cache_key("translate", tweet_id, dst_lang)
    if cached := await get_optional(key):
        logger.debug(f"#t 译文缓存命中: tweet_id={tweet_id} lang={dst_lang}")
        return cached
    try:
        translated, entities = await client.translate_tweet(tweet_id, dst_lang)
    except Exception as e:  # noqa: BLE001 - 翻译失败不能把整条解析弄挂
        _log_failure(tweet_id, dst_lang, e, stage="translate_tweet")
        return None
    md = to_markdown(translated, entities)
    if md:
        await set_with_ttl(key, md, _TRANSLATE_CACHE_TTL)
    return md


async def translate_tweet_markdown(url: str, lang: str) -> str | None:
    """取一条推文的译文 markdown；取不到返回 ``None``。

    **失败不抛**：``#t`` 是用户加的一个开关，平台不返回译文时应该照常发原文，
    而不是整条消息发不出去。
    """
    match = _TWEET_URL_RE.search(url)
    if not match:
        return None
    client = _build_twitter_client()
    return await _translate_id_markdown(client, match[1], x_lang_for(lang))


def replace_quote_block_body(quote_block: str, translated_md: str) -> str:
    """只替换引用块（``> ...``）里的正文文本，保留作者署名行与块内结构行。

    ``format_quote_block`` 产出的引用块形态固定为：
    - 第 0 行：``> <i><a href="...">作者</a> <code>@handle</code></i>``（署名行）
    - 后续行：``> <i>正文行</i>`` / ``>`` / ``> {{poll:N}}`` / ``> {{media:N}}`` / ``> > YouTube卡片``

    纯图引用块（只有署名行、没有正文）原样返回，避免凭空塞出多余文本。
    """
    body = (translated_md or "").strip()
    if not quote_block or not body:
        return quote_block

    lines = quote_block.splitlines()
    if not lines:
        return quote_block

    # 识别署名行（TwitterParser._quote_block 统一由 format_author_link 生成）
    has_author_header = (
        lines[0].startswith("> <i>") and ("<a " in lines[0] or "<code>@" in lines[0])
    )
    header = lines[0] if has_author_header else ""
    rest = lines[1:] if has_author_header else lines

    # 若原块仅有署名行（纯图引用）或仅有结构行（无文字），保持原样
    struct_lines: list[str] = []
    has_text_line = False
    for line in rest:
        stripped = line.strip()
        if not stripped or stripped == ">":
            continue
        if _QUOTE_STRUCT_LINE_RE.match(stripped):
            struct_lines.append(line)
        else:
            has_text_line = True

    if not has_text_line:
        return quote_block

    rendered_lines = [_quote_line(line) for line in body.splitlines()]
    out: list[str] = []
    if header:
        out.append(header)
    out.extend(rendered_lines)
    if struct_lines:
        out.append(">")
        out.extend(struct_lines)
    return "\n".join(out)


def _extract_middle_tail_extras(middle: str) -> list[str]:
    """抽取主帖 ``middle`` 末尾由 ``_compose`` 追加的结构块（投票占位符 / trending 卡片）。

    当一条推文**同时**带有 trending 卡片与被引用推文时，``split_quote_blocks`` 只把
    最末尾的被引用推文切进 ``tail``，trending 卡片和 ``{{poll:N}}`` 会留在 ``middle``
    末尾。替换主帖文本时必须把这些结构段留下来。
    """
    if not middle:
        return []
    blocks = [b for b in re.split(r"\n{2,}", middle.strip()) if b.strip()]
    extras: list[str] = []
    while blocks:
        last = blocks[-1].strip()
        last_lines = [ln.strip() for ln in last.splitlines() if ln.strip()]
        is_poll = len(last_lines) == 1 and bool(_POLL_LINE_RE.match(last_lines[0]))
        is_trending = bool(last_lines) and all(ln.startswith(">") for ln in last_lines) and _is_trending_card(last)
        if is_poll or is_trending:
            extras.append(blocks.pop())
        else:
            break
    extras.reverse()
    return extras


def _existing_body(parse_result: object) -> str:
    """读取解析结果当前的正文（优先 ``markdown_content``，再退 ``content``）。"""
    md = getattr(parse_result, "markdown_content", None)
    if isinstance(md, str) and md:
        return md
    content = getattr(parse_result, "content", None)
    return content if isinstance(content, str) else ""


def set_body(
    parse_result: object,
    markdown: str | None,
    *,
    reply_markdown: str | None = None,
    quoted_markdown: str | None = None,
) -> None:
    """把译文写进解析结果，同时**完整保留**开头回复块、末尾引用块、投票与话题卡片。

    - 仅替换 ``middle`` 的纯文本主体（保留其中的 ``{{poll:N}}`` 与内嵌 trending 块）。
    - 若提供了 ``reply_markdown`` / ``quoted_markdown``，同步替换对应引用块内的文本，
      署名行与块内占位符（``{{poll:N}}`` / ``{{media:N}}`` / 嵌套卡片）保持不动。
    - ``markdown`` 为 ``None`` 时保持主帖原文，仅更新提供的引用块译文（常见于中文主帖
      引用外语推文：主帖无需翻译，引用块仍可译成中文）。
    - **两种结果类各有自己的正文字段，两个都要写**：
      ``RichTextParseResult.markdown_content`` 与 ``MultimediaParseResult.content``。
    """
    existing = _existing_body(parse_result)
    head, middle, tail = split_quote_blocks(existing) if existing else ("", "", "")
    quote_roles = [str(r) for r in (getattr(parse_result, "quote_roles", None) or []) if r]

    # 区分「解析器生成的回复/引用/话题卡片」与「作者在正文里自己写的 `> ` 段落」：
    # 作者自写的 `> ` 属于正文的一部分，不能被当成外部引用块原样拼回译文末尾。
    if head and not ("reply" in quote_roles or (not quote_roles and _is_formatted_quote_block(head))):
        middle = f"{head}\n\n{middle}".strip() if middle else head
        head = ""
    if tail and not (
        "quoted" in quote_roles
        or (not head and quote_roles == ["reply"])
        or _is_trending_card(tail)
        or (not quote_roles and _is_formatted_quote_block(tail))
    ):
        middle = f"{middle}\n\n{tail}".strip() if middle else tail
        tail = ""

    # 把 reply / quoted 译文准确映射到 head / tail：
    # 当只有 1 个引用块且主帖正文为空时，split_quote_blocks 会把唯一块放进 tail，
    # 此时看 quote_roles 决定它到底是 reply 还是 quoted。
    if head and reply_markdown:
        head = replace_quote_block_body(head, reply_markdown)
    if tail:
        tail_role = "reply" if (not head and quote_roles == ["reply"]) else "quoted"
        tail_md = reply_markdown if tail_role == "reply" else quoted_markdown
        if tail_md:
            tail = replace_quote_block_body(tail, tail_md)

    if markdown is not None:
        extras = _extract_middle_tail_extras(middle)
        middle_parts = [p for p in [markdown.strip(), *extras] if p]
        new_middle = "\n\n".join(middle_parts)
    else:
        new_middle = middle

    composed = "\n\n".join(p for p in (head, new_middle, tail) if p)

    for field in ("markdown_content", "content"):
        try:
            object.__setattr__(parse_result, field, composed)
        except (AttributeError, TypeError):
            continue


async def apply_translation(parse_result: object, url: str, lang: str) -> bool:
    """``#t`` 的实现：把 ``parse_result`` 的主帖与引用推文换成平台译文。

    Args:
        parse_result: 已解析的结果（媒体是否下载过无所谓 —— 只改文本部分）。
        url: 原推文链接。
        lang: 目标界面语言（``zh-hans`` / ``ja-jp`` …）。

    Returns:
        是否真的换成了译文（主帖或任一引用推文翻译成功即为 ``True``）。
        ``False`` 表示平台都没给译文，正文保持原文 —— 调用方**不需要**额外处理，
        照常发原文即可（用户加的开关不该让消息发不出去）。
    """
    match = _TWEET_URL_RE.search(url)
    if not match:
        return False

    main_id = match[1]
    dst_lang = x_lang_for(lang)
    quote_ids = dict(getattr(parse_result, "quote_ids", None) or {})
    reply_id = str(quote_ids.get("reply") or "").strip()
    quoted_id = str(quote_ids.get("quoted") or "").strip()

    client = _build_twitter_client()

    # 并发请求主帖与引用推文翻译，避免串行放大延迟
    async def _maybe(tid: str) -> str | None:
        return await _translate_id_markdown(client, tid, dst_lang) if tid else None

    main_md, reply_md, quoted_md = await asyncio.gather(
        _translate_id_markdown(client, main_id, dst_lang),
        _maybe(reply_id),
        _maybe(quoted_id),
    )

    if not any((main_md, reply_md, quoted_md)):
        return False

    set_body(
        parse_result,
        main_md,
        reply_markdown=reply_md,
        quoted_markdown=quoted_md,
    )
    return True


async def translate_for_display(parse_result: _T, url: str, lang: str) -> _T:
    """浅拷贝 ``parse_result`` 并就地翻译正文/引用，原对象不动（用于进度展示与发送）。"""
    cloned = copy.copy(parse_result)
    try:
        changed = await apply_translation(cloned, url, lang)
        log_translation_outcome(url, lang, changed, label="#t 翻译结果")
    except Exception as e:  # noqa: BLE001 - 翻译失败不能阻断解析与发送
        log_translation_fallback(url, lang, e, source="display")
        return parse_result
    return cloned if changed else parse_result


async def translate_cache_entry(entry: _T, url: str, lang: str) -> _T:
    """克隆持久化缓存条目并翻译其 ``parse_result``（复用已上传的 file_id，零下载/零上传）。"""
    pr = entry.parse_result.model_copy(deep=True)  # type: ignore[attr-defined]
    try:
        changed = await apply_translation(pr, url, lang)
        log_translation_outcome(url, lang, changed, label="#t 缓存命中翻译")
    except Exception as e:  # noqa: BLE001 - 翻译失败不能让缓存直发失败
        log_translation_fallback(url, lang, e, source="cached_display")
        return entry
    return entry.model_copy(update={"parse_result": pr}) if changed else entry  # type: ignore[attr-defined]
