"""基线对照测试：新渲染实现的输出必须与旧实现**逐块相同**。

## 这份基线是什么

`test/golden/*.json` 是用**旧实现**（`build_rich_markdown` + `markdown_to_blocks`）
对真实链接跑出来的产物，包含：

- `markdown`：旧字符串通道的中间产物
- `blocks`：旧最终形态（块类型 + 文本 + 嵌套，拍平成 JSON）

采集脚本见 `doc/format-adaptations.md` 末尾。

## 怎么用

改渲染层之后跑这个测试：

    pytest test/test_golden_parity.py -q

任何 diff 都要能解释：
- **是 bug** → 修新实现
- **是故意改进**（旧行为本身错了）→ 在 `KNOWN_DIFFS` 里登记理由，并把基线也更新

没有第三种情况。"看起来差不多"不算。

## 为什么必须用它

上一轮重构失败 3 次，每次都是「测试全绿、真机全坏」。原因很简单：那些测试是
照着**新实现**写的，验证的是"我的实现和我以为的一致"，不是"和旧行为一致"。
这份基线是唯一独立于新实现的判据。
"""

from __future__ import annotations

import json
import pathlib

import pytest

GOLDEN = pathlib.Path(__file__).parent / "golden"

#: 已知的、**故意**与旧实现不同的地方。每条必须写明理由。
#: 格式：``{"case名": "理由"}``
KNOWN_DIFFS: dict[str, str] = {}


def _cases() -> list[str]:
    if not GOLDEN.exists():
        return []
    return sorted(p.stem for p in GOLDEN.glob("*.json") if p.name != "manifest.json")


def _load(case: str) -> dict:
    return json.loads((GOLDEN / f"{case}.json").read_text())


def _flatten(blocks) -> list[dict]:
    """把渲染出的块拍平成与基线同构的 JSON（不依赖具体类名以外的东西）。"""

    def one(b) -> dict:
        item = {"type": type(b).__name__}
        text = getattr(b, "text", None)
        if isinstance(text, str):
            item["text"] = text
        elif text is not None:
            item["text"] = _rich_text(text)
        for attr in ("blocks", "items"):
            child = getattr(b, attr, None)
            if isinstance(child, list):
                item[attr] = [one(c) for c in child]
        if hasattr(b, "summary"):
            item["summary"] = _rich_text(getattr(b, "summary", ""))
        if hasattr(b, "size"):
            item["size"] = getattr(b, "size", None)
        return item

    return [one(b) for b in blocks or []]


def _rich_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_rich_text(v) for v in value)
    inner = getattr(value, "text", None)
    if inner is not None:
        return _rich_text(inner)
    return ""


def _diff(golden: list[dict], actual: list[dict]) -> list[str]:
    """逐块比对，返回人类可读的差异列表（空 = 完全一致）。"""
    out: list[str] = []
    if len(golden) != len(actual):
        out.append(f"块数不同: 旧 {len(golden)} / 新 {len(actual)}")
    for i, (g, a) in enumerate(zip(golden, actual)):
        if g == a:
            continue
        if g.get("type") != a.get("type"):
            out.append(f"[{i}] 类型 {g.get('type')} → {a.get('type')}")
        if g.get("text") != a.get("text"):
            out.append(
                f"[{i}] 文本不同:\n      旧: {g.get('text')!r}\n      新: {a.get('text')!r}"
            )
        if g.get("size") != a.get("size"):
            out.append(f"[{i}] size {g.get('size')} → {a.get('size')}")
        gb, ab = g.get("blocks"), a.get("blocks")
        if (gb is None) != (ab is None):
            out.append(f"[{i}] 嵌套块有无不同")
        elif gb is not None:
            out.extend(f"[{i}].{d}" for d in _diff(gb, ab))
    return out


@pytest.mark.parametrize("case", _cases())
def test_rendering_matches_the_golden_baseline(case: str):
    """新渲染路径必须复现旧实现在这个平台上产出的每一个块。"""
    from parsehub import ParseHub

    golden = _load(case)
    url = golden["url"]

    # 解析是网络调用——基线里存了结果字段，但 parse 需要真实请求。
    # 这里只验证「同一份输入走新渲染」，所以用 golden 的 markdown 作为输入，
    # 直接测转换 + 渲染这一层（不依赖网络，CI 可跑）。
    from parsehub.types.md_segments import markdown_to_segments
    from plugins.parse.render_blocks import render_segments

    segments = markdown_to_segments(golden["markdown"])
    blocks = render_segments(segments)
    actual = _flatten(blocks)

    diffs = _diff(golden["blocks"], actual)
    if case in KNOWN_DIFFS:
        pytest.skip(f"已知差异（{KNOWN_DIFFS[case]}），基线待更新")
    assert not diffs, (
        f"\n{case} 与基线不一致，共 {len(diffs)} 处：\n" + "\n".join(diffs[:15])
    )
