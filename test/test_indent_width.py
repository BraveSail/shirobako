"""平台原文的缩进必须**原样保留**，由 blocks 结构化通道承载。

历史（两个阶段的教训）：

1. 行首 4+ 空格在 **markdown 字符串通道** 里被解析成缩进代码块（X 的动画预告
   整行变代码块）。当时的补丁是「换成全角空格」。
2. 那个补丁把缩进**宽度翻倍**（全角占两个字宽），把平台用半角空格做的对齐整体
   推歪：``◤　　　◥`` 边框内 11 个半角空格的标题被推出边框（用户报「拆东墙补西墙」）。

根因是**通道选错**：`InputRichMessageMarkdown` 的语义是"这段是 markdown，请解析"，
而我们喂的是平台原文（字面量）。改成 `InputRichMessage(blocks=[...])` 后，每个块
携带字面量文本、服务端不解析 —— 缩进不需要改写，也不需要任何字符替换补丁。
"""
import pytest

from plugins.parse.rich_blocks import markdown_to_blocks


def _paragraph_texts(block) -> str:
    """把段落块的文本拼回来（parse_inline 无格式时返回 str，有格式时返回片段列表）。"""
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "".join(p if isinstance(p, str) else getattr(p, "text", "") for p in text)
    return ""


def _first_paragraph(markdown: str) -> str:
    blocks = markdown_to_blocks(markdown)
    assert blocks, "应当产出至少一个块"
    return _paragraph_texts(blocks[0])


@pytest.mark.parametrize("indent", [4, 5, 6, 11, 15])
def test_indent_survives_as_literal_spaces(indent):
    """行首半角空格按原数保留，一个都不换（换成全角会让宽度翻倍、对齐错位）。"""
    text = _first_paragraph(" " * indent + "キービジュアル")
    assert text.startswith(" " * indent + "キービジュアル"), f"{indent} 个空格没有原样保留"
    assert "\u3000" not in text, "不应引入全角空格"


def test_border_and_title_keep_their_alignment():
    """实测样例：边框用全角空格、框内标题用半角空格，两者宽度必须仍然对齐。"""
    source = "\n".join(
        [
            "◤" + "\u3000" * 11 + "◥",
            " " * 11 + "キービジュアル",
            " " * 15 + "第2弾公開",
            "◣" + "\u3000" * 11 + "◢",
        ]
    )
    blocks = markdown_to_blocks(source)
    lines = _paragraph_texts(blocks[0]).splitlines()
    assert lines[0] == "◤" + "\u3000" * 11 + "◥"
    # 11 个半角空格，宽度与边框内 11 个全角空格不一致是**原文如此**（半角 11 ≈ 全角 5.5），
    # 关键是我们没有动它 —— 原文怎么排就怎么发。
    assert lines[1] == " " * 11 + "キービジュアル"
    assert lines[2] == " " * 15 + "第2弾公開"
    assert lines[3] == "◣" + "\u3000" * 11 + "◢"


def test_blocks_path_does_not_produce_indented_code_blocks():
    """blocks 通道里缩进只是段落文本的一部分，永远不会变成代码块块类型。"""
    from pyrogram.types import InputRichBlockPreformatted

    blocks = markdown_to_blocks("      TVアニメ『あかね噺』")
    assert not any(isinstance(b, InputRichBlockPreformatted) for b in blocks)
