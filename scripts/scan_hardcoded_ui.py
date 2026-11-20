#!/usr/bin/env python3
"""扫描**面向用户但没走 i18n** 的硬编码中文文案。

与 ``audit_i18n.py`` 分工：那个查「调了 ``t_()`` 但缺译/坏译」；
这个查「**根本没调** ``t_()``」的。

## 判据（收紧版，只看真会显示给用户的）

1. 字符串含 CJK；
2. 不在 ``t_()`` / ``_t()`` / ``t_[lang]()`` 调用里；
3. 不在日志调用（``logger.*``）里；
4. 不是 docstring；
5. 不是异常消息（``raise X("...")``）；
6. 输出按文件分组，并标出所在函数（便于判断是否面向用户）。

用法::

    python scripts/scan_hardcoded_ui.py [--root .] [--with-lib]
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

_UI_DIRS = ("plugins", "services", "core", "db", "repo", "utils")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_T_NAMES = {"t_", "_t"}
_LOG_NAMES = {
    "debug",
    "info",
    "warning",
    "error",
    "exception",
    "critical",
    "trace",
    "log",
    "success",
    "warning_once",
    "catch",
}
_SKIP = {
    ".venv",
    "__pycache__",
    ".git",
    "node_modules",
    "i18n",
    "test",
    "tests",
    "downloads",
    "doc",
}


class Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str, str]] = []
        self.translated: set[int] = set()
        self.internal: set[int] = set()
        self.docstrings: set[int] = set()
        self._stack: list[str] = []

    def _mark_docstring(self, node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if not body:
            return
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                self.docstrings.add(id(first.value))

    def visit_Module(self, node: ast.Module) -> None:
        self._mark_docstring(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self._mark_docstring(node)
        self.generic_visit(node)
        self._stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self._mark_docstring(node)
        self.generic_visit(node)
        self._stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(f"class:{node.name}")
        self._mark_docstring(node)
        self.generic_visit(node)
        self._stack.pop()

    def _mark(self, node: ast.Call, target: set[int]) -> None:
        for a in node.args:
            target.add(id(a))
        for kw in node.keywords:
            if kw.value is not None:
                target.add(id(kw.value))

    def visit_Call(self, node: ast.Call) -> None:
        name = ""
        f = node.func
        if isinstance(f, ast.Name):
            name = f.id
        elif isinstance(f, ast.Attribute):
            name = f.attr
        elif isinstance(f, ast.Subscript):
            inner = f.value
            if isinstance(inner, ast.Name):
                name = inner.id
            elif isinstance(inner, ast.Attribute):
                name = inner.attr

        if name in _T_NAMES:
            self._mark(node, self.translated)
        if name in _LOG_NAMES:
            self._mark(node, self.internal)
        if isinstance(f, ast.Name) and (f.id.endswith("Error") or f.id.endswith("Exception")):
            self._mark(node, self.internal)

        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None:
            for sub in ast.walk(node.exc):
                self.internal.add(id(sub))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str):
            return
        text = node.value
        if not text or not _CJK_RE.search(text):
            return
        if id(node) in self.translated or id(node) in self.internal or id(node) in self.docstrings:
            return
        stripped = text.strip()
        if len(stripped) < 3:
            return
        ctx = " > ".join(self._stack) or "<module>"
        self.hits.append((node.lineno, ctx, stripped.replace("\n", " ")[:80]))


def scan(path: Path) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    v = Scan()
    v.visit(tree)
    return v.hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--with-lib", action="store_true", help="连 lib/src 一起扫")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    files: list[Path] = []
    for d in _UI_DIRS:
        files.extend((root / d).rglob("*.py"))
    if args.with_lib:
        files.extend((root / "lib" / "src").rglob("*.py"))

    total = 0
    for p in sorted(set(files)):
        if any(part in _SKIP for part in p.parts):
            continue
        hits = scan(p)
        if not hits:
            continue
        total += len(hits)
        print(f"\n### {p.relative_to(root)}  ({len(hits)})")
        for line, ctx, text in hits:
            print(f"  {line:5d}  [{ctx}]  {text}")

    print(f"\n合计 {total} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
