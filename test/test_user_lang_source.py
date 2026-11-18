"""``users.lang_source``：区分「Telegram 自动归一」与「用户手动 /lang」。

## 为什么需要这一列

早期 ``locale_for`` 只认 ISO 639-1 短码，Telegram 报 ``en-us`` 的英语用户被归一成
``zh-hans``（查不到 → 退回 bot 全局语言）。``ensure_lang`` 对已有用户不重读 TG 语言
（为的是保住手动选择），于是这批人永远显示简中，改映射也救不回来。

加 ``lang_source`` 后：

- ``tg``（自动归一）→ 每次都能按 Telegram 当前语言重新同步；
- ``manual``（``/lang`` 选过）→ **任何自动同步都不许覆盖**。

存量行一律当 ``tg``（迁移里 ``UPDATE users SET lang_source='tg'``）——
这是用户明确要的「加个 source 批量修」，代价是手动选过但 TG 语言不同的会被拉回一次。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.base import Base
from db.models.user import LANG_SOURCE_MANUAL, LANG_SOURCE_TG, User
from services.user import UserService


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class TestDefaults:
    async def test_new_user_defaults_to_tg_source(self, session: AsyncSession) -> None:
        svc = UserService(session)
        await svc.ensure_lang(1001, "en-us")
        user = await svc.get(1001)
        assert user is not None
        assert user.lang_source == LANG_SOURCE_TG
        assert user.language_code == "en-us"


class TestAutoSync:
    async def test_tg_source_gets_resynced(self, session: AsyncSession) -> None:
        """★ 核心：自动归一的行要能跟着 Telegram 语言变。"""
        svc = UserService(session)
        user = await svc.add(2001)
        user.language_code = "zh-hans"  # 当年误存的错值
        user.lang_source = LANG_SOURCE_TG
        await session.flush()

        got = await svc.ensure_lang(2001, "en-us")
        assert got == "en-us", "自动来源的行必须能重新同步"
        await session.refresh(user)
        assert user.language_code == "en-us"

    async def test_no_change_when_already_right(self, session: AsyncSession) -> None:
        svc = UserService(session)
        await svc.ensure_lang(2002, "ja")
        assert await svc.ensure_lang(2002, "ja-jp") == "ja-jp"

    async def test_missing_tg_language_keeps_value(self, session: AsyncSession) -> None:
        """拿不到 TG 语言时不能退回默认 —— 那会把正确值冲掉。"""
        svc = UserService(session)
        await svc.ensure_lang(2003, "ja")
        assert await svc.ensure_lang(2003, None) == "ja-jp"
        assert await svc.ensure_lang(2003, "") == "ja-jp"


class TestManualProtection:
    async def test_manual_is_never_overwritten(self, session: AsyncSession) -> None:
        """★★ 手动选过的，即使 TG 说英语也不许改。"""
        svc = UserService(session)
        await svc.set_lang(3001, "ja-jp")  # 默认 source=manual
        user = await svc.get(3001)
        assert user is not None and user.lang_source == LANG_SOURCE_MANUAL

        got = await svc.ensure_lang(3001, "en-us")
        assert got == "ja-jp", "手动选择被自动同步覆盖了"
        await session.refresh(user)
        assert user.language_code == "ja-jp"

    async def test_set_lang_defaults_to_manual(self, session: AsyncSession) -> None:
        """默认值取保守的那个 —— 新调用点忘了传 source 也不会被自动覆盖。"""
        svc = UserService(session)
        await svc.set_lang(3002, "ko-kr")
        user = await svc.get(3002)
        assert user is not None
        assert user.lang_source == LANG_SOURCE_MANUAL

    async def test_explicit_tg_source(self, session: AsyncSession) -> None:
        svc = UserService(session)
        await svc.set_lang(3003, "en-us", source=LANG_SOURCE_TG)
        user = await svc.get(3003)
        assert user is not None
        assert user.lang_source == LANG_SOURCE_TG
        # 之后就可以被同步了
        assert await svc.ensure_lang(3003, "ja") == "ja-jp"
