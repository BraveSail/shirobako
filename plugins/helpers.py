"""plugins 共用的工具函数和数据类"""

import html
import re
import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from easy_ai18n import LocaleContent
from parsehub import ParseHub, Platform
from parsehub.types import AnyParseResult, RichTextParseResult
from parsehub.utils.helpers import SPOILER_FOLD_SUMMARY, format_author_label, format_author_link
from pyrogram.types import Message

from i18n import t_
from log import logger
from repo.settings import SettingsConfig

logger = logger.bind(name="Helpers")

COMMANDS = {
    "start": t_("开始"),
    "jx": t_("解析并预览内容"),
    "raw": t_("发送原文件, 避免画质压缩"),
    "zip": t_("打包发送, 附带解析信息"),
    "jxjx": t_("绕过缓存解析"),
    "lang": t_("语言"),
    "cfg": t_("配置"),
}


def build_start_text() -> LocaleContent:
    return t_(
        f"**发送分享链接以进行解析**\n\n"
        f"**支持的平台:**\n"
        f"<blockquote expandable>{get_supported_platforms()}</blockquote>\n\n"
        f"**命令列表:**\n"
        f"<blockquote expandable>"
        f"/jx <链接> - 解析并预览内容\n"
        f"/raw <链接> - 发送原文件, 避免画质压缩\n"
        f"/zip <链接> - 打包发送, 附带解析信息\n"
        f"/jxjx <链接> - 绕过缓存解析\n"
        f"/lang - 语言\n"
        f"/cfg - 配置\n"
        f"/cfg <频道用户名/链接/id> - 频道配置\n"
        f"</blockquote>\n\n"
        f"**开源地址: [GitHub](https://github.com/BraveSail/shirobako)**"
    )


def build_caption(
    parse_result: AnyParseResult,
    *,
    custom_content: str = "",
    config: SettingsConfig,
    rich: bool = False,
    allow_blockquote: bool = True,
    allow_expandable: bool = True,
    lang: str = "",
    view_label: str = "",
    max_length: int | None = None,
) -> str:
    return build_caption_by_str(
        parse_result.title,
        replace_url(parse_result.platform, parse_result.markdown_content)
        if rich and isinstance(parse_result, RichTextParseResult)
        else parse_result.content,
        parse_result.raw_url,
        platform=parse_result.platform,
        hide_source=config.hide_source,
        custom_content=custom_content,
        author_name=format_author_label(
            get_parse_author_name(parse_result), getattr(parse_result, "author_handle", "")
        ),
        hide_title=config.hide_title,
        hide_desc=config.hide_desc,
        rich=rich,
        allow_blockquote=allow_blockquote,
        allow_expandable=allow_expandable,
        # 投票占位符 (`{{poll:N}}`) 同样要在这里替换 —— 老 caption 路径 (raw/zip)
        # 也是用户能看到的产物, 占位符字面量绝不能泄漏出去。
        polls=getattr(parse_result, "polls", None) or (),
        metadata_line=build_metadata_line(
            published_at=getattr(parse_result, "published_at", None),
            view_count=getattr(parse_result, "view_count", None),
            like_count=getattr(parse_result, "like_count", None),
            lang=lang,
            view_label=view_label,
            # 平台自己的叫法优先（bgm 是「状态」）—— caption 与富文本两条路一致
            like_label=metadata_like_label(parse_result, t_[lang] if lang else t_)
            if (view_label and lang)
            else "",
            reply_label=metadata_reply_label(parse_result, t_[lang] if lang else t_)
            if (view_label and lang)
            else "",
        ),
        max_length=max_length,
        fold_summary=t_[lang]("展开全文") if lang else "",
        lang=lang,
    )


#: 统计行里各段的分隔符
# 标签行的显示宽度预算 (全角记 2), 超出就截断: 一行大约三四十个全角字符
TAG_LINE_DISPLAY_BUDGET = 60

_METADATA_SEPARATOR = " · "

#: 引用卡片与**图片**之间的间距（用户报「图片和引用贴在一起」）。
#:
#: 用**分割线**（实测产出独立的 ``Divider`` 块）。曾经试过全角空格段 ——
#: 也能产出独立段落，但**太宽**（用户「这个不行太宽了，试试分割线」）。
#:
#: ⚠️ 别改成空行或半角空格：连续空行与半角空格段都会被服务端**折叠**
#: （实测块结构与不加时完全相同），间距会静默消失且没有任何报错。
_QUOTE_GAP = "---"


def _is_quote_card(part: str) -> bool:
    """这一段是不是引用卡片。

    两种形态都要认：**短引用**走 ``> …`` 的 markdown 形态（服务端自己解析成引用块），
    **长引用/带媒体**走 ``<blockquote>`` 容器形态（``render_folded_quote_card``）。
    """
    head = part.lstrip()
    return head.startswith("<blockquote") or head.startswith(">")


def _is_media_block(part: str) -> bool:
    """这一段是不是媒体块（图片/视频占位符或图集容器）。

    ⚠️ 卡片**内部**的图（``> ![](...)``）算卡片的一部分，不走这里 —— 那种图必须
    和卡片贴在一起（脱离就掉出卡片）。
    """
    head = part.lstrip()
    return head.startswith("![]") or head.startswith("<tg-collage>")


def _append_gap_before_media(parts: list[str]) -> None:
    """媒体块**前面是引用卡片**时，插一条分割线。

    用户报「图片和引用贴在一起」—— 卡片与图片是两个相邻的独立块，中间要隔开。
    """
    if parts and _is_quote_card(parts[-1]):
        parts.append(_QUOTE_GAP)


def _append_gap_before_card(parts: list[str]) -> None:
    """引用卡片**前面是媒体块或另一张引用卡片**时，插一条分割线（同上，方向相反）。

    卡片与卡片相邻时也要隔开 —— "回复了 A 且引用 B"的推文、正文为空时两块直接
    贴上（富文本里两段之间只隔一个空行会被折叠），用户报「引用和引用之间也分割线
    隔一下」。
    """
    if parts and (_is_media_block(parts[-1]) or _is_quote_card(parts[-1])):
        parts.append(_QUOTE_GAP)

#: 元数据时间按该时区渲染 (容器是 UTC, 必须显式指定, 否则会差 8 小时)
_METADATA_TIMEZONE = "Asia/Shanghai"


#: 页脚里"点赞"那一段在**各平台叫什么**。bgm 没有"点赞"这个概念 —— 它的表情回应
#: （页面上的 ``data_likes_list``）才是同一种正反馈，站点自己叫**「状态」**。
_LIKE_LABEL_KEYS = {Platform.BANGUMI: "状态"}

#: 页脚里"回复"那一段在**各平台叫什么**。微博把自己的回复叫**「评论」** ——
#: 它的 ``comments_count`` 在站点 UI 上就是"评论"（页脚写「回复」会与用户在微博上
#: 看到的字样不一致）。其余平台仍是「回复」。
_REPLY_LABEL_KEYS = {Platform.WEIBO: "评论"}


def metadata_like_label(parse_result: AnyParseResult, translate: Callable[[str], str]) -> str:
    """页脚里"点赞"那一段的文案 —— 平台有自己的叫法时用它，否则空串（走默认「点赞」）。

    ``translate`` 传当前语言的 ``t_``（``t_[lang]``），这样文案跟随用户语言。
    """
    key = _LIKE_LABEL_KEYS.get(getattr(parse_result, "platform", None))
    return translate(key) if key else ""


def metadata_reply_label(parse_result: AnyParseResult, translate: Callable[[str], str]) -> str:
    """页脚里"回复"那一段的文案 —— 平台有自己的叫法时用它（微博 → 「评论」），否则空串。"""
    key = _REPLY_LABEL_KEYS.get(getattr(parse_result, "platform", None))
    return translate(key) if key else ""


def build_metadata_line(
    *,
    published_at: datetime | None = None,
    view_count: int | None = None,
    like_count: int | None = None,
    reply_count: int | None = None,
    lang: str = "",
    view_label: str = "",
    like_label: str = "",
    reply_label: str = "",
) -> str:
    """把发布时间/浏览量/点赞/回复渲染成一行,

    例如「2026年10月3日 19:00 · 1,455 查看 · 158 点赞 · 32 回复」。

    时间那段是个时间戳实体 (客户端会按本地时区重新渲染并本地化)。

    平台不提供的项直接跳过, 不会留下空占位符 —— 页面位置也**不预留**
    (拿不到就不显示, 与各平台同一条原则)。
    view_label / like_label / reply_label 由调用方用 ``t_[lang]("查看")`` 这类提供;
    不传时按 ``lang`` 兜底 (以前兜底走模块级 ``t_`` = 默认语言, 与用户语言不一致)。
    """
    parts: list[str] = []
    if published_at:
        parts.extend(_format_published(published_at, lang))
    if view_count is not None:
        fallback = t_[lang]("查看") if lang else t_("查看")
        parts.append(f"{view_count:,} {view_label or fallback}".strip())
    if like_count is not None:
        like_fallback = t_[lang]("点赞") if lang else t_("点赞")
        parts.append(f"{like_count:,} {like_label or like_fallback}".strip())
    if reply_count is not None:
        reply_fallback = t_[lang]("回复") if lang else t_("回复")
        parts.append(f"{reply_count:,} {reply_label or reply_fallback}".strip())
    return _METADATA_SEPARATOR.join(part for part in parts if part)


def _format_published(value: datetime, lang: str) -> list[str]:
    """发布时间 —— 走 Telegram 的时间戳实体, 由**客户端按本地时区**渲染。

    服务端只发一个 UTC unix 时间戳 (``<tg-time unix=...>``), 显示交给客户端:
    于是每个用户看到的时间都与自己的时区相符, 而不是消息里写死的那一刻。
    (以前固定按 ``Asia/Shanghai`` 算好再发出去 —— 人在别的时区看到的就是"北京时间"。)

    ``format="Dt"`` = 长日期 + 短时间, 即「2026年10月4日 12:34」; 短时间 ``t`` 是
    24 小时制 (``16:20``)。实际呈现仍由客户端本地化, 12/24 小时制跟随用户设备设置。

    标签里的文字是**兜底**: 老客户端不认 ``tg-time`` 时照常显示, 所以内容与
    客户端渲染的形态保持一致。
    """
    local = value
    try:
        local = value.astimezone(ZoneInfo(_METADATA_TIMEZONE))
    except Exception:  # noqa: BLE001 - 时区数据缺失时退回原时区, 不该让整条消息失败
        pass

    clock = f"{local.hour:02d}:{local.minute:02d}"
    # 兜底文字: 中日用「年月日」书写 (日语也一样); 其余语言用 ISO 日期
    if lang.startswith(("zh", "ja")):
        fallback = f"{local.year}年{local.month}月{local.day}日 {clock}"
    else:
        fallback = f"{local.strftime('%Y-%m-%d')} {clock}"
    # unix 取自归一到 _METADATA_TIMEZONE 后的 local (aware), 与原来的日期算法同源
    return [f'<tg-time unix="{int(local.timestamp())}" format="Dt">{fallback}</tg-time>']


def build_caption_by_str(
    title: str | None,
    content: str | None,
    raw_url: str,
    *,
    platform: Platform | None = None,
    hide_source: bool = False,
    custom_content: str = "",
    author_name: str = "",
    hide_title: bool = False,
    hide_desc: bool = False,
    rich: bool = False,
    allow_blockquote: bool = True,
    allow_expandable: bool = True,
    metadata_line: str = "",
    max_length: int | None = None,
    fold_summary: str = "",
    lang: str = "",
    polls: Sequence | None = None,
) -> str:
    """构建消息正文：标题 + 内容 + 统计行 + 来源链接"""
    title, content = title or "", content or ""
    # 投票占位符 (`{{poll:N}}`) 先替换 —— 这条 (老 caption) 路径同样是用户可见产物,
    # 字面量绝不能泄漏。polls 为空时原样返回 (零变化)。两个分支都要, 所以放这里。
    content = replace_poll_placeholders(content, polls, lang)
    if rich:
        body = f"### {title}\n\n <details><summary>📃</summary>\n\n{content}\n\n</details>"
    else:
        parts = []
        if not hide_title and title:
            parts.append(f"**{neutralize_markdown(title)}**")
        if not hide_desc and content:
            # 正文里的 URL 也要中和: URL 中的 '__' 会被 markdown 解析插进 <i> 破坏链接
            parts.append(neutralize_markdown_urls(content))
        body = format_text(
            ("\n\n".join(parts)).strip(),
            allow_blockquote=allow_blockquote,
            allow_expandable=allow_expandable,
            max_length=max_length,
            fold_summary=fold_summary,
        )

    if author_name:
        label = neutralize_markdown(html.escape(author_name))
        body = f"<b>{label}:</b>\n\n{body}" if body else f"<b>{label}:</b>"

    if custom_content:
        body += f"\n\n{custom_content}"

    if metadata_line:
        body = f"{body}\n\n{metadata_line}" if body else metadata_line

    if hide_source:
        return body
    platform = platform or ParseHub().get_platform(raw_url)
    display = neutralize_markdown(html.escape(platform.display_name)) if platform else ""
    # 来源标签要跟随用户语言 (以前硬编码英文 "Source（…）", 任何语言都显示英文)
    _t = t_[lang] if lang else t_
    source = _t(f"来源（{display}）") if display else _t("来源")
    # href 里的 URL 必须中和: 否则 pyrogram 会把 URL 中的 '__' 解析成 <i> 塞进 href
    safe_url = neutralize_markdown(raw_url)
    return f"{body}\n\n{format_label(f"<a href='{safe_url}'>{source}</a>")}"


