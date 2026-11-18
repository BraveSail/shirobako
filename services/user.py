from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core import bs
from db.models.user import LANG_SOURCE_MANUAL, LANG_SOURCE_TG, User
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

    async def ensure_lang(self, telegram_user_id: int, telegram_language_code: str | None = None) -> str:
        """取用户语言; 用户**首次出现**时按其 Telegram 语言初始化。

        以前只有"建表默认值 + 手动 /lang"两个来源, Telegram 上报的语言从没被用上,
        于是所有新用户一律是 bot 全局语言 (简中) —— 表现为"日语/繁体用户收到的还是简中"。

        **已有用户**：
        - ``lang_source == manual``（用户自己 /lang 选过）→ 原样返回，**绝不覆盖**；
        - ``lang_source == tg``（当初自动归一的）→ 拿本次 Telegram 上报的值重新归一一遍，
          变了就更新。这一条是为了修掉「只认短码不认 BCP-47」时被误存成简中的那批人：
          他们下次发消息就自动纠正，不需要手动 /lang。

        拿不到 Telegram 语言（``None``/空）时不动库里的值 —— 不能因为一次缺失就退回默认。
        """
        locale_hint = locale_for(telegram_language_code, default="") if telegram_language_code else ""

        if user := await self.get(telegram_user_id):
            if user.lang_source == LANG_SOURCE_MANUAL or not locale_hint:
                return user.language_code
            if locale_hint != user.language_code:
                user.language_code = locale_hint
                logger.info(
                    f"用户语言按 Telegram 重新同步: tg={telegram_user_id} "
                    f"{user.language_code!r} <- {telegram_language_code!r}"
                )
            return user.language_code

        locale = locale_for(telegram_language_code)
        await self.set_lang(telegram_user_id, locale, source=LANG_SOURCE_TG)
        return locale

    async def get_lang(self, telegram_user_id: int) -> str:
        if user := await self.get(telegram_user_id):
            return user.language_code
        return bs.language

    async def set_lang(
        self,
        telegram_user_id: int,
        language_code: str,
        *,
        source: str = LANG_SOURCE_MANUAL,
    ) -> User:
        """设置语言。

        ``source`` 默认 ``manual`` —— 唯一的手动入口是 ``/lang``，默认值取更保守的那个，
        免得哪天新加一个调用点忘了传就把它当成「可自动覆盖」。
        """
        user = await self.ensure(telegram_user_id)
        user.language_code = language_code
        user.lang_source = source
        return user
