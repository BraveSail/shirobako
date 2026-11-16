"""小黑盒长文（ARTICLE）必须带回附图。

## 真实故障（2026-11-16，用户报「怎么没图？」）

`https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?...link_id=3e4e72d44705`

解析结果 `media=5` 看着没问题，但发出去一张图都没有。两个独立问题叠在一起：

### 1. 没声明 ``requires_media_download``

`RichTextParseResult` 基类默认 `False`，注释写着「正文已有外链图，Telegram 自己抓」。
但渲染层 `rewrite_external_images` **无条件**把正文外链图改写成上传媒体引用，
对不上 `media_urls` 的直接剥掉（防外链 404/防盗链导致整条 400）。
于是跳过下载 ⇒ `media_urls` 为空 ⇒ 5 张图全被剥。

⇒ 子类 `XiaoHeiHeRichTextParseResult` 置 `requires_media_download = True`。
⚠️ 必须在 **result 类**上，`should_skip_richtext_download` 读的是
`parse_result.requires_media_download`（PTT 踩过同一个坑）。

### 2. 正文与 media 用的是两套 CDN 域名

| 来源 | URL |
| --- | --- |
| 正文 HTML 的 `data-original` | `imgheybox.max-c.com/…/thumb.jfif` |
| `text_list[1:]` 的 img 节点 | `imgheybox1.max-c.com/…/thumb.jfif?imageMogr2/…` |

**path 完全相同，host 与 query 不同** ⇒ `rewrite_external_images` 按整串 URL 匹配
对不上，照样剥。所以 provider 用正文那批 URL 覆盖 media（逐位对齐时）。

两个都修了才出图：只修第 1 个，图能下载但正文里仍被剥；只修第 2 个，
URL 对上了但根本不下。
"""

from __future__ import annotations

from parsehub.parsers.parser.xiaoheihe import XiaoHeiHeParser, XiaoHeiHeRichTextParseResult
from parsehub.types import PostType

LINK_ID = "3e4e72d44705"
SHARE_URL = (
    "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share"
    f"?h_camp=link&h_src=YXBwX3NoYXJl&link_id={LINK_ID}&new_post_share_style_v2=1"
)

#: 正文 HTML 里 ``data-original`` 的形态（懒加载属性，不是 src）
BODY_HTML = (
    '<p class="pgc-p">第一段</p>'
    '<p><img data-description="" data-height="894" data-type="image" data-width="970" '
    'data-original="https://imgheybox.max-c.com/bbs/2026/11/16/aaa/thumb.jfif"/></p>'
    '<p class="pgc-p">第二段</p>'
    '<p><img data-description="" data-height="676" data-type="image" data-width="1222" '
    'data-original="https://imgheybox.max-c.com/bbs/2026/11/16/bbb/thumb.jfif"/></p>'
)

#: 同一批图在 text_list[1:] 里的形态：**另一个 host + 查询参数**
IMG_NODES = [
    {
        "type": "img",
        "url": "https://imgheybox1.max-c.com/bbs/2026/11/16/aaa/thumb.jfif?imageMogr2/format/jpg/thumbnail/850x1450",
        "width": "970",
        "height": "894",
    },
    {
        "type": "img",
        "url": "https://imgheybox1.max-c.com/bbs/2026/11/16/bbb/thumb.jfif?imageMogr2/format/jpg/thumbnail/850x1450",
        "width": "1222",
        "height": "676",
    },
]


class TestRequiresMediaDownload:
    def test_result_class_declares_download(self) -> None:
        """★ 核心：不声明就会跳过下载 → 图全被剥。"""
        assert XiaoHeiHeRichTextParseResult.requires_media_download is True

    def test_base_class_stays_false(self) -> None:
        """基类语义别动 —— 别的富文本平台（正文真内嵌外链）仍靠它跳过下载。"""
        from parsehub.types import RichTextParseResult

        assert RichTextParseResult.requires_media_download is False

    def test_result_type_is_richtext(self) -> None:
        assert XiaoHeiHeRichTextParseResult(title="t").type == PostType.RICHTEXT


class TestUrlAlignment:
    """provider 必须让正文 URL 与 media URL 逐字一致，否则渲染层按串匹配不上。"""

    def test_body_urls_win(self) -> None:
        parser = XiaoHeiHeParser()
        post = _build_post(parser, BODY_HTML, IMG_NODES)
        body_urls = [m.group(1) for m in __import__("re").finditer(r"!\[\]\(([^)\s]+)\)", post.content)]
        media_urls = [m.url for m in post.media]
        assert body_urls == media_urls, "正文与 media 的 URL 必须逐字相同"

    def test_host_is_the_body_one(self) -> None:
        parser = XiaoHeiHeParser()
        post = _build_post(parser, BODY_HTML, IMG_NODES)
        for m in post.media:
            assert "imgheybox.max-c.com" in m.url
            assert "imgheybox1" not in m.url

    def test_count_mismatch_keeps_originals(self) -> None:
        """数量对不上就不覆盖 —— 宁可少换，也不要错位。"""
        parser = XiaoHeiHeParser()
        extra = [*IMG_NODES, {"type": "img", "url": "https://imgheybox1.max-c.com/ccc.jpg", "width": "10", "height": "10"}]
        post = _build_post(parser, BODY_HTML, extra)
        assert any("imgheybox1" in m.url for m in post.media), "数量不一致时不该覆盖"


def _build_post(parser: XiaoHeiHeParser, html: str, img_nodes: list[dict]):
    """复刻 provider 的组装逻辑（逐行对齐源码，只换成给定素材）。"""
    import json
    import re

    from parsehub.provider_api.xiaoheihe import (
        XHHConverter,
        XiaoHeiHeMedia,
        XiaoHeiHeMediaType,
        XiaoHeiHePost,
        XiaoHeiHePostType,
    )

    text_list = [{"type": "html", "text": html}, *img_nodes]
    body_image_urls: list[str] = []
    if text_list[0]["type"] == "html":
        content = XHHConverter(heading_style="ATX").convert(text_list[0]["text"])
        body_image_urls = re.findall(r"!\[\]\(([^)\s]+)\)", content)
    else:
        content = text_list[0]["text"]

    images = []
    for image in text_list[1:]:
        if image["type"] == "img":
            media_type = XiaoHeiHeMediaType.GIF if ".gif" in image["url"] else XiaoHeiHeMediaType.IMAGE
            images.append(
                XiaoHeiHeMedia(
                    type=media_type,
                    url=image["url"],
                    thumb_url=image["url"],
                    height=int(float(image.get("height", 0))),
                    width=int(float(image.get("width", 0))),
                )
            )
    if body_image_urls and len(body_image_urls) == len(images):
        for media, url in zip(images, body_image_urls, strict=True):
            media.url = url
            media.thumb_url = url

    return XiaoHeiHePost(
        type=XiaoHeiHePostType.ARTICLE, title="t", content=content, media=images, author_name="a"
    )
