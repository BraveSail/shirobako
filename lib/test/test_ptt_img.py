"""PTT 里 <img> 标签的图（div.richcontent 包装）要抽成 media 并在原位。"""

from pathlib import Path

from parsehub.provider_api.ptt import PttAPI

# M.1793266681：PTT 原生的"发图"形态 —— 一条裸链接 + 一个 div.richcontent > img，
# 两者指向同一张图。
IMG_FIXTURE = Path(__file__).parent / "fixtures" / "ptt_article_img.html"


def test_img_tag_is_extracted_as_media():
    """<img src> 抽成 images + 原位 {{media:N}}"""
    a = PttAPI.parse_html(IMG_FIXTURE.read_text(encoding="utf-8"))
    assert a.images == ["https://i.mopix.cc/YRTfNb.jpg"]
    assert a.body.startswith("{{media:0}}")


def test_bare_link_for_the_same_image_is_not_duplicated():
    """同一条裸链接不该再出现（图已经由 media 表示）"""
    a = PttAPI.parse_html(IMG_FIXTURE.read_text(encoding="utf-8"))
    assert "mopix.cc" not in a.body
    assert a.body.count("{{media:0}}") == 1


def test_media_download_is_required():
    """抽成 media 的图**必须**让流水线真下载 —— 否则富文本路径跳过下载，
    占位符没有对应媒体会被渲染层删掉（用户实测「还是没图」）。"""
    from parsehub.parsers.parser.ptt import PttRichTextParseResult

    # 属性在 **result 类**上，不在 parser 上（base 不会把 parser 的属性搬过去）
    assert PttRichTextParseResult.requires_media_download is True


def test_body_text_survives_and_signature_is_gone():
    """图片之后的正文与签名处理都正常"""
    a = PttAPI.parse_html(IMG_FIXTURE.read_text(encoding="utf-8"))
    assert "看預購圖挺可愛的" in a.body
    assert "發信站" not in a.body
    assert not a.body.rstrip().endswith("--")
