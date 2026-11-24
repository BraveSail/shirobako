from pyrogram.types import InputRichBlockParagraph, InputRichMessage, RichTextBold


def rich_notice(text: str) -> InputRichMessage:
    """Build a single literal paragraph without parsing HTML or Markdown."""
    return InputRichMessage(blocks=[InputRichBlockParagraph(text)])


def notice_label(text: str) -> InputRichBlockParagraph:
    return InputRichBlockParagraph(RichTextBold(f"▎{text}"))
