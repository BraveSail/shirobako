"""下载失败（内容不是图片）时：降级成链接，**索引不能错位**。

## 用户口径

> 下载失败你保留 url 不就行了？

图床常对服务器 IP 限流（429）或返回 HTML 错误页。PTT 这类平台还会把
「取不到图的链接」也渲染成 `<img>`。此时：

1. **不能把整条解析弄挂**（原来会抛 ``DownloadError``）；
2. **也不能把这一项从列表里删掉** —— ``media_refs`` 与 ``processed_list``
   是按位置 ``zip`` 的，删一项会让**后面所有媒体错位**（把 A 图显示到 B 的位置）。

正确形态：留一个 ``BrokenMedia`` 占位（带原 URL），渲染层把
``{{media:N}}`` 换成一个链接。
"""

from __future__ import annotations

import pytest

from parsehub.types import BrokenMedia, ImageFile
from parsehub.types.media_file import MediaFile


class TestBrokenMedia:
    def test_carries_the_original_url(self) -> None:
        m = BrokenMedia(path="", url="https://imgur.com/WLAHkWh.gif")
        assert m.url == "https://imgur.com/WLAHkWh.gif"
        assert str(m.path) == ""

    def test_does_not_touch_the_filesystem(self) -> None:
        """空 path 不能触发 MediaInfoReader（那会去读文件，path 为空必炸）。"""
        m = BrokenMedia(path="")
        assert m.width == 0
        assert m.height == 0

    def test_is_a_media_file(self) -> None:
        assert isinstance(BrokenMedia(path="", url="x"), MediaFile)

    def test_not_confused_with_image_file(self) -> None:
        """渲染层靠 isinstance 分流；BrokenMedia 不能被认成可发送的图片。"""
        assert not isinstance(BrokenMedia(path="", url="x"), ImageFile)


class TestRenderFallback:
    """渲染层：BrokenMedia 占位 → 一个链接文本，且**占位符数量不变**。"""

    def test_placeholder_is_a_link(self) -> None:
        from plugins.parse.inline_rich import build_rich_media
        from services.media import ProcessedMedia

        ref_url = "https://i.urusai.cc/Wm3EI.gif"
        broken = BrokenMedia(path="", url="https://imgur.com/WLAHkWh.gif")
        media_refs = [_FakeRef("https://imgur.com/WLAHkWh.gif"), _FakeRef(ref_url)]
        processed = [
            ProcessedMedia(broken, None),
            ProcessedMedia(ImageFile(path=__file__), None),
        ]

        _, placeholders, _, _, _, urls = build_rich_media(media_refs, processed)
        # 坏图降级成链接，但仍占一个位置
        assert len(placeholders) == 2, "占位符数量必须与 media_refs 对齐"
        assert "imgur.com/WLAHkWh.gif" in placeholders[0]
        assert placeholders[0].startswith("[")
        assert urls[0] == "https://imgur.com/WLAHkWh.gif"


class _FakeRef:
    """最小 MediaRef 替身（只需 url / thumb_url / type）。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.thumb_url = url
        self.width = 0
        self.height = 0


@pytest.mark.parametrize("n", [1, 2, 5])
def test_alignment_with_mixed_good_and_broken(n: int) -> None:
    """好坏混合时占位符数量恒等于 refs 数量 —— 这是防错位的核心断言。"""
    from plugins.parse.inline_rich import build_rich_media
    from services.media import ProcessedMedia

    refs = [_FakeRef(f"https://example.com/{i}.gif") for i in range(n)]
    processed = []
    for i in range(n):
        if i % 2 == 0:
            processed.append(ProcessedMedia(BrokenMedia(path="", url=refs[i].url), None))
        else:
            processed.append(ProcessedMedia(ImageFile(path=__file__), None))

    _, placeholders, _, _, _, _ = build_rich_media(refs, processed)
    assert len(placeholders) == n