def _meta_divider(meta_parts: Sequence[str], parts: Sequence[str]) -> list[str]:
    """元信息 (标题/作者) 与内容之间的分割线。

    用户要求「作者下面的分割线」—— 让"谁发的"和"发了什么"之间有一道明显的界,
    与**内容与页脚之间**那条 (``body`` 拼接时加的 ``---``) 同一形态。

    三个条件都满足才插, 否则不加多余的线:

    - **有作者行** (只有标题时用户没要求, 不引入新的视觉变化)
    - **后面还有内容** (纯元信息没有要分隔的东西)
    - 元信息段里作者不是唯一一个空壳 (由调用方过滤后传入)
    """
    has_author = any(part and not part.startswith("### ") for part in meta_parts)
    has_content = any(part for part in parts)
    return ["---"] if (has_author and has_content) else []


#: lib 放进正文的**投票占位符**行: ``{{poll:N}}`` (引用块内形如 ``> {{poll:0}}``)。
#: N = ``result.polls`` 里的索引 —— 渲染成表格由本模块负责 (**按用户语言**),
#: lib 只出数据 (见计划「方案 C」)。
_POLL_PLACEHOLDER_RE = re.compile(r"^(> )?\{\{poll:(\d+)\}\}$")

#: lib 放进正文的**原位媒体标记**: ``{{media:N}}``（N = 正文媒体占位符下标）。
#: 媒体占位符 ``![](tg://photo?id=mN)`` 是**内联**结构（可以出现在行中），所以
#: 这里用子串匹配（不像 ``{{poll:N}}`` 是块级表格、要求整行）。
_MEDIA_PLACEHOLDER_RE = re.compile(r"\{\{media:(\d+)\}\}")

#: 正文里的**外链图**（``![](https://…)``）。富文本要求正文里的图必须引用**已上传**的
#: 媒体（``tg://photo?id=``）；外链 URL 由 Telegram 服务端去抓，抓不到（SVG 等非位图、
#: 防盗链、404）就**整条消息被拒**（``400 RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND``，
#: 2026-10-16 linux.do 的 onebox SVG 实锤）。所以正文外链图要么换成上传媒体引用
#: （URL 能对上 media 时），要么剥掉（对不上时）。
_EXTERNAL_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^)\s]+)[^)]*\)")


def rewrite_external_images(content: str, media_urls: Sequence[str]) -> str:
    """正文里的外链图 → ``{{media:N}}`` 标记（对不上 media 的剥掉）。

    N = ``media_urls`` 里的下标（该列表与 ``build_rich_media`` 产出的占位符平铺对齐）。

    为什么必须处理: 富文本的正文图只能是 ``tg://`` 上传媒体引用；外链图交给 Telegram
    服务端抓，抓不到就**整条消息 400**（SVG 必炸 —— 服务端不把 SVG 当图抓）。而 lib
    抽图时是按附件渠道走的（``media``），正文里的 ``![](url)`` 是原始外链形态 ——
    两边各写各的，正文那份不换掉就会炸。

    对不上的（图不在 media 里，例如平台的 link-preview 图标没被抽）**剥掉**:
    宁可少一张装饰图，也不能让整条消息发不出去。
    """
    if not content or "![" not in content:
        return content
    index_by_url = {url: i for i, url in enumerate(media_urls) if url}

    def _replacement(match: re.Match[str]) -> str:
        url = match.group(1).strip()
        index = index_by_url.get(url)
        if index is None:
            logger.warning(f"正文外链图不在媒体列表里, 已剥掉(防 Telegram 拒收): {url[:120]}")
            return ""
        return f"{{{{media:{index}}}}}"

    return _EXTERNAL_IMAGE_RE.sub(_replacement, content)


