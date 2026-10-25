"""PTT 文章解析（fixture 取自真实页面，见 fixtures/ptt_article.html）。"""

from pathlib import Path

from parsehub.parsers.parser.ptt import PttParser
from parsehub.provider_api.ptt import PttAPI

FIXTURE = Path(__file__).parent / "fixtures" / "ptt_article.html"


def _article():
    return PttAPI.parse_html(FIXTURE.read_text(encoding="utf-8"))


def test_metaline_fields():
    """作者拆成「显示名 / 登录名」，时间按台北时区解析"""
    a = _article()
    assert a.title.startswith("[蔚藍]")
    assert a.author_name == "大炎上、確定ですわ。"
    assert a.author_handle == "anpinjou"
    assert a.board == "C_Chat"
    assert a.published_at is not None
    assert a.published_at.utcoffset().total_seconds() == 8 * 3600


def test_images_are_marked_in_place():
    """正文里的图片 URL 换成 {{media:N}}，下标与 images 顺序一致"""
    a = _article()
    assert len(a.images) == 2
    assert a.images[0].endswith(".jpeg")
    first = a.images[0]
    # 同一张图出现两次 → 复用同一下标
    assert a.body.count(f"{{{{media:{a.images.index(first)}}}}}") >= 1
    assert "{{media:0}}" in a.body
    assert "{{media:1}}" in a.body


def test_signature_is_dropped():
    """※ 發信站 / 文章網址 那两行不进正文"""
    a = _article()
    assert "發信站" not in a.body
    assert "文章網址" not in a.body


def test_pushes_are_parsed():
    """推文保留 推/噓/→ + 用户名 + 内容（去掉 ': ' 前缀）"""
    a = _article()
    assert len(a.pushes) == 3
    first = a.pushes[0]
    assert first.tag == "推"
    assert first.user == "gsock"
    assert first.content == "6"
    assert not first.content.startswith(":")


def test_render_keeps_ptt_push_style():
    """渲染保留 PTT 原生形态：``推 user: 内容``"""
    md = PttParser._render(_article())
    assert "**推文**" in md
    assert "推 gsock: 6" in md
    # 正文里的图片标记要留着（渲染层替换成媒体引用）
    assert "{{media:0}}" in md


def test_parser_matches_ptt_urls():
    assert PttParser.match("https://www.ptt.cc/bbs/C_Chat/M.1792932204.A.E69.html")
    assert PttParser.match("https://ptt.cc/bbs/C_Chat/M.1.A.1.html")
    assert not PttParser.match("https://example.com/x.html")
    assert not PttParser.match("https://www.ptt.cc/bbs/")
