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


def test_image_urls_stay_plain_links():
    """PTT 的图片是纯文本链接 —— 直接留在正文里，**不抽成 media**

    （用户定：这种直接放链接就行。抽成媒体反而多一次下载，PTT 也没有图注。）
    """
    a = _article()
    assert a.images == []
    assert "{{media:" not in a.body
    assert "https://i.imgur.com/Oo9D6mx.jpeg" in a.body
    assert "https://i.imgur.com/tQm2XSB.jpeg" in a.body
    # 没被包成 markdown 链接（正文里别处的外链仍是 [url](url)，那是正常的）
    assert "](https://i.imgur.com/" not in a.body


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
    # **每条推文各自一个引用块**（行间空行 → 服务端切成独立 blockquote）
    assert "> 推 gsock: 6\n\n> 推 lolicon" in md
    # 整段推文收在 <details> 里: 否则几百条引用会把消息撑得很高，
    # 客户端自动折叠（"显示更多"）会把末尾页脚盖住
    assert "<details><summary>推文（3 條）</summary>" in md
    assert md.rstrip().endswith("</details>")
    # 截断提示只在超出预算时出现（fixture 只有 3 条，不该有）
    assert "未顯示" not in md


def test_parser_matches_ptt_urls():
    assert PttParser.match("https://www.ptt.cc/bbs/C_Chat/M.1792932204.A.E69.html")
    assert PttParser.match("https://ptt.cc/bbs/C_Chat/M.1.A.1.html")
    assert not PttParser.match("https://example.com/x.html")
    assert not PttParser.match("https://www.ptt.cc/bbs/")