def replace_media_placeholders(content: str, placeholders: Sequence[str]) -> str:
    """把正文里的 ``{{media:N}}`` 标记换成第 N 个媒体占位符。

    - 索引越界 / ``placeholders`` 为空 (**没有媒体的路径**: inline 文字版、处理过程
      的中间状态、纯文字兜底) → **删掉标记** + warning —— 标记字面量绝不能泄漏给用户
      (与 ``replace_poll_placeholders`` 同一纪律)。
    - 引用块内 (``> {{media:0}}``) → 产物带 ``> `` 前缀, 媒体留在引用块里。

    只能替换**正文段**的占位符: 参数只收正文媒体那一段 (``build_rich_media``
    第二个返回值), 引用块媒体不在这个列表里 —— 索引天然对齐。
    """
    if not content or "{{media:" not in content:
        return content

    def _replacement(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(placeholders):
            logger.warning(f"媒体标记没有对应媒体, 已删除: index={index}, media={len(placeholders)}")
            return ""
        # 子串替换: 标记前面的内容 (含引用块的 '> ' 前缀) 原样留在正文里,
        # 所以这里直接给占位符即可 —— 行中/行首/引用块内都自然对齐。
        return placeholders[index]

    return _MEDIA_PLACEHOLDER_RE.sub(_replacement, content)


def _consumed_media_indices(content: str) -> set[int]:
    """正文里所有 ``{{media:N}}`` 标记用到的下标集合（供末尾追加列表去重）。"""
    return {int(m.group(1)) for m in _MEDIA_PLACEHOLDER_RE.finditer(content)}


def _poll_field(source: object, name: str, default=None):
    """读一个投票字段 —— lib 给的是对象 (属性访问), 缓存里是 dict (键访问)。"""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _poll_int(value: object) -> int | None:
    """投票里的计数字段 → int; 缺失/非法一律 None (渲染时跳过该段)。"""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _poll_cell(text: str) -> str:
    """表格单元格转义 —— 选项文案里的 ``|`` 会把列切断 (与 lib 侧同一手法)。"""
    return text.replace("|", "\\|")


def _render_poll(poll, lang: str = "") -> str:
    """把一个投票 (``{{poll:N}}`` 对应的数据) 渲染成本地化的 markdown。

    形态与改造前 lib 直接拼出的表格一致, 只有文案换成用户语言:

    - **头部**: ``voters`` 非 None (linux.do) → ``📊 投票（「标题」 · N 人参与）``;
      ``voters`` 为 None 且只有总票数 (youtube) → ``投票 · 共 N 票``;
      其余 (twitter / threads) **无头部**, 直接表格。
    - **表格**: 任一选项带票数 → 三列 (选项 / 票数 / 占比); 全都没有 (youtube 匿名) → 单列。
    - 占比的分母: **参与人数**优先 (linux.do 旧形态按它算), 再退总票数, 最后按选项票数求和。

    ⚠️ ``total`` / ``voters`` / ``details`` 这些**变量名不能改** —— i18n 的键取自
    f-string 里的表达式源码, 改名即换键, 译文会静默 fallback 回中文。
    """
    options = list(_poll_field(poll, "options") or [])
    if not options:
        return ""
    # 「有语言就用它, 否则退回默认语言」—— 与 build_rich_markdown 里其它文案同一兜底。
    # ⚠️ **别名必须叫 `_t`**: easy_ai18n 只认调用点源码里的 ``t_`` / ``_t`` 两个字面量名,
    # 换成别的名字 (如 ``translate``) 会静默退回原文 (模板解析不到 → 键对不上)。
    _t = t_[lang] if lang else t_
    counts = [_poll_int(_poll_field(option, "count")) for option in options]
    voters = _poll_int(_poll_field(poll, "voters"))
    total_votes = _poll_int(_poll_field(poll, "total_votes"))
    title = str(_poll_field(poll, "title") or "").strip()
    # 三列判据: 有任一选项带票数
    has_counts = any(count is not None for count in counts)
    # 占比分母 (只在三列时用到): 参与人数 → 总票数 → 选项票数求和
    percent_base = voters or total_votes or sum(count or 0 for count in counts)

    lines: list[str] = []
    if voters is not None:
        # linux.do: 「📊 投票（「标题」 · N 人参与）」—— 两段都可能缺
        details_parts: list[str] = []
        if title:
            details_parts.append(_t(f"「{title}」"))
        if voters > 0:
            # 与改造前 lib 的拼法逐字一致: `{voters} 人参与` 不加千分位 (键里也没有 `:,`)
            details_parts.append(_t(f"{voters} 人参与"))
        head = _t("📊 投票")
        if details_parts:
            details = " · ".join(details_parts)
            head += _t(f"（{details}）")
        lines.append(head)
    elif total_votes is not None and not has_counts:
        # youtube: 匿名只给选项与总票数 → 「投票 · 共 N 票」(空行分隔表格, 与旧形态一致)
        head_parts = [_t("投票")]
        if total_votes:
            # 变量名 total 是 i18n 键的一部分 (`共 {total:,} 票`), 不能改名
            total = total_votes
            head_parts.append(_t(f"共 {total:,} 票"))
        lines.append(" · ".join(head_parts))
    if lines:
        lines.append("")

    if has_counts:
        option_label = _t("选项")
        votes_label = _t("票数")
        share_label = _t("占比")
        lines.append(f"| {option_label} | {votes_label} | {share_label} |")
        lines.append("| --- | --- | --- |")
        for option, count in zip(options, counts, strict=True):
            cell = _poll_cell(str(_poll_field(option, "text") or ""))
            # 与改造前 lib 一致: 票数不加千分位 (只有总票数 `共 {total:,} 票` 有)
            count_text = "" if count is None else str(count)
            percent = f"{round(count * 100 / percent_base) if percent_base else 0}%" if count is not None else ""
            lines.append(f"| {cell} | {count_text} | {percent} |")
    else:
        # 全都没有单项票数 (youtube 匿名 / linux.do 老站点退化): 单列表格
        option_label = _t("选项")
        lines.append(f"| {option_label} |")
        lines.append("| --- |")
        for option in options:
            cell = _poll_cell(str(_poll_field(option, "text") or ""))
            lines.append(f"| {cell} |")
    return "\n".join(lines)


def replace_poll_placeholders(content: str, polls: Sequence | None, lang: str = "") -> str:
    """把正文里的 ``{{poll:N}}`` 占位符行替换成按用户语言渲染的投票表格。

    - ``polls`` 为空 (老缓存 / 没有投票的帖子) → **原样返回**, 零变化。
    - 索引越界 → **整行删除** + warning —— 占位符字面量绝不泄漏给用户。
    - 引用块内的占位符 (``> {{poll:0}}``) → 渲染产物每行都带 ``> `` 前缀
      (空行写成 ``>``, 与 lib 的 ``_quote_line`` 同一形态; 表格行不包 ``<i>``)。
    """
    if not content or not polls:
        return content
    out: list[str] = []
    for line in content.split("\n"):
        match = _POLL_PLACEHOLDER_RE.match(line)
        if match is None:
            out.append(line)
            continue
        index = int(match.group(2))
        if index >= len(polls):
            logger.warning(f"投票占位符索引越界, 已删除该行: index={index}, polls={len(polls)}")
            continue
        rendered = _render_poll(polls[index], lang)
        if not rendered:
            logger.warning(f"投票数据为空, 已删除占位符行: index={index}")
            continue
        if match.group(1):
            out.extend(f"> {one}" if one.strip() else ">" for one in rendered.split("\n"))
        else:
            out.extend(rendered.split("\n"))
    return "\n".join(out)


#: 可翻译片段占位符 —— lib 输出的**内联短文案**（它不认识用户语言），
#: 渲染层按用户语言替换。语法 ``{{t:源文本}}``（无嵌套花括号）。
#: 例：bgm 归属行的 ``**<a …>小组名</a>** » {{t:讨论}}``。
_TRANSLATABLE_FRAGMENT_RE = re.compile(r"\{\{t:([^{}]+)\}\}")


def _translate_fragment(fragment: str, lang: str) -> str:
    """把占位符片段名翻译成当前语言；不认识的原样返回。

    ⚠️ 每个片段必须写成**字面量调用点**（``t_[lang]("讨论")``）—— easy_ai18n 从调用点
    的源码解析源文本。写成变量传参（``translate(fragment)``）虽然运行时也命中译文，
    但会在日志里留一条 ``I18N parse error`` ERROR（库对"看不见调用点"的已知行为，
    生产累积日志里已有同类条目）。新增片段时照抄这个 shape。
    """
    if fragment == "讨论":
        return t_[lang]("讨论") if lang else t_("讨论")
    return fragment


def replace_translatable_fragments(text: str, lang: str = "") -> str:
    """把 ``{{t:源文本}}`` 片段替换成当前语言的译文。

    词条走**字面量调用点**（见 ``_translate_fragment``）；手工维护 i18n 词条，
    **别跑全量 i18n.build**（静态扫描会把这些调用当孤儿清掉）。
    """
    if not text or "{{t:" not in text:
        return text
    return _TRANSLATABLE_FRAGMENT_RE.sub(lambda m: _translate_fragment(m.group(1), lang), text)


def build_rich_markdown(
    parse_result: AnyParseResult,
    *,
    config: SettingsConfig,
    lang: str = "",
    view_label: str = "",
    custom_content: str = "",
    media_placeholders: Sequence[str] = (),
    quote_media_placeholders: Sequence[str] = (),
    reply_media_placeholders: Sequence[str] = (),
    media_urls: Sequence[str] = (),
    hide_content: str = "",
    progress: str = "",
) -> str:
    """构建富文本 (rich message) 正文: 标题 + 作者 + 原文格式正文 + 媒体 + 页尾。

    :param hide_content: 用户手动要求遮住内容时**命中的标记** (``#nsfw`` /
        ``#spoiler``, 空串=没要求)。非空即把内容折成
        ``<details><summary>⚠️ <标记></summary>`` —— **不留预览**, 与自动折叠不同:
        那是为了别让长正文撑屏, 这是用户明确要藏起来。
        **除标题/作者外全部进折叠**(含图, 用户明确要求"所有东西都遮")。

    :param progress: 处理过程的阶段文案 (如 ``▎下 载 中...``), 放在**页尾的第一段**。
        处理过程用它渲染"最终排版的无媒体版": 主体 (标题/作者/正文/标签) 与结果
        完全一致, 只有页尾那段从"进度"变成"时间 · 统计"—— 位置不动, 不跳版。

    :param media_urls: 与 ``media_placeholders`` **平铺对齐**的原始 URL 列表
        (``build_rich_media`` 的第六个返回值)。正文里的外链图靠它换成上传媒体引用 ——
        外链图 Telegram 抓不到就整条 400 (SVG 必炸)。空列表时的行为:
        正文外链图**全部剥掉**(防炸), 图仍以末尾附加媒体的形式出现。

    富文本由 Telegram 服务端解析 markdown, 所以正文直接沿用解析器给出的原文格式
    (标题/列表/引用/表格/加粗等都会被还原), 不像旧路径那样先转成 HTML 再拼 caption。
    统计数据与来源放在页尾的 <footer> 里。
    """
    title = (parse_result.title or "").strip()
    # 投票占位符 (`{{poll:N}}`) 的替换必须排在**一切结构处理之前**:
    # lib 只出数据 (结果里的 ``polls``), 表格 (含语言) 由渲染层拼。放在
    # link_hashtags / escape_setext_underlines / preserve_linebreaks 之前,
    # 生成出来的表格行才能和 lib 旧形态一样走完整套处理 (表格行跳过硬换行等)。
    # polls 为空 (老缓存 / 没有投票) 时原样返回, 零变化。
    poll_content = replace_poll_placeholders(
        rich_content(parse_result).strip(),
        getattr(parse_result, "polls", None) or [],
        lang,
    )
    # 正文外链图 (`![](https://…)`) → `{{media:N}}` 标记 (对不上 media 的剥掉):
    # 富文本正文里的图必须是 tg:// 上传媒体引用, 外链由 Telegram 服务端去抓, 抓不到
    # (SVG / 防盗链 / 404) 就**整条 400** —— linux.do 的 onebox SVG 图标就是实锤。
    # **无条件执行**: media_urls 为空时外链图会全部剥掉 (防炸), 图还能从末尾附加
    # 媒体的形式出现; 不剥的话一次 400 就是整条消息都收不到。
    poll_content = rewrite_external_images(poll_content, media_urls)
    # 原位媒体标记 (`{{media:N}}`): lib 把正文图抽出走 media 通道时, 原位置留标记;
    # 这里换成对应的 ``![](tg://photo?id=mN)`` 占位符, 图就留在文章原位
    # (不换的话标记会字面泄漏给用户; media_placeholders 为空时零变化)。
    # 被原位消费的媒体**从末尾追加列表里摘掉** —— 否则同一张图会出现两次
    # (原位一次、末尾一次)。
    body_with_media = replace_media_placeholders(poll_content, media_placeholders)
    if "{{media:" in poll_content:
        consumed = _consumed_media_indices(poll_content)
        media_placeholders = [p for i, p in enumerate(media_placeholders) if i not in consumed]
    # lib 输出的可翻译片段（``{{t:讨论}}`` 这类内联短文案）同样在这里展开。
    content = preserve_linebreaks(
        escape_setext_underlines(
            link_hashtags(
                replace_translatable_fragments(body_with_media, lang),
                parse_result.platform,
                # 平台实体给的标签名（拿不到时为空列表 → 退回正则）
                getattr(parse_result, "hashtags", None),
            )
        )
    )
    # 分两组: 标题/作者是"这是什么"的元信息, 留在折叠外; 其余(正文/引用块/标签/媒体)
    # 都是内容 —— 手动遮住时整组进 details (含图, 用户明确要求"所有东西都遮")。
    meta_parts: list[str] = []
    if title and not config.hide_title:
        # **一级标题** (`#`, size 1 —— 这个 API 里 1 最大、6 最小)。
        # 以前用的是 `###`(size 3), 结果帖子标题比正文里的一级小节还小
        # (实测某 linux.do 帖: 标题 size=3, 正文 `# 小节` size=1), 完全不像标题。
        meta_parts.append(f"# {title}")
    # 归属行（"这条内容属于哪里"）夹在**标题与作者行之间** —— 它是元信息，
    # 放在正文里会挤到引用块前面，把引用块的归位搅乱（bgm 的教训）。
    # lib 侧可能带可翻译片段（``» {{t:讨论}}``）—— 与正文同一套替换。
    if origin := replace_translatable_fragments(
        (getattr(parse_result, "origin_line", "") or "").strip(), lang
    ):
        meta_parts.append(origin)
    if author := format_author_line(parse_result):
        meta_parts.append(author)
    parts: list[str] = []
    # 被回复的卡片在正文前、被引用的卡片在正文后, 各自的媒体留在自己的块里
    reply_quote, body_text, quote = split_quote_blocks(content) if content else ("", "", "")
    fold_summary = t_[lang]("展开全文") if lang else ""
    # 手动遮住时的摘要: ⚠️ + **用户自己写的那个标记** (#nsfw / #spoiler)。
    # 不写「展开全文」—— 折叠按钮的文字对"被藏起来"这件事没有信息量, 而标记本身
    # 说明了为什么藏 (不宜公开 / 剧透); 也不必翻译, 标记是用户输入。
    spoiler_summary = f"⚠️ {hide_content}" if hide_content else SPOILER_FOLD_SUMMARY
    # 引用块的**角色**决定它拿哪一段媒体。角色由平台显式声明（``quote_roles``，
    # 按引用块出现顺序，取值为两个媒体通道名 ``reply`` / ``quoted``）——
    # **位置不参与判断**。没声明时（老缓存 / 未改的平台）走下面那段位置推断。
    declared_roles = [r for r in (getattr(parse_result, "quote_roles", None) or []) if r]
    declared_tail: list[str] = []
    """声明角色时，"正文之后"那一侧的引用卡片（末尾统一追加）"""
    reply_media = list(reply_media_placeholders)
    if declared_roles:
        # 角色**同时**决定两件事：媒体取哪一段、块渲染在正文的**哪一侧**。
        #   ``reply``  → 正文之前（媒体取 reply 段）
        #   ``quoted`` → 正文之后（媒体取 quoted 段）
        # 于是位置彻底不参与判断 —— 无正文的产物（纯图楼层）里引用块会被
        # `split_quote_blocks` 判成"末尾块"，以前媒体就跟着排到它前面去了
        # （真机实测: 图在前、引用块在后）。
        buckets = {"reply": list(reply_media_placeholders), "quoted": list(quote_media_placeholders)}
        pending = list(declared_roles)
        # 正文里抽出的块按出现顺序与声明的角色配对
        paired: list[tuple[str, str, str]] = []  # (位置, 角色, 块)
        for slot, block in (("head", reply_quote), ("tail", quote)):
            if not block:
                continue
            role = pending.pop(0) if pending else ""
            if not role:
                # 声明少于块 —— 按位置兜底（宁可保守，也别把媒体丢了）
                role = "reply" if slot == "head" else "quoted"
                logger.warning(f"块多于声明的引用块角色, {slot} 块按位置兜底为 {role}")
            paired.append((slot, role, block))

        front: list[str] = []
        back: list[str] = []
        for _slot, role, block in paired:
            media = buckets.pop(role, []) if role in buckets else []
            if not media and buckets:  # 桶已被别的块取走 → 取还没被消费的
                pick = next(iter(buckets))
                logger.warning(f"角色 {role} 没有自己的媒体段, 取未消费的 {pick} 段")
                media = buckets.pop(pick)
            # 渲染位置由**角色**决定: reply 在正文前、quoted 在正文后
            (front if role == "reply" else back).append(render_quote_card(block, media, summary=fold_summary)[0])
        # 剩下的媒体段（角色没声明 / 对应块不存在）→ 落到正文媒体，不能丢
        media_placeholders = [*media_placeholders, *(m for rest in buckets.values() for m in rest)]
        if not config.hide_desc:
            # 正文之前那一侧（reply）现在放；正文之后那一侧（quoted）留到末尾
            parts.extend(front)
            declared_tail = back
        # 媒体段已按角色分完，原路径的末尾引用块不再使用
        quote = ""
        quote_media_placeholders = ()
    else:
        # ---- 未声明角色：按**位置**推断（保持与引入 quote_roles 之前逐字一致）----
        # 末尾引用块不存在时, 属于它的媒体要交回**头部**引用块 (反之亦然, 下面那段)。
        # 平台产出"引用块在前"的结构时就会走到这里 (linux.do 把主楼做成引用块放最上面) ——
        # 不兜的话媒体的归属块为空, render_quote_card 直接丢弃它们 (症状: 图不见了)。
        if not config.hide_desc and quote_media_placeholders and not quote:
            if reply_quote:
                reply_media = [*reply_media, *quote_media_placeholders]
            else:
                media_placeholders = [*media_placeholders, *quote_media_placeholders]
            quote_media_placeholders = ()
        if reply_quote and not config.hide_desc:
            # 被回复的卡片也要能折叠: 以前直接拼进 parts, 1294 字的回复块整屏铺开
            parts.extend(render_quote_card(reply_quote, reply_media, summary=fold_summary))
            reply_media = []  # 已安置
        elif not reply_quote:
            # 没有独立的回复块 (例如正文为空): 媒体不能丢, 兜到末尾引用块或正文媒体里
            if quote:
                quote_media_placeholders = [*quote_media_placeholders, *reply_media]
            else:
                media_placeholders = [*media_placeholders, *reply_media]
            reply_media = []
    if body_text and not config.hide_desc:
        # 正文只折叠、不截断: 富文本没有媒体 caption 的 1024 限制,
        # 截断会把长正文变成省略号, 折叠也就轮不上了
        # (手动遮住时不在这里折 —— 整组内容会在下面一起进 details)
        parts.append(body_text if hide_content else format_text(body_text, fold_summary=fold_summary))
    if custom_content:
        parts.append(custom_content)

    # 图集超过阈值时把多出来的折进按钮; 手动打码时整组内容本来就要进 details,
    # 再套一层折叠客户端没有保证 ⇒ 那时不给摘要 (wrap_collage 就不折)。
    if media_placeholders:
        # 卡片与图片之间留一行间距（用户报「图片和引用贴在一起」）
        _append_gap_before_media(parts)
    if hide_content:
        parts.extend(wrap_collage(media_placeholders))
    else:
        # 摘要里的张数写成**局部变量**再进 f-string: i18n 的 key 取的是占位符**源码**，
        # 直接写表达式会得到一整串 `len(...) - _COLLAGE_FOLD_THRESHOLD` 进键名。
        collage_summary = ""
        if lang and len(media_placeholders) > _COLLAGE_FOLD_THRESHOLD:
            count = len(media_placeholders) - _COLLAGE_FOLD_THRESHOLD
            collage_summary = t_[lang](f"🖼 展开其余 {count} 张图片")
        parts.extend(wrap_collage(media_placeholders, fold_summary=collage_summary))

    if (quote or declared_tail) and not config.hide_desc:
        # 图片与卡片之间同样留一行间距（这时图片在卡片**上**面）
        _append_gap_before_card(parts)
    if quote and not config.hide_desc:
        # 被引用/被回复的卡片: 主推自己的媒体要排在它上面 (与 X 上的观感一致)
        parts.extend(render_quote_card(quote, quote_media_placeholders, summary=fold_summary))
    # 声明角色时"正文之后"那一侧（quoted）的卡片 —— 与上面原位：在正文与正文媒体之后
    parts.extend(declared_tail)

    # 标签是**整条内容**的收尾: 排在正文、媒体、引用卡片之后 (与上一块之间自然空一行)。
    #
    # 以前只挂在**正文**之后, 于是"正文末尾的引用块被抽成卡片"的内容会渲染成
    # 「正文 → 标签 → 引用卡片」—— 标签把引用卡片挤到了它下面 (用户报「引用为啥跑 tags
    # 下面」)。那个位置的假设是"正文之后 == 内容末尾", 一旦末尾还有卡片/媒体就不成立。
    # 按原版排列, 标签在所有内容之后。
    if tag_line := format_tags(parse_result):
        parts.append(tag_line)

    if hide_content and (visible := [part for part in parts if part]):
        # 手动遮住: 内容整组进 details —— **正文、引用块、标签、媒体(含图集)全在里面**,
        # 且不留预览 (留了就等于没遮)。只保留标题/作者在外面, 否则看不出这是什么内容。
        inner = "\n\n".join(visible)
        folded = f"<details><summary>{spoiler_summary}</summary>\n\n{inner}\n\n</details>"
        body = "\n\n".join([*[p for p in meta_parts if p], *_meta_divider(meta_parts, parts), folded])
    else:
        body = "\n\n".join(part for part in [*meta_parts, *_meta_divider(meta_parts, parts), *parts] if part)

    footer_parts: list[str] = []
    # 处理过程的进度放页尾第一段: 与最终结果的"时间 · 统计"同一个位置, 主体不动
    if progress:
        footer_parts.append(progress)
    # 当前语言的 t_: 有 lang 就用它, 否则退回默认语言（与既有兜底一致）
    meta_t = t_[lang] if lang else t_
    metadata = build_metadata_line(
        published_at=getattr(parse_result, "published_at", None),
        view_count=getattr(parse_result, "view_count", None),
        like_count=getattr(parse_result, "like_count", None),
        reply_count=getattr(parse_result, "reply_count", None),
        lang=lang,
        view_label=view_label,
        # 平台自己的叫法优先（bgm 是「状态」）, 否则默认「点赞」
        like_label=metadata_like_label(parse_result, meta_t) if (view_label and lang) else "",
        # 微博的回复位叫「评论」（站点自己的字样）, 其余平台默认「回复」
        reply_label=metadata_reply_label(parse_result, meta_t) if (view_label and lang) else "",
    )
    if metadata:
        footer_parts.append(metadata)
    if not config.hide_source:
        platform = parse_result.platform or ParseHub().get_platform(parse_result.raw_url)
        display = platform.display_name if platform else ""
        # 来源标签跟随用户语言 (以前硬编码英文 "Source", 任何语言都显示英文)
        label = t_[lang](f"来源（{display}）") if (display and lang) else (t_[lang]("来源") if lang else "Source")
        # footer 里 markdown 链接语法不生效 (会原样显示成 "[文字](url)"), 必须用 HTML
        href = html.escape(parse_result.raw_url, quote=True)
        footer_parts.append(f'<a href="{href}">{html.escape(label)}</a>')

    if not footer_parts:
        return body
    footer = f"<footer>{_METADATA_SEPARATOR.join(footer_parts)}</footer>"
    return f"{body}\n\n---\n\n{footer}" if body else footer


def build_progress_markdown(
    parse_result: AnyParseResult | None = None,
    *,
    progress: str,
    config: SettingsConfig,
    lang: str = "",
    view_label: str = "",
    spoiler_tag: str = "",
    custom_content: str = "",
    raw_url: str = "",
) -> str:
    """处理过程的消息 —— 与最终结果**同一种排版**。

    用户要求: 处理过程别再用老格式的小字, 要和结果一致 (原话「处理过程的消息能不能和最终
    消息保持一致? 现在格式还是老格式, 只有最终消息是新的」)。

    两个分支:

    - **已有解析结果** → 直接走 ``build_rich_markdown`` (**不带媒体占位符**)。
      标题/作者/正文/标签/页脚全部就位, 与结果唯一的差别是没有媒体 ——
      视觉上就是"正文先出来, 图随后出现", 不再有格式跳变。
    - **还没有结果** (解析阶段) → 骨架: 进度行作正文 + 页尾的来源链接。
      结构与有结果时一致 (正文 + ``---`` + 页脚), 所以结果出来时不会跳版。

    进度文案统一放在**页尾第一段**: 与结果里的"时间 · 统计"同一个位置, 主体位置完全不动。

    :param spoiler_tag: 手动打码标记 —— **处理过程也必须遮**, 否则"下载中"那几秒
        就把该藏的内容露出来了。
    """
    if parse_result is not None:
        return build_rich_markdown(
            parse_result,
            config=config,
            lang=lang,
            view_label=view_label,
            custom_content=custom_content,
            hide_content=spoiler_tag,
            progress=progress,
        )

    # 还没有解析结果: 骨架。正文放进度, 页尾放来源 —— 与有结果时的结构一致
    href = html.escape(raw_url, quote=True) if raw_url else ""
    platform = ParseHub().get_platform(raw_url) if raw_url else None
    display = platform.display_name if platform else ""
    if href and display and lang:
        label = t_[lang](f"来源（{display}）")
    elif lang:
        label = t_[lang]("来源")
    else:
        label = "Source" if href else ""
    footer = f'<footer><a href="{href}">{html.escape(label)}</a></footer>' if href else ""
    return f"{progress}\n\n---\n\n{footer}" if footer else progress


def build_rich_markdown_by_str(
    title: str,
    content: str,
    raw_url: str,
    *,
    config: SettingsConfig,
    lang: str = "",
    view_label: str = "",
    author_name: str = "",
    author_handle: str = "",
    author_url: str = "",
    published_at: datetime | None = None,
    view_count: int | None = None,
    like_count: int | None = None,
    tags: Sequence[str] | None = None,
    custom_content: str = "",
    media_placeholders: Sequence[str] = (),
    quote_media_placeholders: Sequence[str] = (),
    reply_media_placeholders: Sequence[str] = (),
    media_urls: Sequence[str] = (),
    hide_content: str = "",
    platform: Platform | None = None,
    markdown_content: str = "",
    position_label: str = "",
    topic_label: str = "",
    hashtags: Sequence[str] | None = None,
    quote_roles: Sequence[str] = (),
    origin_line: str = "",
    reply_count: int | None = None,
    polls: Sequence | None = None,
) -> str:
    """同 build_rich_markdown, 但直接吃字段 (缓存路径没有 ParseResult 对象)。

    ``platform`` **不是可选项**: 渲染里有多处依赖它（标签页链接等）。传 None 时
    调用方应自行用 ``ParseHub().get_platform(raw_url)`` 兜底 —— 否则会静默降级
    （标签变纯文本），而两条路径（现场/缓存）的产物还不一致。

    ``markdown_content`` / ``position_label`` / ``hashtags`` 同理 —— **凡是渲染要用的
    解析字段，这条路上都得有对应参数**。漏一个的症状是"第一次发对、第二次（命中缓存）不对"。
    最典型的是 ``markdown_content``：RichText 平台的 ``content`` 是 markdown 转出来的
    **纯文本**，不给源就等于让富文本渲染吃纯文本（引用块、链接全塌）。

    ``media_urls`` 同理：缓存里存了原始 URL 才能把正文外链图换成上传媒体引用；
    漏传（老缓存）时那些图会被剥掉（图仍在末尾，只是不在原位）。
    """
    return build_rich_markdown(
        _RichFields(  # type: ignore[arg-type]
            title,
            content,
            raw_url,
            author_name,
            author_handle,
            author_url,
            published_at,
            view_count,
            like_count,
            tags,
            platform,
            markdown_content,
            position_label,
            topic_label,
            hashtags,
            quote_roles,
            origin_line,
            reply_count,
            polls,
        ),
        config=config,
        lang=lang,
        view_label=view_label,
        custom_content=custom_content,
        media_placeholders=media_placeholders,
        quote_media_placeholders=quote_media_placeholders,
        reply_media_placeholders=reply_media_placeholders,
        media_urls=media_urls,
        hide_content=hide_content,
    )


class _RichFields:
    """最小 duck-type: 让 build_rich_markdown 能吃缓存里的字段。"""

    def __init__(
        self,
        title,
        content,
        raw_url,
        author_name,
        author_handle,
        author_url,
        published_at,
        view_count,
        like_count=None,
        tags=None,
        platform=None,
        markdown_content="",
        position_label="",
        topic_label="",
        hashtags=None,
        quote_roles=(),
        origin_line="",
        reply_count=None,
        polls=None,
    ):
        self.title = title or ""
        self.content = content or ""
        self.raw_url = raw_url
        self.author_name = author_name or ""
        self.author_handle = author_handle or ""
        self.author_url = author_url or ""
        self.published_at = published_at
        self.view_count = view_count
        self.like_count = like_count
        self.tags = list(tags or [])
        # ⚠️ platform 决定**标签能不能变成链接**（`link_hashtags` 靠它取标签页 URL）。
        # 写死 None 时缓存路径的标签会静默退回纯文本 —— 症状是
        # 「第一次发（现场解析）标签可点，命中缓存那条不可点」（用户报过）。
        self.platform = platform
        # 富文本正文的**源**：RichText 平台的 content 是 markdown 转出来的纯文本，
        # 有 markdown_content 时渲染层优先用它（见 rich_content）。
        self.markdown_content = markdown_content or ""
        # 位置标记（楼层号）与标签实体 —— 同属"渲染要用的解析字段"，
        # 写死空值就是缓存命中时那两处格式消失。
        self.position_label = position_label or ""
        # 话题片段（threads 的话题）—— 同属"渲染要用的解析字段"，写死空值
        # 就是缓存命中时话题消失。
        self.topic_label = topic_label or ""
        self.hashtags = list(hashtags or [])
        # 引用块角色（按出现顺序）—— 同属"渲染要用的解析字段"：漏了它缓存命中时
        # 渲染层只能退回按位置猜引用块的角色。
        self.quote_roles = list(quote_roles or [])
        # 归属行 —— 同属"渲染要用的解析字段"
        self.origin_line = origin_line or ""
        # 回复数（页脚那一段）
        self.reply_count = reply_count
        # 投票数据（``{{poll:N}}`` 占位符渲染用）—— 元素可以是 lib 的 ``Poll`` 对象
        # 或缓存里的 dict（``_render_poll`` 两种都吃）。老缓存没有这个字段 ⇒ 空 ⇒
        # 不替换占位符（新链接重解析才会带 polls）。
        self.polls = list(polls or [])


def rich_content(parse_result: AnyParseResult) -> str:
    """富文本里要用的正文: **有 markdown 源就用它**, 否则用 content。

    ``markdown_content`` 只在 RichText 结果类上存在（其它平台 ``getattr`` 得到空串），
    所以"非空"本身就等于"这是长文且有源"—— 不需要再 ``isinstance`` 判断。

    ⚠️ **以前这里判的是 ``isinstance(parse_result, RichTextParseResult)```，于是缓存路径
    （喂的是 duck-type ``_RichFields``）永远走 ``content`` 分支 —— 而 RichText 的 ``content``
    是 markdown 转出来的**纯文本**，症状就是"第一次发有引用块、第二次（命中缓存）塌成裸文字"。
    判据落在**字段**上（有没有 markdown 源），而不是对象的类型，两条路径才都能走对。
    """
    markdown_content = getattr(parse_result, "markdown_content", "")
    if markdown_content:
        return markdown_content
    return parse_result.content or ""


# 行首的 "#标签" (# 后紧跟非空白, 标签里不含空白与常见标点)
#: 标签的字符边界: 空白与常见句读 **终止** 标签 (``#tag.`` 的句号不属于标签)。
#: 注意 ``-`` **不**在终止集合里 —— ``#foo-bar`` 整串才是一个标签, 而服务端的自动识别
#: 会在连字符处截断 (只染蓝 ``#foo``), 看着像标签被切了。
#: 标签体的字符边界。**必须排除 ``<`` ``>`` ``/``** —— 否则会把紧跟在标签后面的
#: HTML 标签一起吞进去（实测症状: 引用块署名行的 ``#1</i>`` 把 ``</i>`` 吃成链接文字，
#: 闭合标签丢失后，后面的 ``<i>`` 就裸露在消息里）。
_TAG_BODY = r"[^\s#，。！？、,.!?）)】\]<>/]+"
#: 任意位置的 ``#标签`` (前面不是字母/数字/``&``/``/`` —— 排除 ``a#b`` 与 URL 里的片段)
_HASH_TAG_RE = re.compile(rf"(?<![\w&/])#({_TAG_BODY})")
#: 已经是链接的整段 (``<a …>…</a>``): 处理标签时跳过, 免得把标签包第二层
_ANCHOR_SEGMENT_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.S)


