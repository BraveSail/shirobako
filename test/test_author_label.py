"""format_author_label 的离线单元测试 (纯函数, 不碰网络/库)。"""

import pytest
from parsehub.utils.helpers import format_author_label

# ── 两边都有 · 不等 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "handle", "expected"),
    [
        ("张三", "zhangsan", "张三 @zhangsan"),
        ("Author", "author_handle", "Author @author_handle"),
        (" Author ", "author_handle", "Author @author_handle"),
        ("Author", " author_handle ", "Author @author_handle"),
        ("Author", "@author_handle", "Author @author_handle"),
        ("Author", "@ author_handle", "Author @author_handle"),
    ],
)
def test_both_present_and_different(name, handle, expected):
    assert format_author_label(name, handle) == expected


def test_different_never_produces_double_at():
    assert "@@" not in format_author_label("Author", "@@author_handle")


# ── 两边都有 · 相等 (忽略大小写/空白/@ 前缀/去重) ────────────────


@pytest.mark.parametrize(
    ("name", "handle", "expected"),
    [
        ("Author", "author", "@author"),  # 大小写差异
        ("Author", "@author", "@author"),  # handle 自带 @
        ("Author", "@@author", "@author"),  # handle 重复 @
        ("  Author  ", "author", "@author"),  # name 首尾空白
        ("Author", "  author  ", "@author"),  # handle 首尾空白
        ("Author", " @Author ", "@Author"),  # 大小写 + 空白 + @ 混合
    ],
)
def test_both_present_and_equal_only_keeps_handle(name, handle, expected):
    assert format_author_label(name, handle) == expected
    assert "@@" not in format_author_label(name, handle)


# ── 只有一边 ──────────────────────────────────────────────────


def test_only_name():
    assert format_author_label("Author") == "Author"
    assert format_author_label("  Author  ") == "Author"


def test_only_handle():
    assert format_author_label("", "author") == "@author"
    assert format_author_label("", "@author") == "@author"
    assert format_author_label("  ", "  @author  ") == "@author"


# ── 都空 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "handle"),
    [
        ("", ""),
        ("  ", "  "),
        ("", "@"),
        ("", "   @   "),
    ],
)
def test_both_empty(name, handle):
    assert format_author_label(name, handle) == ""
