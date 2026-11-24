from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from parsehub.types.document import Document, html_blocks, visible_text

from ..utils import http
from ..utils.helpers import UA, get_author_name


@dataclass
class Coolapk:
    title: str = ""
    markdown_content: str = ""
    text_content: str = ""
    imgs: list[str] | None = None
    author_name: str = ""
    document: Document | None = None

    @classmethod
    async def parse(cls, url: str, proxy: str | None = None) -> "Coolapk":
        async with http.AsyncClient(headers={"User-Agent": UA}, proxy=proxy) as client:
            result = await client.get(url)
        soup = BeautifulSoup(result.text, "lxml")
        author = soup.select_one(".feed-user-name, .feed-username, .user-name, .username")
        author_name = author.get_text(strip=True) if author else ""
        if not author_name and (meta := soup.find("meta", {"name": "author"})):
            author_name = get_author_name(meta.get("content"))
        # 酷安网页版不加载实况照片
        title_element = soup.find(class_="message-title")
        if title_element and (title := title_element.text.strip()):
            content = soup.find(class_="feed-article-message")
            if content is None:
                if "s=" in url or "shareKey=" in url:
                    raise ValueError("获取内容失败")
                raise ValueError("获取内容失败, 分享时请保留 shareKey 或 s 参数")
            markdown_content = MarkdownConverter(heading_style="ATX").convert(str(content))
            imgs: list[str] = []
            media: dict[str, int] = {}
            for img in content.find_all("img"):
                if "message-image" not in img.get("class", []) or not img.get("src"):
                    img.decompose()
                    continue
                src = urljoin(url, str(img["src"]))
                media.setdefault(src, len(imgs))
                imgs.append(src)
                img.attrs = {"src": src}
            blocks = html_blocks(content, base_url=url, media=media)
            text_content = "\n".join(filter(None, (visible_text((block,)) for block in blocks)))
            return cls(title, markdown_content, text_content, imgs, author_name=author_name, document=Document(blocks))

        feed_element = soup.find(class_="feed-message")
        if feed_element and (feed_content := feed_element.text.strip()):
            message_image_group = soup.find(class_="message-image-group")
            imgs = (
                [urljoin(url, str(i["src"])) for i in message_image_group.select("img[src]")]
                if message_image_group else []
            )
            return cls("", "", feed_content, imgs, author_name=author_name)

        raise ValueError("获取内容失败, 分享时请保留 shareKey 或 s 参数")