def _link_known_tags(text: str, platform: Platform | None, hashtags: Sequence[str]) -> str:
    """按**平台实体**给的标签名精确链接化（优先于正则）。

    为什么需要它（2026-10-06，用户报）: 一条推文的正文是

        TVアニメ「#FX戦士くるみちゃん」第一話より

    正则把标签吃成了 ``#FX戦士くるみちゃん」第一話より`` —— 终止字符集没枚举 ``」``。
    而 API 的 ``entities.hashtags[].text`` 就是 ``FX戦士くるみちゃん``（与网页上 hashtag
    链接的最后一段逐字一致），服务端自己算的边界，不受标点影响。

    做法: 在**非锚点**的片段里，把 ``#<名字>`` 精确替换成链接。名字用 ``re.escape`` ——
    实体里可能含正则元字符。**匹配不到就不动**（正文里的标签被平台改写过的情况），
    交给兜底正则处理 —— 绝不为了"用上实体"而改写正文。
    """

    def link(name: str, source: str) -> str:
        url = tag_page_url(platform, name)
        if not url:
            return source
        return source.replace(f"#{name}", f'<a href="{html.escape(url, quote=True)}">#{html.escape(name)}</a>')

    out: list[str] = []
    pos = 0
    # 锚点内的标签已经不裸 —— 跳过整段 ``<a>…</a>``（与正则那条路同一纪律）
    for anchor in _ANCHOR_SEGMENT_RE.finditer(text):
        out.append(_link_tags_in_segment(text[pos : anchor.start()], hashtags, link))
        out.append(anchor.group(0))
        pos = anchor.end()
    out.append(_link_tags_in_segment(text[pos:], hashtags, link))
    return "".join(out)


