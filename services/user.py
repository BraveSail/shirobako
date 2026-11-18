from sqlalchemy.ext.asyncio import AsyncSession

from core import bs
from db.models.user import User
from i18n import ISO639_MAP
from repo.user import UserRepo


def locale_for(language_code: str | None, default: str | None = None) -> str:
    """把 Telegram 上报的语言码归一成项目 locale。

    Telegram 的形态**不统一**：同一套代码里实测过 ``ja``（短码）与 ``en-us``
    （BCP-47 全码）。所以这里做**两级查找**：

    1. 整串小写后查 ``ISO639_MAP``（``en-us`` / ``zh-hant`` 走这层）；
    2. 查不到就取主语言（``en-GB`` 之类未登记的地区变体 → ``en``）。

    都没有才退回 ``default``（bot 全局语言）。

    ⚠️ **别再只查一层**：只查整串时 ``en-us`` 会掉进 fallback（简中），
    症状就是「TG 是英语但 i18n 是中文」，而日语用户因为报的是短码恰好正常
    —— 这种不对称极具误导性（当年就是这么漏掉的）。
    """
    fallback = default or bs.language
    if not language_code:
        return fallback
    normalized = language_code.strip().lower().replace("_", "-")
    if not normalized:
        return fallback
    if (locale := ISO639_MAP.get(normalized)) is not None:
        return locale
    # 二级：去掉地区/书写系统，只留主语言（``en-gb`` → ``en``、``sr-latn-rs`` → ``sr``）
    primary = normalized.split("-", 1)[0]
    if primary and (locale := ISO639_MAP.get(primary)) is not None:
        return locale
    return fallback


class UserNotFoundError(Exception):
    pass


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.user = UserRepo(session)

    async def add(self, telegram_user_id: int) -> User:
        return await self.user.add(telegram_user_id)

    async def get(self, telegram_user_id: int) -> User | None:
        return await self.user.get_by_tg_user_id(telegram_user_id)

    async def get_or_raise(self, telegram_user_id: int) -> User:
        if not (user := await self.get(telegram_user_id)):
            raise UserNotFoundError(f"在数据库中找不到用户: {telegram_user_id}")
        return user

    async def ensure(self, telegram_user_id: int) -> User:
        if not (user := await self.get(telegram_user_id)):
            return await self.add(telegram_user_id)
        return user

    @staticmethod
    def lang_for(telegram_language_code: str | None) -> str:
        """按 Telegram 上报的语言码算出界面语言。**纯函数，不碰数据库。**

        用户指令（2026-11-18）：语言每次按 Telegram 语言适配，不入库 —— 没有手动选择，
        也就没有要持久化的"用户偏好"。这样 Telegram 里改了语言，下一条消息立刻生效，
        不存在「缓存了旧语言」这种状态。

        拿不到语言码时用 bot 全局语言（``bs.language``）—— 这是**唯一**的兜底，
        因为它不是"用户偏好"，而是"这次拿不到任何线索"。
        """
        return locale_for(telegram_language_code)

    async def ensure_lang(self, telegram_user_id: int, telegram_language_code: str | None = None) -> str:
        """取用户语言 —— **纯计算，不读也不写库**。

        参数 ``telegram_user_id`` 保留只是为了不改 15 个调用点（它们手里都有这个值）。
        以后清账时可以去掉，但那是纯机械重构，不必和本改动混在一起。
        """
        del telegram_user_id  # 未使用；语言不再与用户记录绑定
        return self.lang_for(telegram_language_code)

    async def get_lang(self, telegram_user_id: int) -> str:
        """兼容旧调用点：没有持久化的语言可查，直接给 bot 全局语言。"""
        del telegram_user_id
        return bs.language
