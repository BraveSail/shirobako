"""Project the deployed native X status into the shared source document."""
from dataclasses import replace

from .document import Block, Document, TextRun, paragraph
from .x_status import XPost, XStatus


def _signer(post: XPost) -> tuple[TextRun, ...]:
    name = post.author_name.strip()
    handle = post.author_handle.strip().lstrip("@")
    if handle and name and name.casefold() != handle.casefold():
        return (TextRun(name, post.author_url), TextRun(" "), TextRun(f"@{handle}", styles=("code",)))
    label = f"@{handle}" if handle else name
    return (TextRun(label, post.author_url),) if label else ()


def _body(post: XPost, article: tuple[Block, ...] | None = None) -> tuple[Block, ...]:
    blocks = list(article) if article is not None else (
        [Block("paragraph", runs=post.segments, role="x_body")] if post.segments else []
    )
    if post.poll_index is not None:
        blocks.append(Block("poll", index=post.poll_index))
    if post.youtube:
        blocks.append(paragraph((post.youtube,)))
    return tuple(blocks)


def _related(post: XPost, role: str) -> Block:
    return Block("quote", runs=_signer(post), children=_body(post), role=role)


def document_from_x(status: XStatus, *, article: tuple[Block, ...] | None = None) -> Document:
    """Preserve native runs and role/poll identities, without looking at legacy Markdown."""
    blocks = []
    if status.reply is not None:
        blocks.append(_related(status.reply, "reply"))
    blocks.extend(_body(status.main, article))
    if card := status.trending:
        children = []
        if card.title:
            children.append(paragraph((TextRun(card.title, styles=("bold",)),)))
        if card.cover_index is not None:
            children.append(Block("media", index=card.cover_index))
        if card.subtitle:
            children.append(paragraph(card.subtitle))
        if card.url:
            children.append(Block("localize", runs=(TextRun("查看原話題", card.url),)))
        if children:
            blocks.append(Block("quote", children=tuple(children), role="trending"))
    if status.quoted is not None:
        blocks.append(_related(status.quoted, "quoted"))
    return Document(tuple(blocks))


def replace_x_document_text(document: Document | None, status: XStatus) -> Document:
    """Refresh #t display text only; preserve source articles, polls, cards and media slots."""
    if document is None:
        return document_from_x(status)

    def body(block: Block, post: XPost | None) -> Block:
        return replace(block, runs=post.segments) if post and block.role == "x_body" else block

    blocks = []
    for block in document.blocks:
        if block.kind == "quote" and block.role in {"reply", "quoted"}:
            post = status.reply if block.role == "reply" else status.quoted
            block = replace(block, children=tuple(body(child, post) for child in block.children))
        else:
            block = body(block, status.main)
        blocks.append(block)
    return replace(document, blocks=tuple(blocks))


def bind_x_attachments(
    document: Document, *, main: tuple[Block, ...] = (), reply: tuple[Block, ...] = (),
    quoted: tuple[Block, ...] = (), trending_url: str = "",
    reply_article: tuple[Block, ...] = (), quoted_article: tuple[Block, ...] = (),
) -> Document:
    """Attach source-positioned article bodies and download refs absent from XStatus."""
    blocks = []
    for block in document.blocks:
        if block.kind == "quote" and block.role == "reply":
            block = replace(block, children=(*reply_article, *block.children, *reply))
        elif block.kind == "quote" and block.role == "quoted":
            blocks.extend(main)
            main = ()
            block = replace(block, children=(*quoted_article, *block.children, *quoted))
        elif block.kind == "quote" and block.role == "trending":
            block = replace(block, children=tuple(
                replace(child, url=trending_url) if child.kind == "media" else child for child in block.children
            ))
        blocks.append(block)
    return replace(document, blocks=(*blocks, *main))