def _link_tags_in_segment(segment: str, hashtags: Sequence[str], link: Callable[[str, str], str]) -> str:
    """在**不含锚点**的片段里逐个标签做精确替换。

    两件必须做对的事:

    1. **长名字优先**: ``#foo`` 与 ``#foobar`` 都在实体里时，先换长的 ——
       否则 ``#foobar`` 会先被 ``#foo`` 换出个残缺的 ``<a>#foo</a>bar``。
    2. **每轮都跳过已生成的锚点**: 长名字换完后，短名字那一轮会在**刚生成的锚点内部**
       再包一层（``<a …>`` 里嵌 ``<a>#foo</a>bar``，标签全乱）。所以逐轮重扫锚点，
       只在锚点之外的片段里替换。
    """
    for name in sorted((n for n in hashtags if n), key=len, reverse=True):
        # 前面不能是 ``\w`` / ``&`` / ``/`` —— 排除 ``a#b`` 与 URL 片段（与正则同判据）
        pattern = re.compile(rf"(?<![\w&/])#{re.escape(name)}")
        pieces: list[str] = []
        pos = 0
        for anchor in _ANCHOR_SEGMENT_RE.finditer(segment):
            pieces.append(pattern.sub(lambda m, n=name: link(n, m.group(0)), segment[pos : anchor.start()]))
            pieces.append(anchor.group(0))
            pos = anchor.end()
        pieces.append(pattern.sub(lambda m, n=name: link(n, m.group(0)), segment[pos:]))
        segment = "".join(pieces)
    return segment


def link_hashtags(
    text: str, platform: Platform | None = None, hashtags: Sequence[str] | None = None
) -> str:
    r"""把正文里的 **所有** 裸 ``#标签`` 渲染成标签页链接（不限于行首）。

    :param hashtags: **平台实体**给的标签名（不含 ``#``，如 twitter 的
        ``entities.hashtags[].text``）。给了就先按名字精确链接化，**正则只兜底**剩下的
        —— 正则在日文 ``」``、全角标点这些地方会把边界猜错（见 ``_link_known_tags``）。
        没给（其它平台 / 拿不到实体）时行为与本参数引入前**逐字相同**。

    为什么要自己链接而不是交给服务端:
      - 服务端的 hashtag 自动识别**有字符限制**: ``#foo-bar`` / ``#foo.bar`` 会在 ``-``/``.``
        处**截断**（只有前半截可点），**纯数字**标签（``#123``）干脆不识别 ——
        于是同一条消息里"有的标签是链接、有的不是"（用户报障就是这个）。
      - 行首 ``#xxx`` 还会被富文本 markdown 当成**一级标题**（字号巨大）。
    自己写成 ``<a href="标签页">`` 两个问题一起解决，且形态与**标签行**（``format_tags``）一致。

    平台**没有**标签页模板时退回纯文本；行首那种情况继续用 ``\#`` 转义（防标题）。
    带空格的 ``# 标题`` 是真标题，本来就不匹配（``#`` 后面是空格）。
    """

    def repl(match: re.Match[str]) -> str:
        tag = match.group(1)
        # **纯数字不是标签** —— 那是楼层号/序号（如引用块署名行的 ``作者 · #1``）。
        # 不排除的话 ``#1`` 会被链接到标签页（linux.do 上还会变成不存在的 tag 链接）。
        if tag.isdigit():
            return match.group(0)
        url = tag_page_url(platform, tag)
        if url:
            label = html.escape(tag)
            return f'<a href="{html.escape(url, quote=True)}">#{label}</a>'
        # 没有标签页可指向: 行首的 ``#`` 得转义 (否则被当一级标题), 行中的原样
        at_line_start = match.start() == 0 or text[match.start() - 1] == "\n"
        escaped = html.escape(tag)
        return f"\\#{escaped}" if at_line_start else f"#{escaped}"

    # ① 先按**平台实体**精确链接化（服务端算好的边界，胜过正则猜）
    if hashtags:
        text = _link_known_tags(text, platform, hashtags)

    # ② 剩下的裸标签用正则兜底（实体没覆盖到的，或平台没给实体）
    # 锚点内的标签已经不裸: 跳过整段 ``<a>…</a>``, 免得包第二层
    out: list[str] = []
    pos = 0
    for anchor in _ANCHOR_SEGMENT_RE.finditer(text):
        out.append(_HASH_TAG_RE.sub(repl, text[pos : anchor.start()]))
        out.append(anchor.group(0))
        pos = anchor.end()
    out.append(_HASH_TAG_RE.sub(repl, text[pos:]))
    return "".join(out)


def link_leading_hashtags(text: str, platform: Platform | None = None) -> str:
    """旧名保留（行为已扩到全文，见 ``link_hashtags``）。"""
    return link_hashtags(text, platform)


# 整行只有 - 或 = 的行: markdown 会把它当成 setext 标题下划线, 把上面整段变成标题(大字)
_SETEXT_UNDERLINE_RE = re.compile(r"^([ \t]*)([-=]+)([ \t]*)$")


def escape_setext_underlines(text: str) -> str:
    r"""转义「**紧跟在一行文字下面**的 ``-`` / ``=``」行, 防止它把上一行变成大标题。

    markdown 的 setext 语法里「文本行 + 下一行是若干 ``=`` 或 ``-``」= 一级/二级标题,
    而 ``--`` 两个减号就够触发。实测 threads 的节目表末尾有一行 ``--``, 结果**整个正文段**
    被服务端解析成 ``section heading``(大字)。转义首个字符即可, 渲染出来仍是原来的 ``--``。

    ⚠️ **判据必须带上"上一行有文字"** —— setext 不会跨空行。以前是无条件转义整行的分隔符,
    于是 discourse 那种**独立成行的 ``---`` (分隔线)** 也被转义成 ``\---``, 服务端不再认它,
    渲染成字面的 ``---`` 段落 (实测一篇 linux.do 长帖里 18 处)。独立行既不会触发 setext,
    也不该被转义 —— 它就该是分隔线。
    """
    lines = text.split("\n")
    out: list[str] = []
    for index, line in enumerate(lines):
        # 只有上一行是**有内容的文字行**时才危险 (空行/开头都安全)
        after_text = index > 0 and bool(lines[index - 1].strip())
        if after_text:
            line = _SETEXT_UNDERLINE_RE.sub(r"\1\\\2\3", line)
        out.append(line)
    return "\n".join(out)


#: 表格行（``| a | b |``，可带引用前缀 ``> ``）—— 它们之间不需要 markdown 硬换行
#: （表格语义自身保留结构），而"行尾两空格"有让服务端把行拼起来、表格解析失败的风险。
_TABLE_ROW_RE = re.compile(r"^>?\s*\|.*\|$")


def preserve_linebreaks(text: str) -> str:
    """把单换行变成 markdown 硬换行 (行尾两个空格)。

    富文本 markdown 解析时**单个换行会被吞成空格** —— threads 这种一行一条的
    排版会挤成一整段。行尾补两个空格即保留换行, 又不像空行那样拉开段间距。

    **表格行跳过** —— 表格的结构由行写法自身承载, 补了硬换行反而可能让服务端
    把行拼起来。
    """
    lines = text.split("\n")
    out: list[str] = []
    for index, line in enumerate(lines):
        followed_by_text = index + 1 < len(lines) and bool(lines[index + 1].strip())
        is_table_row = bool(_TABLE_ROW_RE.match(line.strip()))
        out.append(line.rstrip() + "  " if line.strip() and followed_by_text and not is_table_row else line)
    return "\n".join(out)


def split_quote_blocks(content: str) -> tuple[str, str, str]:
    """把正文拆成 (开头的引用块, 中间正文, 末尾的引用块)。

    解析器把**被回复**的推文渲染在正文最前、**被引用**的推文渲染在正文最后
    (与 X 上的卡片位置一致)。主推自己的媒体要夹在两个卡片之间, 所以两边都要摘出来。

    只认连续的行首 ``>``, 不跨普通空行。开头那个块必须紧贴正文开头 ——
    正文里自己写的引用 (中间位置) 不会被误当卡片。
    """
    lines = content.split("\n")

    # 末尾块
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    tail_start = end
    while tail_start > 0 and lines[tail_start - 1].lstrip().startswith(">"):
        tail_start -= 1

    # 开头块 (在末尾块之前)
    head_end = 0
    while head_end < tail_start and not lines[head_end].strip():
        head_end += 1
    head_scan = head_end
    while head_scan < tail_start and lines[head_scan].lstrip().startswith(">"):
        head_scan += 1
    # 只有确实是一整块引用 (后面跟空行或正文) 才算, 且不能把整个正文都吃进来
    head = "\n".join(lines[head_end:head_scan]).strip() if head_scan > head_end else ""

    middle = "\n".join(lines[head_scan:tail_start]).strip() if head else "\n".join(lines[:tail_start]).strip()
    tail = "\n".join(lines[tail_start:end]).strip()
    return head, middle, tail


def split_trailing_quote(content: str) -> tuple[str, str]:
    """兼容旧调用点: 返回 (去掉末尾引用块后的正文, 末尾引用块)。"""
    head, middle, tail = split_quote_blocks(content)
    body = f"{head}\n\n{middle}".strip() if head else middle
    return body, tail


def attach_quote_media(quote: str, placeholders: Sequence[str]) -> str:
    """把被引用内容的媒体接进引用块内部。

    富文本里引用块的成员是"连续以 ``>`` 开头的行", 所以占位符也必须带 ``> ``,
    否则媒体会掉到引用块外面 (实测: 不带前缀就变成独立的图片块)。
    多张时包成 ``<tg-collage>``, 与正文媒体一致 —— 引用块里嵌图集同样成立。
    """
    if not quote or not placeholders:
        return quote
    lines: list[str] = []
    for block in wrap_collage(placeholders):
        lines.extend(f"> {line}" if line.strip() else ">" for line in block.split("\n"))
    return "\n".join([quote, *lines])


def _quote_body(quote: str) -> str:
    """引用块去掉每行行首的 '> ' 后的正文。"""
    lines = quote.rstrip("\n").split("\n")
    return "\n".join(line[1:].lstrip() if line.startswith(">") else line for line in lines)


