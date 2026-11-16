"""小黑盒长文的附图不能被流水线跳过下载。

见 lib/test/test_xiaoheihe_article_media.py 的完整背景。这里只钉 bot 侧那一环：
`should_skip_richtext_download` 看到 `requires_media_download=True` 时必须**不跳过**。

以前小黑盒长文用的是裸 `RichTextParseResult`（默认 False）⇒ 跳过下载 ⇒
`media_urls` 为空 ⇒ 渲染层 `rewrite_external_images` 把正文外链图全剥掉
（日志：「正文外链图不在媒体列表里, 已剥掉(防 Telegram 拒收)」）⇒ 用户看到「怎么没图？」。
"""

from __future__ import annotations

from parsehub.parsers.parser.xiaoheihe import XiaoHeiHeRichTextParseResult
from parsehub.types import RichTextParseResult

from services.pipeline import should_skip_richtext_download


class TestXiaoHeiHeArticleDownload:
    def test_does_not_skip_download(self) -> None:
        result = XiaoHeiHeRichTextParseResult(
            title="Viper已提前回韩国",
            markdown_content="正文\n\n![](https://imgheybox.max-c.com/a.jpg)",
        )
        assert should_skip_richtext_download(result, richtext_skip_download=True) is False

    def test_bare_richtext_still_skips(self) -> None:
        """其它富文本平台（正文真内嵌外链、Telegram 自己抓）语义别被改掉。"""
        result = RichTextParseResult(title="t", markdown_content="正文")
        assert should_skip_richtext_download(result, richtext_skip_download=True) is True

    def test_config_false_forces_download(self) -> None:
        result = RichTextParseResult(title="t", markdown_content="x")
        assert should_skip_richtext_download(result, richtext_skip_download=False) is False
