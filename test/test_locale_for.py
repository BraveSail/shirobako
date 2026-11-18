"""Telegram 语言码归一：短码与 BCP-47 全码都要认。

## 真实故障（2026-11-16，用户报「TG 是英语为什么 i18n 是中文」）

群里有英语用户，bot 给他发的是简体中文；而**日语用户一切正常**。

`locale_for` 当时只做一层查找：

```python
return ISO639_MAP.get(language_code.strip().lower(), fallback)
```

而 `ISO639_MAP` 只登记了 ISO 639-1 **短码**（`en` / `ja`），没有 BCP-47 全码。
Telegram 上报的形态**因客户端/版本而异**，实测同一套代码里：

| 上报值 | 当时结果 | 应该 |
| --- | --- | --- |
| `ja` | `ja-jp` ✓ | ✓ |
| `en` | `en-us` ✓ | ✓ |
| **`en-us`** | **`zh-hans`** ✗ | `en-us` |
| **`ja-jp`** | **`zh-hans`** ✗ | `ja-jp` |

「日语正常、英语变中文」的不对称正是线索：日语用户报的是短码 `ja`，
英语用户报的是全码 `en-us`。

## 契约

1. 整串小写查表（`en-us` / `zh-hant` 走这层）；
2. 查不到退到主语言（`en-GB` → `en`）；
3. 都没有才回退 `default`；
4. **繁简绝不互相回退**：`zh-hant` / `zh-tw` / `zh-hk` / `zh-mo` 都是繁体，
   其余中文变体是简体。
"""

from __future__ import annotations

import pytest

from i18n import ISO639_MAP
from services.user import locale_for


class TestShortCodes:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("zh", "zh-hans"),
            ("ja", "ja-jp"),
            ("en", "en-us"),
            ("ko", "ko-kr"),
            ("de", "de-de"),
        ],
    )
    def test_iso639_1(self, code: str, expected: str) -> None:
        assert locale_for(code) == expected


class TestBcp47:
    """★ 核心回归：全码必须能被认出来，否则退回简中是错的。"""

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("en-us", "en-us"),
            ("en-US", "en-us"),
            ("ja-jp", "ja-jp"),
            ("ko-kr", "ko-kr"),
            ("de-de", "de-de"),
            ("ru-ru", "ru-ru"),
            ("es-es", "es-es"),
        ],
    )
    def test_full_codes(self, code: str, expected: str) -> None:
        assert locale_for(code) == expected

    def test_the_actual_bug(self) -> None:
        """用户报的那次：``en-us`` 曾经返回 ``zh-hans``。"""
        assert locale_for("en-us") == "en-us"
        assert locale_for("en-us") != "zh-hans"


class TestTraditionalChinese:
    """繁简是两种语言，绝不互相回退。"""

    @pytest.mark.parametrize("code", ["zh-hant", "zh-tw", "zh-hk", "zh-mo"])
    def test_traditional(self, code: str) -> None:
        assert locale_for(code) == "zh-hant"

    @pytest.mark.parametrize("code", ["zh", "zh-hans", "zh-cn", "zh-sg"])
    def test_simplified(self, code: str) -> None:
        assert locale_for(code) == "zh-hans"

    def test_case_insensitive(self) -> None:
        assert locale_for("zh-Hant") == "zh-hant"


class TestFallbacks:
    def test_unlisted_region_falls_back_to_primary(self) -> None:
        """未登记的地区变体退到主语言，别直接掉进 bot 默认语言。"""
        assert locale_for("en-gb") == "en-us"
        assert locale_for("en-au") == "en-us"
        assert locale_for("fr-ca") == "fr-fr"

    def test_underscore_is_normalized(self) -> None:
        """有些客户端用下划线（``en_US``）。"""
        assert locale_for("en_US") == "en-us"
        assert locale_for("zh_Hant") == "zh-hant"

    def test_unknown_language_uses_default(self) -> None:
        assert locale_for("xx-yy") == "zh-hans"
        assert locale_for("xx-yy", default="ja-jp") == "ja-jp"

    def test_empty_uses_default(self) -> None:
        assert locale_for("") == "zh-hans"
        assert locale_for(None) == "zh-hans"
        assert locale_for("   ") == "zh-hans"

    def test_explicit_default(self) -> None:
        assert locale_for(None, default="en-us") == "en-us"


class TestMapIntegrity:
    def test_every_value_is_a_known_locale(self) -> None:
        from i18n import LANG_MAP

        for code, locale in ISO639_MAP.items():
            assert locale in LANG_MAP, f"{code!r} 映射到未知 locale {locale!r}"

    def test_all_locales_reachable(self) -> None:
        """每个界面语言都应至少有一个语言码能映射到它。"""
        from i18n import LANG_MAP

        reachable = set(ISO639_MAP.values())
        missing = set(LANG_MAP) - reachable - {""}
        assert not missing, f"这些 locale 没有任何语言码能映射到: {sorted(missing)}"