#: 引用块的**署名行**（``format_quote_block`` 在开头加的作者行）:
#: 整行是 ``<i>…</i>`` 且行内含链接（引用块整块斜体，这一行也是）。
#: 正文行不会长这样（正文是 ``<i>文字</i>``，除非它本身就是个纯链接 ——
#: 那种情况把它提到外面也无害）。
_QUOTE_AUTHOR_RE = re.compile(r"^<i>.*?<a\s+href=.*?</i>$", re.S)


def _split_quote_author(body: str) -> tuple[str, str]:
    """把引用正文拆成 (署名行, 其余正文)。没有署名行时返回 ("", body)。

    为什么要拆: 署名行（作者名 + ``@handle``）往往就有 80+ 字符，会**把预览配额吃光**
    —— ``split_fold_preview`` 按字符数取前几行，署名行一个人就顶到上限，正文一行都露不出来
    （用户反馈「引用里的作者和正文也分割」）。拆开后署名行单独一段（不参与折叠），
    正文自己按同一套阈值留预览。
    """
    lines = body.split("\n")
    if len(lines) > 1 and _QUOTE_AUTHOR_RE.match(lines[0].strip()):
        return lines[0].strip(), "\n".join(lines[1:]).strip()
    return "", body


def quote_will_fold(quote: str) -> bool:
    """引用块会不会被折叠 (折叠形态见 ``render_folded_quote_card``)。

    只按**正文**判断: 署名行是固定开销 (名字 + ``@handle`` 常有 80+ 字符), 算进去的话
    内容很短的引用也会被折起来 —— 那是"作者行吃了配额"的另一种表现。
    """
    if not quote:
        return False
    _, text = _split_quote_author(_quote_body(quote))
    return _should_fold(text)


def quote_with_divider(quote: str) -> str:
    """在引用块的署名行与正文之间插一条分割线（与主帖的作者分割线同一形态）。

    块内写法是独立一行的 ``> ---``（服务端认它, 实测产出 ``RichBlockDivider``）。
    没有署名行、或署名行后面没内容时原样返回 —— 不给光秃秃的引用加线。
    """
    if not quote:
        return quote
    author, text = _split_quote_author(_quote_body(quote))
    if not author or not text:
        return quote
    lines = [f"> {author}", ">", "> ---", ">"]
    lines.extend(f"> {line}" if line.strip() else ">" for line in text.split("\n"))
    return "\n".join(lines)


def fold_quote_block(quote: str, *, summary: str = "") -> str:
    """把过长的引用块整体折成 ``<blockquote expandable>``（客户端自显前几行）。

    ⚠️ 引用卡片**不走这条路**（``render_quote_card`` 统一用容器 + 按钮）。
    这里留着是因为它是个独立的、可复用的折叠形态（纯文字引用块用得上），
    与卡片形态无关 —— 别把「按钮太多」这类问题归到它头上：
    那次的真因是 linux.do 正文被切出多个引用块、**每块各折一次**
    （已改成整篇只折一次，见 ``format_text``）。
    """
    if not quote:
        return quote
    body = _quote_body(quote)
    if not _should_fold(body):
        return quote
    return render_expandable_quote(body)


#: **卡片容器**的特征: ``<blockquote>`` 里紧跟一个 ``<details>``。
#: 不能只用 ``"<blockquote>" in markdown`` —— 正文里本来就有引用块
#: (linux.do 那种帖子一篇能有十几个)，那样判据会**误命中**，把普通帖子也推去走 blocks。
#: 正文引用块**内部不会有** details（正文的引用块不折叠），整篇折叠的 details 又在块**外**,
#: 所以「块内出现 details」是卡片容器独有的特征。
_QUOTE_CARD_RE = re.compile(r"<blockquote>\s*\n\s*\n(?:(?!</blockquote>).)*?<details>", re.S)


def markdown_needs_blocks(markdown: str) -> bool:
    """这段 markdown 里有没有**只有 blocks 才表达得了**的结构。

    就是**折叠的引用卡片**（容器内 [预览 + ``<details>`` + 可选图]）——
    markdown 的 ``>`` 引用块嵌不了 ``<details>``，所以发送方必须切 blocks 路径。
    判据直接看**渲染产物**（与渲染保持单一来源），且只认卡片容器这一种特征
    （见 ``_QUOTE_CARD_RE``：普通的正文引用块不误命中）。
    """
    return bool(_QUOTE_CARD_RE.search(markdown))


def render_folded_quote_card(quote: str, media: Sequence[str] = (), *, summary: str = "") -> str:
    """会折叠的引用卡片：容器内 = **预览前几行** + ``<details>`` 折剩余 (+ 图)。

    为什么不用 ``<blockquote expandable>``：那个块**只吃 RichText**，而 RichText 里
    **没有任何图片类型**（Bot API 文档的成员表可查），块内 ``![]()`` 也不解析 ——
    所以旧做法只能把图挪到块**外**（用户反馈「图片是引用里面的，放外面了」）。
    改用 ``blockquote`` 容器（成员是输入块列表）+ ``details`` 折文字，图就留在引用块内。

    预览沿用正文那套折叠逻辑（``split_fold_preview``）：开头几行留在外面，
    否则收起时只剩一个「展开全文」按钮，看不到一点内容。
    """
    if not quote:
        return quote
    body = _quote_body(quote)
    # 署名行单独一段、不参与折叠: 否则它一个就把预览配额吃光, 正文一行都露不出来
    author_line, text = _split_quote_author(body)
    preview, rest = split_fold_preview(text)
    parts: list[str] = []
    if author_line:
        parts.append(author_line)
        if text:
            # 与主帖同一条规矩: 作者行与内容之间隔一道分割线 (用户要"统一")
            parts.append("---")
    if preview:
        parts.append(preview)
    if rest:
        folded_summary = summary or _DEFAULT_FOLD_SUMMARY
        parts.append(f"<details><summary>{folded_summary}</summary>\n\n{rest}\n\n</details>")
    parts.extend(wrap_collage(media))
    return "<blockquote>\n\n" + "\n\n".join(parts) + "\n\n</blockquote>"


def render_quote_card(quote: str, media: Sequence[str] = (), *, summary: str = "") -> list[str]:
    """渲染一张引用卡片（含它自己的媒体），返回若干段。

    **媒体的位置取决于折没折叠**（真机实测）：
    - 折叠块（``<blockquote expandable>``）内 ``![]()`` 图片语法**不解析**，
      会原样显示成 ``![]()`` 加一个链接（图片格式坏掉）；块内改用 ``<img>``
      虽然能出图，却会把块**退化成不可折叠**的普通引用块。
    - 普通 ``<blockquote>`` 内 ``![]()`` **正常出图**。

    ⇒ 会折叠 + 带媒体时改用容器写法（图留在块内，见 ``render_folded_quote_card``）；
    不折叠时把媒体接进块**内**（语义上属于引用内容）；
    不带媒体时只用折叠块（块里没图，没有上面的问题）。
    """
    if not quote:
        return []
    if quote_will_fold(quote):
        # 统一: 长引用一律「容器 + 按钮」, 不论有没有媒体 —— 两种形态的观感不一致
        # 会让人以为坏了 (用户要求全部统一)。容器内留前几行当预览, 不用 expandable
        # 那种"整块一起折"的老形态。
        return [render_folded_quote_card(quote, media, summary=summary)]
    marked = quote_with_divider(quote)
    if media:
        return [attach_quote_media(marked, media)]
    return [marked]


#: 图集超过这个张数就把**多出来的**折进按钮 (用户: 「图片超过4张的也按钮折叠一下」)。
#: 前 ``_COLLAGE_FOLD_THRESHOLD`` 张留在外面当预览 —— 与正文长文折叠同一套观感
#: (收起时不能只剩一个按钮, 那样看不到一点内容)。
_COLLAGE_FOLD_THRESHOLD = 4


def _collage(placeholders: Sequence[str]) -> str:
    """把若干媒体占位符包成一个图集块。

    富文本里多个独立的图片块会渲染成各自分散的图; 包进 ``<tg-collage>`` 才是图集。
    """
    return "<tg-collage>\n\n" + "\n".join(placeholders) + "\n\n</tg-collage>"


def wrap_collage(placeholders: Sequence[str], *, fold_summary: str = "") -> list[str]:
    """多张媒体包成一个图集块; 超过阈值时把多出来的折进 ``<details>`` 按钮。

    单张直接返回原样。超过 :data:`_COLLAGE_FOLD_THRESHOLD` 张且给了 ``fold_summary``
    时切成「前几张图集 + ``<details>`` 折其余」—— 一次铺十几张图会占满整屏。

    ``fold_summary`` 为空则永不折叠: 调用方在**自己已经处于折叠块内**时应当留空
    (例如引用卡片、手动打码 —— 客户端对嵌套折叠没有保证)。
    """
    if len(placeholders) <= 1:
        return list(placeholders)
    if not fold_summary or len(placeholders) <= _COLLAGE_FOLD_THRESHOLD:
        return [_collage(placeholders)]
    head = placeholders[:_COLLAGE_FOLD_THRESHOLD]
    tail = placeholders[_COLLAGE_FOLD_THRESHOLD:]
    return [
        _collage(head),
        f"<details><summary>{fold_summary}</summary>\n\n{_collage(tail)}\n\n</details>",
    ]


# 各平台的标签页/标签搜索页. 没有列出的平台退回纯文本 #标签
TAG_PAGE_URLS: dict[Platform, str] = {
    Platform.PIXIV: "https://www.pixiv.net/tags/{tag}",
    Platform.TWITTER: "https://x.com/hashtag/{tag}",
    Platform.WEIBO: "https://s.weibo.com/weibo?q=%23{tag}%23",
    Platform.BILIBILI: "https://search.bilibili.com/all?keyword={tag}",
    Platform.THREADS: "https://www.threads.com/search?q=%23{tag}",
    Platform.TIKTOK: "https://www.tiktok.com/tag/{tag}",
    Platform.INSTAGRAM: "https://www.instagram.com/explore/tags/{tag}/",
    Platform.YOUTUBE: "https://www.youtube.com/hashtag/{tag}",
    Platform.FACEBOOK: "https://www.facebook.com/hashtag/{tag}",
    Platform.DOUYIN: "https://www.douyin.com/search/{tag}",
    Platform.KUAISHOU: "https://www.kuaishou.com/search/video?searchKey={tag}",
    Platform.ZHIHU: "https://www.zhihu.com/search?q={tag}",
    Platform.TIEBA: "https://tieba.baidu.com/f/search/res?qw={tag}",
    Platform.DOUBAN: "https://www.douban.com/search?q={tag}",
    Platform.XHS: "https://www.xiaohongshu.com/search_result?keyword={tag}",
    # Discourse 论坛: /tag/<名字> 会重定向到规范地址 /tag/<slug>/<id>, 浏览器自动跟随
    Platform.LINUXDO: "https://linux.do/tag/{tag}",
    # bgm 的日志标签是**用户级**的（``/user/<uid>/blog/tag/<名>``）——
    # 全站 ``/blog/tag/<名>`` 实测返回 0 字节空响应。所以模板需要 ``{id}``。
    Platform.BANGUMI: "https://bgm.tv/user/{id}/blog/tag/{tag}",
}


def tag_page_url(platform: Platform | None, tag: str, *, user_id: str = "") -> str:
    """平台上的标签页地址; 没有对应模板时返回空串。

    有的平台标签页是**用户级**的（bgm 的日志标签挂在作者名下），模板里带 ``{id}``；
    这时拿不到 ``user_id`` 就返回空串 —— 宁可退回纯文本，也不生成一个打不开的链接。
    """
    template = TAG_PAGE_URLS.get(platform) if platform else None
    if not template:
        return ""
    if "{id}" in template and not str(user_id or "").strip():
        return ""
    return template.format(tag=quote(str(tag), safe=""), id=quote(str(user_id or ""), safe=""))


def _tag_label(tag: str, *, linked: bool) -> str:
    """标签的显示文本。

    ``#`` 需要转义是因为**行首**的 ``#xxx`` 会被当标题。带链接时行首是 ``<a``,
    本来就不是标题, 所以不加转义 —— 加了虽然渲染正常, 但复制消息时会露出反斜杠。
    退回纯文本时行首就是 ``#``, 必须转义。
    """
    escaped = html.escape(str(tag))
    return f"#{escaped}" if linked else f"\\#{escaped}"


def _render_tag(platform: Platform | None, tag: str, *, user_id: str = "") -> str:
    """单个标签: 能拿到标签页就渲染成链接, 否则退回纯文本。

    ``user_id`` 给**用户级标签页**的平台用（bgm 的日志标签挂在作者名下）。
    """
    url = tag_page_url(platform, tag, user_id=user_id)
    if not url:
        return _tag_label(tag, linked=False)
    return f'<a href="{html.escape(url, quote=True)}">{_tag_label(tag, linked=True)}</a>'


