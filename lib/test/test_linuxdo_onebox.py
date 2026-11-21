"""linux.do 的外链预览卡片（`aside.onebox`）必须变成引用块，不能污染正文。

## 用户报障

> 格式有问题，前面那一堆是引用的东西，md 格式错乱

原始 `cooked` 里，Discourse 为外部链接自动生成了一张预览卡片：

```html
<aside class="onebox allowlistedgeneric">
  <header class="source">
    <img src=".../favicon.ico" class="site-icon">      ← 站标，下载回来 Pillow 认不出
    <a href="...">Lean community blog – 21 Nov 26</a>
  </header>
  <article class="onebox-body">
    <img width="500" src="...500x500.png" class="thumbnail onebox-avatar">  ← 站点缩略图
    <h3><a href="...">Lean verification of the Riemann Hypothesis</a></h3>
    <p>...摘要...</p>
  </article>
</aside>
```

原样转 markdown 的后果：卡片被拆成散乱的标题/摘要/**两张图**，而且那两张图被
`_extract_images` 当成正文媒体抽走 —— favicon 下载失败还会占掉 `{{media:0}}`
的位置。用户看到的就是"正文前面多了一堆东西"。

正确形态：整块卡片 → 一个引用块（链接 + 摘要），**卡片里的图不进媒体列表**。
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from parsehub.provider_api.linuxdo import LinuxDoTopic

ONEBOX = """
<aside class="onebox allowlistedgeneric" data-onebox-src="https://example.com/post">
  <header class="source">
      <img src="https://example.com/img/favicon.ico" class="site-icon" alt="" width="48" height="48">
      <a href="https://example.com/post" target="_blank" rel="noopener" title="01:55PM">Example blog – 21 Nov 26</a>
  </header>
  <article class="onebox-body">
    <img width="500" height="500" src="https://cdn.example.com/thumb_500x500.png"
         class="thumbnail onebox-avatar" alt="">
    <h3><a href="https://example.com/post" target="_blank" rel="noopener">A real article title</a></h3>
    <p>A real article title
Joint statement by the communities.</p>
  </article>
  <div class="onebox-metadata"></div>
  <div style="clear: both"></div>
</aside>

<p>This is the actual post body.</p>
"""


def _soup() -> BeautifulSoup:
    return BeautifulSoup(ONEBOX, "lxml")


class TestOneboxImagesAreNotPostMedia:
    """卡片里的站标和缩略图不是发帖人贴的图。"""

    def test_favicon_and_thumbnail_are_excluded(self) -> None:
        images = LinuxDoTopic._extract_images(_soup())
        urls = [i.url for i in images]
        assert not any("favicon.ico" in u for u in urls), "站标不能当正文图"
        assert not any("thumb_500x500" in u for u in urls), "卡片缩略图不能当正文图"
        assert urls == []

    def test_real_post_image_still_extracted(self) -> None:
        """不能因为跳过 onebox 就把正文里真正的图也漏掉。"""
        html = ONEBOX + '<p><img src="https://i.example.com/real.png" width="800"></p>'
        images = LinuxDoTopic._extract_images(BeautifulSoup(html, "lxml"))
        assert [i.url for i in images] == ["https://i.example.com/real.png"]


class TestOneboxBecomesQuote:
    def test_card_content_lands_in_a_blockquote(self) -> None:
        soup = _soup()
        LinuxDoTopic._onebox_to_quote(soup)
        quotes = soup.find_all("blockquote")
        assert len(quotes) == 1, "整块卡片应折叠成一个引用块"
        text = quotes[0].get_text(" ", strip=True)
        assert "A real article title" in text
        assert "Joint statement by the communities" in text

    def test_no_aside_left_behind(self) -> None:
        soup = _soup()
        LinuxDoTopic._onebox_to_quote(soup)
        assert soup.find("aside") is None

    def test_link_points_at_the_article(self) -> None:
        soup = _soup()
        LinuxDoTopic._onebox_to_quote(soup)
        link = soup.find("blockquote").find("a")
        assert link is not None
        assert link["href"] == "https://example.com/post"

    def test_title_is_not_repeated(self) -> None:
        """标题在 h3、链接文字、摘要开头各出现一次 —— 只保留一行。"""
        soup = _soup()
        LinuxDoTopic._onebox_to_quote(soup)
        text = soup.find("blockquote").get_text(" ", strip=True)
        assert text.count("A real article title") == 1, f"标题重复了: {text!r}"

    def test_actual_post_body_stays_outside_the_quote(self) -> None:
        soup = _soup()
        LinuxDoTopic._onebox_to_quote(soup)
        quote = soup.find("blockquote")
        assert quote is not None
        assert "This is the actual post body" not in quote.get_text()

    def test_card_without_text_is_dropped(self) -> None:
        """没有任何可读内容的卡片直接丢掉，不要留空引用块。"""
        html = '<aside class="onebox"><div class="onebox-metadata"></div></aside>'
        soup = BeautifulSoup(html, "lxml")
        LinuxDoTopic._onebox_to_quote(soup)
        assert soup.find("aside") is None
        assert soup.find("blockquote") is None


class TestFullSimplifyPipeline:
    """走完整 `_simplify`：卡片变引用块 + 图不进列表，正文保持独立。"""

    def test_simplify_renders_card_as_quote(self) -> None:
        soup = _soup()
        LinuxDoTopic._simplify(soup)
        assert soup.find("aside") is None
        assert soup.find("blockquote") is not None
        assert "This is the actual post body" in soup.get_text()
