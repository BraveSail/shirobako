"""用户语言：**每次按 Telegram 上报的语言码实时计算**，不入库。

## 演进（别把旧的「只在首次设置」当契约）

1. 最初：只有「建表默认值 + 手动 /lang」两个来源，Telegram 的 language_code 从没被用上
   —— 日语/繁体用户收到的仍是简中。
2. 后来：首次出现时按 Telegram 语言写库。但``locale_for`` 只认 ISO 639-1 短码，
   Telegram 报 BCP-47 全码（``en-us``）时查不到 → 退回简中并存死，之后改客户端语言也不生效。
3. **现在**（用户指令「删掉入库，每次根据用户的 tg 语言适配」）：完全不持久化。
   ``ensure_lang`` 是纯计算，Telegram 里改语言，下一条消息立刻用新语言 ——
   「陈旧缓存」在结构上不可能发生。手动 ``/lang`` 也删了（没有要保存的偏好）。

所以：**别再**断言「已有用户不会被覆盖」或「新用户会把语言写进 created.language_code」，
那两条都是已被废弃的中间形态。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core import bs
from services.user import UserService, locale_for

# ── 映射 ────────────────────────────────────────────────────────────────


def test_maps_telegram_short_codes():
    assert locale_for("ja") == "ja-jp"
    assert locale_for("en") == "en-us"
    assert locale_for("ko") == "ko-kr"
    assert locale_for("zh") == "zh-hans"


def test_maps_telegram_bcp47_codes():
    """★ 回归：Telegram 在某些客户端报全码，早期只认短码会退回简中。"""
    assert locale_for("en-us") == "en-us"
    assert locale_for("ja-jp") == "ja-jp"
    assert locale_for("ko-kr") == "ko-kr"


def test_traditional_chinese_is_its_own_locale():
    """Telegram 报 zh-hant —— 它跟 zh 是两种语言, 不能套用简中"""
    assert locale_for("zh-hant") == "zh-hant"
    assert locale_for("zh-Hant") == "zh-hant"  # 大小写不定
    assert locale_for("zh-hans") == "zh-hans"
    assert locale_for("zh-tw") == "zh-hant"
    assert locale_for("zh-cn") == "zh-hans"


def test_case_and_whitespace_are_normalised():
    assert locale_for("  JA  ") == "ja-jp"
    assert locale_for("ZH-hant") == "zh-hant"
    assert locale_for("en_US") == "en-us"


def test_unknown_or_missing_falls_back_to_bot_language():
    assert locale_for(None) == bs.language
    assert locale_for("") == bs.language
    assert locale_for("xx") == bs.language


def test_explicit_default_is_honoured():
    assert locale_for(None, "en-us") == "en-us"
    assert locale_for("xx", "ja-jp") == "ja-jp"


# ── ensure_lang：纯计算 ──────────────────────────────────────────────────


def _service(existing=None) -> tuple[UserService, SimpleNamespace]:
    """造一个带 repo 的 UserService，用来断言「它根本没被碰过」。"""
    created = SimpleNamespace(language_code=None)
    repo = SimpleNamespace(
        get_by_tg_user_id=AsyncMock(return_value=existing),
        add=AsyncMock(return_value=created),
    )
    service = UserService(session=SimpleNamespace())
    service.user = repo
    return service, created


def test_lang_for_is_pure():
    """``lang_for`` 是静态纯函数，不依赖任何会话/用户记录。"""
    assert UserService.lang_for("en-us") == "en-us"
    assert UserService.lang_for("ja") == "ja-jp"
    assert UserService.lang_for(None) == bs.language


def test_ensure_lang_follows_telegram_every_time():
    """★ 核心：同一个用户，Telegram 语言变了，结果立刻跟着变。"""
    service, _ = _service()
    assert asyncio.run(service.ensure_lang(123, "zh-hans")) == "zh-hans"
    assert asyncio.run(service.ensure_lang(123, "en-us")) == "en-us"
    assert asyncio.run(service.ensure_lang(123, "ja-jp")) == "ja-jp"


def test_ensure_lang_never_touches_the_database():
    """语言不再入库 —— repo 的读写一个都不该被调用。"""
    service, _ = _service()
    asyncio.run(service.ensure_lang(123, "en-us"))
    service.user.get_by_tg_user_id.assert_not_awaited()
    service.user.add.assert_not_awaited()


def test_existing_user_record_is_irrelevant():
    """库里存着什么都无所谓 —— 结果只由本次 Telegram 语言决定。"""
    stale = SimpleNamespace(language_code="zh-hant")
    service, _ = _service(existing=stale)
    assert asyncio.run(service.ensure_lang(123, "en-us")) == "en-us"
    assert stale.language_code == "zh-hant", "不应该改用户记录"


def test_missing_language_uses_bot_default():
    service, _ = _service()
    assert asyncio.run(service.ensure_lang(123, None)) == bs.language