def format_tags(parse_result: AnyParseResult) -> str:
    """把作品标签渲染成一行, 尽量渲染成指向标签页的链接。

    两个坑都靠链接绕开:
    1. 行首的 ``#标签`` 在富文本 markdown 里是一级标题语法, 字号会大得离谱;
    2. Telegram 解析 hashtag 时遇到 ``・`` 之类的非字母字符就停, 长标签
       (如 ``アリサ・ミハイロヴナ・九条``) 只会染蓝到 ``・`` 之前, 看着像被截断。
    链接形式两者都没有, 还多了点击进标签页的能力。

    标签多时一行会很长 (pixiv 一条作品常有 8+ 个): 按显示宽度预算截断,
    超出部分省略成 ``…``, 保证整行落在一行到两行之间。
    """
    tags = getattr(parse_result, "tags", None) or []
    if not tags:
        return ""
    platform = getattr(parse_result, "platform", None) or ParseHub().get_platform(
        getattr(parse_result, "raw_url", "") or ""
    )

    # 用户级标签页的平台要用作者标识拼地址（bgm）；其它平台模板里没有 ``{id}``，传了也不影响
    user_id = str(getattr(parse_result, "author_handle", "") or "")

    rendered: list[str] = []
    width = 0
    for tag in tags:
        # +1 是标签之间的空格
        piece = _display_width(f"#{tag}") + 1
        if rendered and width + piece > TAG_LINE_DISPLAY_BUDGET:
            rendered.append("…")
            break
        rendered.append(_render_tag(platform, tag, user_id=user_id))
        width += piece
    return " ".join(rendered)


def _display_width(text: str) -> int:
    """按观感算显示宽度: 全角 (CJK 等) 记 2, 其余记 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def get_parse_author_name(parse_result: AnyParseResult) -> str:
    """读取解析器提供的文章作者，兼容未来平台扩展。"""
    for field in ("author_name", "author", "author_info", "user"):
        value = getattr(parse_result, field, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is None:
            continue
        for name_field in ("name", "full_name", "display_name", "username"):
            name = getattr(value, name_field, None)
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


def format_author_line(parse_result: AnyParseResult) -> str:
    """作者行 (markdown): ``**<a>名字</a> <code>@handle</code>**`` (常规小字, 非等宽)。

    拼装与链接一律交给库里的 ``format_author_link`` —— 这里不要再手写一遍替换,
    否则"作者长什么样"这件事就有了两份实现 (曾经就是如此)。

    **不加冒号** (用户要求「取消冒号」) —— 名字已经可点、@handle 已是小字标识,
    再加冒号只是多余的标点。

    平台给了**位置标记** (如 linux.do 的楼层号) 时接在末尾: ``… @某人 · #4``。
    这与引用块里其它层的形态一致 (`` · #1``) —— 用户报「主楼标楼层号了但是回复没标」:
    引用块里的主楼早就标了, 而本层（也就是这条回复）反而没标。
    """
    label = format_author_link(
        get_parse_author_name(parse_result),
        str(getattr(parse_result, "author_handle", "") or ""),
        str(getattr(parse_result, "author_url", "") or ""),
    )
    if not label:
        return ""
    # **粗体只包名字, 不包 @handle**: 整行包 `**` 时角标里的 handle 会**继承粗体**
    # (服务端块实测是 textSubscript(textBold(textPlain))) —— 用户要的"常规样式"是
    # 名字粗体 + handle 常规小字。所以把 `**` 收在链接结束标签处。
    # 位置标记在**粗体外面** (与 @handle 一样不进粗体): 粗体标的是"谁写的",
    # 位置是附带信息。没有这个概念的平台为空串 → 输出与改动前逐字一致。
    position = str(getattr(parse_result, "position_label", "") or "")
    suffix = f" · {position}" if position else ""
    # **话题**（threads）接在最后，用 `` > `` 分隔 —— 跟着平台自己的显示走
    # （用户给的形态：``雪楓Yuki @snowmaple_official > 申請審判``）。话题片段已带链接，
    # 不进粗体（粗体标的是"谁写的"）。没有话题的平台为空串 → 输出逐字不变。
    topic = str(getattr(parse_result, "topic_label", "") or "")
    if topic:
        suffix = f"{suffix} > {topic}"
    if "</a>" in label:
        head, _, tail = label.partition("</a>")
        return f"**{head}</a>**{tail}{suffix}"
    return f"**{label}**{suffix}"


_QUOTE_BLOCK_RE = re.compile(r"(?m)^>[^\n]*(?:\n>[^\n]*)*")

# 折叠阈值: 超过 500 字 (或行数超限) 就折起来。
# **正文与引用块共用这一套** —— 只写一份, 两处共用 (曾分别在两处硬编码, 改一处必漏另一处)。
# 200 对拉丁文字太紧: 英文 200 字符 ≈ 30 个词, 一个长句就顶到线 (实测一条三条短句的
# 英文推文 289 字符就被折了), 而中文 200 字是实打实的两三大段。放宽到 500 后
# 中英的信息量大致对齐。
_FOLD_CHAR_THRESHOLD = 500
_FOLD_LINE_THRESHOLD = 10


def _should_fold(text: str) -> bool:
    """文本是否超过折叠阈值 (字符数或内容行数任一超出)。

    **行数只数非空行**: 空行是段落排版, 不是内容量。若连空行一起数,
    「五句话 + 中间四个空行」= 9 行, 总共才十几个字也会被折叠 —— 行数阈值
    就成了比字符阈值更早触发的误判来源。
    """
    content_lines = sum(1 for line in text.splitlines() if line.strip())
    return len(text) > _FOLD_CHAR_THRESHOLD or content_lines > _FOLD_LINE_THRESHOLD

# pyrogram 的 Markdown 解析器 (client 默认 ParseMode.DEFAULT) 把成对的
# __ ** -- ~~ || ` 当格式定界符, 定界符字符本身会被吃掉 —— 引用块里的推文 handle
# (@__yuuuumr__ 显示成 @yuuuumr 并变成斜体) 就是这么被误伤的。
# 换成 HTML 数字实体: markdown 阶段不再匹配, 后续 html.parse 阶段还原成原字符,
# 最终显示完全一致。
_MD_DELIM_ENTITY = {
    ord("_"): "&#95;",
    ord("*"): "&#42;",
    ord("~"): "&#126;",
    ord("`"): "&#96;",
    ord("|"): "&#124;",
}


def neutralize_markdown(text: str) -> str:
    """中和 Markdown 格式定界符, 防止正文本被 pyrogram 的 markdown 解析误伤。"""
    return text.translate(_MD_DELIM_ENTITY)


_URL_RE = re.compile(r"https?://[^\s<>]+")


def neutralize_markdown_urls(text: str) -> str:
    """只中和 URL 里的 markdown 定界符。

    URL 里出现 '__' 时 (例如 https://x.com/__yuuuumr__/status/...), pyrogram 的
    markdown 解析器会把它当斜体定界符并插入 <i>/</i>, 于是
    <a href='https://x.com/__yuuuumr__/...'> 的 href 被写成
    'https://x.com/<i>yuuuumr</i>/...' —— 链接直接作废、变成不可点的纯文本。
    只处理 URL 片段, 避免影响正文里有意写的 markdown。
    """
    return _URL_RE.sub(lambda m: neutralize_markdown(m.group(0)), text)


def _strip_quote_markers(block: str) -> str:
    """去掉引用块每行行首的 '>', 并中和块内文本自己的 markdown 定界符。

    中和是为了防止引用内容里的 ``*`` ``_`` 被 markdown 阶段误当定界符吃掉;
    斜体标记本身由 ``parsehub`` 的 ``format_quote_block`` 直接产出 ``<i>``,
    这里没有"把 ``*`` 转成 ``<i>``"这一步。
    """
    lines = block.rstrip("\n").split("\n")
    body = "\n".join(line[1:].lstrip() if line.startswith(">") else line for line in lines)
    return neutralize_markdown(body)


def _strip_quote_prefix(match: re.Match) -> str:
    return _strip_quote_markers(match.group(0))


#: 折叠块的默认摘要文案 (源码语言), 调用方按 locale 传译文进来
_DEFAULT_FOLD_SUMMARY = "展开全文"

#: 折叠前**留在外面**的预览: 折叠态只显示 summary, 不留预览的话正文一个字都看不到
#: (用户原话「这个折叠看不到一点内容啊」「怎么还是没有前几行啊」)。
#: 行数与字符数任一触顶即停; 行数只数**非空行**(空行是排版, 不是内容量)。
_FOLD_PREVIEW_LINES = 7
_FOLD_PREVIEW_CHARS = 300


def split_fold_preview(content: str) -> tuple[str, str]:
    """把内容拆成 (外面可见的预览, 折进 details 的剩余部分)。

    预览取开头几行 (行数/字符数任一超限即停); 内容全挤在一行时按字符切,
    否则整条正文只有一行就永远折不起来。剩余为空时返回 (content, "") = 不折。
    """
    lines = content.split("\n")
    end = 0
    chars = 0
    counted = 0
    for line in lines:
        # **只数非空行** (与 _should_fold 同一原则): 空行是段落排版, 不是内容量。
        # 若把空行也算一行, 首行文字后面跟个空行就把配额用完了 ——
        # 折叠态只看到一行, 与「前几行预览」的说法不符。
        if counted >= _FOLD_PREVIEW_LINES or (counted and chars + len(line) > _FOLD_PREVIEW_CHARS):
            break
        if line.strip():
            counted += 1
        chars += len(line)
        end += 1

    preview = "\n".join(lines[:end]).rstrip()
    rest = "\n".join(lines[end:]).strip()

    # 表格不能被拆开: 切点落在表格中间时, 把 preview 末尾那组表格行整体挪给 rest
    # —— 拆成两半后两边都不再是表格 (「引用里的投票」在折叠卡片里散架)。
    if rest and _TABLE_ROW_RE.match(rest.split("\n", 1)[0].strip()):
        prev_lines = preview.split("\n")
        moved: list[str] = []
        while prev_lines and _TABLE_ROW_RE.match(prev_lines[-1].strip()):
            moved.insert(0, prev_lines.pop())
        if moved:
            rest = "\n".join([*moved, *rest.split("\n")]).strip()
        preview = "\n".join(prev_lines).rstrip()

    # 只有一行 (或首行就吃满): 按字符切出预览, 否则没有可折的剩余
    if not rest and len(preview) > _FOLD_PREVIEW_CHARS:
        rest = preview[_FOLD_PREVIEW_CHARS:].strip()
        preview = preview[:_FOLD_PREVIEW_CHARS].rstrip()

    if not rest:
        return content, ""
    return preview, rest


def use_br_linebreaks(text: str) -> str:
    """把换行转成 ``<br>``。

    ``<blockquote expandable>`` 里**真换行会被并成空格** (实测), 只有 ``<br>``
    才保留换行; 而带真空行的内容会让它**退化成普通引用块、完全不折叠**。
    把换行写成 ``<br>`` 两个问题一起解决。
    """
    lines = [line.rstrip() for line in text.split("\n")]
    return "<br>".join(lines)


def render_expandable_quote(content: str) -> str:
    """引用块的老折叠形态: 整体一个 ``<blockquote expandable>``。

    与正文的 ``<details>`` 不同 —— ``<details>`` 会把「预览 / 按钮 / 折起部分」
    切成三段 (用户原话「引用块被按钮分割, 割裂感太强了」)。这里整块一起折,
    客户端自己显示开头几行, 形态是一体的。
    """
    return f"<blockquote expandable>{use_br_linebreaks(content)}</blockquote>"


#: 独占一行的分隔线 (markdown / discourse 的 hr)。
#: 服务端**要求前后有空行**才认它是分隔线: 紧贴上一行时会被当普通文本 (实测一篇 linux.do
#: 长帖里 18 处变成了字面 '---' 段落), 更糟的是紧跟文字行时会被当成 setext 标题语法
#: (把上一行整行变成 H2)。所以渲染前统一补空行。
_HR_LINE_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")


def _normalize_hr(text: str) -> str:
    """把独占一行的分隔线前后规范成空行, 让它稳定解析成分隔线。

    只动"整行只有分隔符"的行 —— ``**粗体**``、``***粗斜体***``、列表项里的短横线、
    表格的 ``|---|`` 都不是独占一行的纯分隔符, 不会被误伤。
    """
    lines = text.splitlines()
    if not any(_HR_LINE_RE.match(line.strip()) for line in lines):
        return text

    out: list[str] = []
    for line in lines:
        if _HR_LINE_RE.match(line.strip()):
            if out and out[-1].strip():
                out.append("")
            out.append("---")
            out.append("")
        else:
            out.append(line)
    # 补空行可能撑出连续空行: 合并回一个空行, 避免块之间出现大段空白
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _fold_whole(content: str, *, summary: str = "") -> str:
    """把内容折一次: **开头几行留外面当预览**, 其余折进 details。

    **用 <details> 而不是 <blockquote expandable>**: 后者一旦块内含空行,
    Telegram 就把它退化成普通引用块 (完全不折叠) —— 而正文天然是多段落,
    于是长正文永远折不起来; 而且引用块内的换行会被并成空格, 段落结构全丢。
    <details> 保留完整段落, 是富文本 markdown 里唯一能折叠多段落的容器。

    收起时客户端**只显示 summary**, 所以必须留一截在外面当预览 —— 否则用户看到的
    就是光秃秃一个「展开全文」(曾一度全收起, 用户报「没有前几行」)。
    预览行数与阈值见 ``split_fold_preview`` / ``_FOLD_PREVIEW_*``。
    """
    preview, rest = split_fold_preview(content)
    if not rest:
        return content
    folded = f"<details><summary>{summary or _DEFAULT_FOLD_SUMMARY}</summary>\n\n{rest}\n\n</details>"
    return f"{preview}\n\n{folded}"


def convert_markdown_quote(
    text: str, *, allow_blockquote: bool = True, allow_expandable: bool = True, fold_summary: str = ""
) -> str:
    """把 Markdown 引用行 (以 '>' 开头) 转成 Telegram 的 <blockquote>。

    引用块超长时按与正文相同的阈值折叠成 <blockquote expandable> (统一折叠规则);
    allow_expandable=False 时引用块保持展开。

    allow_blockquote=False 时把引用前缀整个去掉: 内联消息带 blockquote 实体时,
    Telegram 会丢掉整批格式、整条消息变成纯文本。注意不能只跳过 <blockquote> 标签,
    pyrogram 的 Markdown 解析器只要看到行首 '>' 就会重新生成 blockquote 实体,
    所以那条通道必须把 '>' 前缀也剥掉。
    """
    if not allow_blockquote:
        return _QUOTE_BLOCK_RE.sub(_strip_quote_prefix, text)

    def _replace(match: re.Match) -> str:
        body = _strip_quote_markers(match.group(0))
        if allow_expandable and _should_fold(body):
            return render_expandable_quote(body)
        return f"<blockquote>{body}</blockquote>"

    return _QUOTE_BLOCK_RE.sub(_replace, text)


#: Telegram 富文本（MarkdownV2 同族）里**必须转义**的保留字符 —— 官方文档原话:
#: "characters ``_ * [ ] ( ) ~ ` > # + - = | { } . !`` must be escaped with the
#: preceding character ``\``"（另有 "1..126 任意字符都可用 \ 转义"，一般用不到）。
#: 我们**不能无脑全转** —— 其中 ``* _ ` > [ ] ( )`` 正是本项目自己生成语法用的
#: （粗体 / 引用 / 代码 / 链接），全转会把标题、引用、链接一起打坏。
_TG_RESERVED = frozenset("_*[]()~`>#+-=|{}.!")

#: 需要**原样保留**的语法块（我们生成的，不转义）。
#: 顺序有意义：先保护 HTML 标签 / 代码块 / 占位符，再保护行内语法。
_TG_PROTECT_PATTERNS = (
    # 我们自己的占位符（渲染层后面要替换）
    re.compile(r"\{\{(?:media|poll):\d+\}\}"),
    # 整条 <a …>…</a>（**连内部文本一起保护** —— hashtag 链接的可见文字是 ``#tag``，
    # 只保护标签会把里面的 # 转义掉，服务端就不认这个锚点了）
    re.compile(r"<a\b[^>]*>.*?</a>", re.S),
    # 其余 HTML 标签（<i> / <b> / <code> / <details> / <summary> …）
    re.compile(r"</?[a-zA-Z][^>]*>"),
    # 裸 URL（正文里的 ``https://x.com/a_b__c`` —— 转义 _ 会把链接打断）
    re.compile(r"https?://[^\s\)\]<>\"']+"),
    # 围栏代码块（跨行，先于行内代码）
    re.compile(r"```.*?```", re.S),
    # 行内代码
    re.compile(r"`[^`\n]+`"),
    # 图片 / 链接（含 tg:// 媒体引用）
    re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)"),
    # 粗斜体 / 加粗 / 下划线 / 斜体（成对；长的先匹配）
    re.compile(r"\*\*\*[^*\n]+\*\*\*"),
    re.compile(r"\*\*[^*\n]+\*\*"),
    re.compile(r"(?<!\w)__[^_\n]+__(?!\w)"),
    re.compile(r"(?<!\w)_[^_\n]+_(?!\w)"),
    # 行首引用（> / >>）
    re.compile(r"^>+ ?", re.M),
    # 行首标题
    re.compile(r"^#{1,6} ", re.M),
    # 分隔线（独占一行）
    re.compile(r"^-{3,}$", re.M),
    # 表格行（以 | 开头/结尾；**排除 ||spoiler||** —— 第二个字符是竖线的不是表格）
    re.compile(r"^(?!\|\|)\|.*\|$", re.M),
)


def escape_telegram_markdown(text: str) -> str:
    """把**平台原文**里的 Telegram 保留字符转义，但不破坏我们自己生成的语法。

    背景（2026-10-22）：X 的一条动画预告正文就是 ``||◤ 先行カット＆あらすじ公開 ◢||``，
    Telegram 把 ``||`` 当 spoiler 语法 ⇒ 整行被打码，用户报「为什么识别打码」。
    这跟 ``possibly_sensitive`` 无关（那条是 ``False``），纯粹是**原文字面量撞上渲染语法**。

    做法：先用 :data:`_TG_PROTECT_PATTERNS` 把「我们生成的语法块」挖出来占位，
    再对**剩余文本**按官方清单逐字符转义，最后把占位填回去。这样：

    - 平台原文里的 ``||`` / ``~~`` / ``{`` / ``}`` / ``+`` / ``=`` / ``.`` / ``!``
      等等永远不会被误当语法；
    - 我们自己拼的 ``**粗体**`` / ``> 引用`` / ``[链接](url)`` / ```` ```代码``` ````
      / ``{{media:0}}`` 原样保留。

    转义只加一个反斜杠 —— Telegram 会把它还原成字面字符（官方文档保证 1..126 都可转义）。
    """
    if not text:
        return text
    protected: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        # 用不含保留字符的占位符（\x00 起，正文里不会出现）
        return f"\x00{len(protected) - 1}\x00"

    masked = text
    for pattern in _TG_PROTECT_PATTERNS:
        masked = pattern.sub(_stash, masked)

    escaped = "".join(f"\\{ch}" if ch in _TG_RESERVED else ch for ch in masked)

    # 填回保护块 —— **必须逆序**: 后保护的块里可能嵌着先保护的占位符
    # （``[链接](https://…/a_b)``: URL 先被保护，整条链接再被保护），
    # 正序填会把内层占位符当成字面量留在结果里。
    for idx in reversed(range(len(protected))):
        escaped = escaped.replace(f"\x00{idx}\x00", protected[idx])
    return escaped


def format_text(
    text: str,
    *,
    allow_blockquote: bool = True,
    allow_expandable: bool = True,
    max_length: int | None = None,
    fold_summary: str = "",
) -> str:
    """格式化输出内容, 按需限制长度, 添加折叠块样式。

    **折叠只做一次, 对象是整段内容** (用户要求「整篇只折叠一次」)。

    以前是按引用块把正文切成多段、**每段各自判断**是否超阈值 —— 对引用多的长帖
    (discourse 的楼层引用动辄十几个) 会切出十几个片段, 于是产生一堆「展开全文」按钮
    (实测一篇 linux.do 长帖出了 7 个), 碎得没法读。现在改成: 整段超阈值就整段折一次,
    引用块不再单独折叠 (也不做"折叠里再折叠"——客户端对嵌套折叠没有保证)。

    :param max_length: 超过就截断 (在 markdown 阶段截, 避免切断后面生成的 blockquote 标签)。
        **默认不截断** —— 让"忘了传"时的行为是安全的 (完整渲染), 而不是静默砍掉内容。
        只有**发文件**的路径需要传 (Telegram 的媒体 caption 上限 1024):
        ``send_raw`` / ``send_zip`` / GIF 过多时的纯文字提示。
        富文本正文没有这个限制, 不传即可 (超长靠折叠收起)。
    """
    # 分隔线先规范化: 原文里紧贴上一行的 '---' 在服务端不会被认成分隔线
    text = _normalize_hr(text.strip())
    # ⚠️ 平台原文里的保留字符（``|`` → spoiler、``~`` → 删除线、``_`` → 斜体 …）会被
    # Telegram 当语法解析 —— 实测 X 的一条动画预告正文就是
    # ``||◤ 先行カット＆あらすじ公開 ◢||``，整行被打码，用户报「为什么识别打码」
    # （``possibly_sensitive`` 其实是 False）。统一走 escape_telegram_markdown：
    # 保护我们自己生成的语法块，其余按官方保留字符清单转义。
    text = escape_telegram_markdown(text)
    if max_length is not None and len(text) > max_length:
        text = text[: max_length - 100] + "......"

    if not allow_blockquote:
        # 该通道不支持引用块: 剥掉前缀后按普通文本处理
        text = _QUOTE_BLOCK_RE.sub(_strip_quote_prefix, text)
        if allow_expandable and _should_fold(text):
            return _fold_whole(text, summary=fold_summary)
        return text

    # 先按"引用块不折叠"把引用块转换好, 再拿**整篇**判断是否要折。
    # 引用块的长度算在整篇里, 所以"整篇没超阈值、单个引用块却超"不可能出现 ——
    # 整篇折叠是唯一的折叠点 (也就不会有嵌套折叠)。
    converted = convert_markdown_quote(text, allow_expandable=False, fold_summary=fold_summary)
    if allow_expandable and _should_fold(converted):
        return _fold_whole(converted, summary=fold_summary)
    return converted


def replace_url(platform: Platform | None, v: str) -> str:
    match platform:
        case Platform.WEIXIN:
            v = v.replace("mmbiz.qpic.cn", "qpic.cn.in/mmbiz.qpic.cn")
        case Platform.COOLAPK:
            v = v.replace("image.coolapk.com", "qpic.cn.in/image.coolapk.com")
        case Platform.DOUBAN:
            # 豆瓣图片分片域名 img1~imgN.doubanio.com
            v = re.sub(r"img\d+\.doubanio\.com", r"qpic.cn.in/\g<0>", v)
    return v


def get_supported_platforms() -> str:
    text: list[str] = []
    for i in ParseHub().get_platforms():
        text.append(f"**{i['name']}** __({'__, __'.join(i['supported_types'])})__")
    text.sort(reverse=True)
    return "\n".join(text)


def format_label(text: str) -> str:
    """HTML 粗体的阶段标签 —— 给 **老 caption 路径** (HTML parse mode) 用。"""
    return f"<b>▎{text}</b>"





def parse_channel_ref(value: str) -> int | str:
    """解析频道 ID、用户名或 Telegram 链接，忽略 URL 参数、片段和消息 ID。"""
    channel_id_re = re.compile(r"-100\d{1,19}\Z")
    internal_channel_id_re = re.compile(r"[1-9]\d{0,19}\Z")
    username_re = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
    telegram_link_hosts = frozenset({"t.me", "telegram.me"})

    value = value.strip()
    if not value:
        raise ValueError("频道引用不能为空")

    if channel_id_re.fullmatch(value):
        return int(value)

    url = urlsplit(value if "://" in value else f"https://{value}")
    if url.netloc.lower() in telegram_link_hosts:
        if url.scheme not in {"http", "https"}:
            raise ValueError("仅支持 HTTP 或 HTTPS Telegram 链接")

        path_parts = tuple(part for part in url.path.split("/") if part)
        match path_parts:
            case ("c", internal_channel_id) | ("c", internal_channel_id, _):
                if not internal_channel_id_re.fullmatch(internal_channel_id):
                    raise ValueError("无效的 /c/ 频道链接")
                return int(f"-100{internal_channel_id}")

            case (username,):
                if username_re.fullmatch(username):
                    return f"@{username}"
                raise ValueError("频道用户名格式无效")

            case _:
                raise ValueError("Telegram 链接必须包含用户名或 /c/ 频道 ID")

    if "://" in value:
        raise ValueError("仅支持 t.me 或 telegram.me 链接")

    username = value.removeprefix("@")
    if not username_re.fullmatch(username):
        raise ValueError("频道用户名格式无效")

    return f"@{username}"


def get_thread_id(msg: Message) -> int | None:
    return msg.message_thread_id or (1 if msg.chat and msg.chat.is_forum else None)
