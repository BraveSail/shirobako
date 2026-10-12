"""管理命令: ``/add`` ``/list`` ``/purge`` ``/del`` ``/promote`` ``/demote``。

**两级权限**（2026-10-12）：

- ``/add`` —— 加**白名单普通用户**（写 ``users.role='user'``）。
- ``/promote`` / ``/demote`` —— 提升/降级**管理员**（``role='admin'``），
  **仅超管（``.env`` 的 ``ADMIN_USERS``）可用**。
- 管理命令（``/purge`` ``/list`` ``/del``）—— **管理员及以上**可用
  （白名单普通用户不是管理员）。

**调用鉴权**（2026-10-12 权限模型）：群内自动匹配不鉴权；非同群调用（私聊 /
inline / guest）只查本地库白名单（``users.role`` 非空 ∪ 配置超管），见
``plugins/parse/handlers.py`` / ``inline.py`` / ``guest.py`` 的入口判定。
"""

from pyrogram import Client, filters
from pyrogram.types import Message

from core import bs
from db import get_session
from i18n import t_
from log import logger
from plugins.context import get_config_target
from plugins.parse.sender import MessageSender
from repo.settings import SettingsConfig
from services import (
    AdminUserService,
    ParseService,
    SettingsService,
    UserService,
    is_admin_user,
    is_super_admin,
    parse_cache,
    persistent_cache,
)

logger = logger.bind(name="Admin")


async def _context(msg: Message) -> tuple[str, SettingsConfig]:
    """取 (语言, 配置) —— 与其他命令同一路径 (顺带完成用户语言的首次初始化)。"""
    async with get_session() as session:
        lang = bs.language
        if msg.from_user:
            lang = await UserService(session).ensure_lang(msg.from_user.id, msg.from_user.language_code)
        config = await SettingsService(session).get_config(get_config_target(msg))
    return lang, config


async def _require_admin(cli: Client, msg: Message) -> bool:
    """**管理员**校验（配置超管 ∪ DB role=admin）: 不是就回一条提示（不静默）。"""
    uid = msg.from_user.id if msg.from_user else None
    if await is_admin_user(uid):
        return True
    lang, config = await _context(msg)
    await MessageSender(cli, msg, config).text(t_[lang]("无权使用该命令"))
    logger.warning(f"管理命令被非管理员调用: user_id={uid} cmd={msg.command}")
    return False


async def _require_super_admin(cli: Client, msg: Message) -> bool:
    """**超管**校验（**只认配置层**）—— ``/promote`` ``/demote`` 专用。"""
    uid = msg.from_user.id if msg.from_user else None
    if await is_super_admin(uid):
        return True
    lang, config = await _context(msg)
    await MessageSender(cli, msg, config).text(t_[lang]("该命令仅限超管使用"))
    logger.warning(f"超管命令被非超管调用: user_id={uid} cmd={msg.command}")
    return False


def _target_user_id_from_reply(reply: Message) -> int | None:
    """从被回复的消息里取"某人"的 ID。

    **优先取原转发者** —— 私聊里被回复的其实是转发过来的内容, 直接看 ``from_user``
    会加白转发的中间人 (甚至加白 bot 自己), 而不是内容真正的作者。
    """
    sender_user = getattr(reply.forward_origin, "sender_user", None)
    if sender_user:
        return sender_user.id
    if reply.from_user and not reply.from_user.is_bot:
        return reply.from_user.id
    return None


async def _resolve_target(cli: Client, msg: Message) -> int | None:
    """``/add`` ``/del`` ``/promote`` ``/demote`` 取谁: 命令参数优先, 否则看被回复的消息。"""
    if msg.command and len(msg.command) > 1:
        arg = msg.command[1].strip()
        if arg.lstrip("-").isdigit():
            return int(arg)
        # 非数字: 试一次用户名/链接 (用户可能写 @name 而不是数字)
        try:
            user = await cli.get_users(arg)
        except Exception as e:  # noqa: BLE001 - 解析不到就当没给, 由调用方提示
            logger.warning(f"无法解析目标: arg={arg} err={type(e).__name__}: {e}")
            return None
        return user.id if user else None
    if reply := msg.reply_to_message:
        return _target_user_id_from_reply(reply)
    return None


@Client.on_message(filters.command("add"))
async def add_admin_user(cli: Client, msg: Message) -> None:
    """``/add <uid>`` 或回复某人的消息 —— 把他加进白名单（**普通用户**）。"""
    if not await _require_admin(cli, msg):
        return

    lang, config = await _context(msg)
    _t = t_[lang]

    target = await _resolve_target(cli, msg)
    if target is None:
        await MessageSender(cli, msg, config).text(_t("请回复某人的消息，或给出用户 ID"))
        return

    async with get_session() as session:
        added = await AdminUserService(session).add(target, added_by=msg.from_user.id if msg.from_user else None)

    label = _t("已加入白名单(普通用户)") if added else _t("已在白名单中")
    await MessageSender(cli, msg, config).text(f"{label}: {target}")


@Client.on_message(filters.command("promote"))
async def promote_user(cli: Client, msg: Message) -> None:
    """``/promote <uid>`` 或回复某人 —— 把他提升为**管理员**。**仅超管可用**。"""
    if not await _require_super_admin(cli, msg):
        return

    lang, config = await _context(msg)
    _t = t_[lang]

    target = await _resolve_target(cli, msg)
    if target is None:
        await MessageSender(cli, msg, config).text(_t("请回复某人的消息，或给出用户 ID"))
        return

    async with get_session() as session:
        changed = await AdminUserService(session).promote(
            target, promoted_by=msg.from_user.id if msg.from_user else None
        )

    label = _t("已提升为管理员") if changed else _t("已是管理员")
    await MessageSender(cli, msg, config).text(f"{label}: {target}")


@Client.on_message(filters.command("demote"))
async def demote_user(cli: Client, msg: Message) -> None:
    """``/demote <uid>`` 或回复某人 —— 把他从管理员降回**普通用户**。**仅超管可用**。"""
    if not await _require_super_admin(cli, msg):
        return

    lang, config = await _context(msg)
    _t = t_[lang]

    target = await _resolve_target(cli, msg)
    if target is None:
        await MessageSender(cli, msg, config).text(_t("请回复某人的消息，或给出用户 ID"))
        return

    # 配置超管降不了 (它在 .env 里) —— 必须说清, 不能谎称"已降级"
    if AdminUserService.is_configured(target):
        await MessageSender(cli, msg, config).text(
            _t(f"{target} 是配置里的超管 (ADMIN_USERS)，需改 .env 才能移除")
        )
        return

    async with get_session() as session:
        changed = await AdminUserService(session).demote(target)

    label = _t("已降为普通用户") if changed else _t("不是管理员")
    await MessageSender(cli, msg, config).text(f"{label}: {target}")


@Client.on_message(filters.command("list"))
async def list_admin_users(cli: Client, msg: Message) -> None:
    """``/list`` —— 列出名单（超管 / 管理员 / 普通用户三段）。"""
    if not await _require_admin(cli, msg):
        return

    lang, config = await _context(msg)
    _t = t_[lang]

    async with get_session() as session:
        entries = await AdminUserService(session).list_roles()

    if not entries:
        await MessageSender(cli, msg, config).text(_t("白名单为空"))
        return

    groups: dict[str, list[int]] = {"super": [], "admin": [], "user": []}
    for uid, role in entries:
        groups.setdefault(role, []).append(uid)

    # ⚠️ 段标题必须走**字面量调用点**（变量传参会让 easy_ai18n 记 parse error）
    labels = {"super": _t("超管"), "admin": _t("管理员"), "user": _t("普通用户")}
    lines: list[str] = []
    for role in ("super", "admin", "user"):
        if ids := groups.get(role):
            lines.append(f"{labels[role]} ({len(ids)}):\n" + "\n".join(f"• {uid}" for uid in ids))
    await MessageSender(cli, msg, config).text("\n\n".join(lines))


@Client.on_message(filters.command("purge"))
async def purge_cache(cli: Client, msg: Message) -> None:
    """``/purge <链接>`` 或回复一条含链接的消息 —— 清掉该链接的解析缓存。

    两层都要清: ``persistent_cache`` (解析字段 + 媒体 file_id, 7 天) 与
    ``parse_cache`` (解析结果, 5 分钟)。**两层现在都在 Redis** (key 前缀
    ``shirobako:parse:*`` / ``shirobako:result:*``) —— 文案里的层名按介质写,
    别再叫"数据库/内存" (那是迁移到 Redis 之前的命名, 会让人以为没清干净)。
    **key 必须用 ``get_raw_url`` 的结果** —— 写入时用的就是它, 拿用户原始输入去清会
    hash 对不上、清不掉。
    """
    if not await _require_admin(cli, msg):
        return

    lang, config = await _context(msg)
    _t = t_[lang]

    args = msg.command[1:] if msg.command and msg.command[1:] else []

    # ``/purge all``: 清空全部。**清掉之后所有人的下一次解析都要重新下载+上传媒体**,
    # 所以只有这一种"批量"形式 (不做 ``/purge 平台`` 之类的中间档 —— 那只会更难解释代价)。
    if len(args) == 1 and args[0].strip().lower() == "all":
        persistent = await persistent_cache.clear()
        result = await parse_cache.clear()
        logger.warning(
            f"/purge all: 清空全部缓存 persistent={persistent} result={result} "
            f"by={msg.from_user.id if msg.from_user else None}"
        )
        await MessageSender(cli, msg, config).text(
            # f-string + `_t` 是本项目的写法: `_t` 从**调用点源码**取模板做词条 key
            # (它的 AST 会把 `{persistent}` 当占位符), 所以变量名就是占位符名。
            _t(f"已清空全部缓存（Redis）: 持久层 {persistent} 条 · 结果层 {result} 条")
        )
        return

    text = " ".join(args)
    if not text and msg.reply_to_message:
        text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    if not text:
        await MessageSender(cli, msg, config).text(_t("请加上链接或回复一条消息"))
        return

    urls = list(dict.fromkeys(i for i in text.split() if ParseService().parser.get_platform(i)))[:10]
    if not urls:
        await MessageSender(cli, msg, config).text(_t("不支持的平台"))
        return

    lines: list[str] = []
    for url in urls:
        try:
            raw_url = await ParseService().get_raw_url(url)
        except Exception as e:  # noqa: BLE001 - 单条失败不该让整批中断
            logger.warning(f"清缓存取原始链接失败: url={url} err={type(e).__name__}: {e}")
            lines.append(f"⚠️ {url}\n{type(e).__name__}: {e}")
            continue
        # remove() 不返回是否命中, 所以先看有没有, 才能回答"清了"还是"本来就没有"
        had = bool(await persistent_cache.get(raw_url)) or bool(await parse_cache.get(raw_url))
        await persistent_cache.remove(raw_url)
        await parse_cache.pop(raw_url)
        logger.info(f"清缓存: url={raw_url} had_cache={had} by={msg.from_user.id if msg.from_user else None}")
        lines.append(f"{_t('已清除缓存') if had else _t('没有缓存')}\n{raw_url}")

    await MessageSender(cli, msg, config).text("\n\n".join(lines))


@Client.on_message(filters.command("del"))
async def remove_admin_user(cli: Client, msg: Message) -> None:
    """``/del <uid>`` 或回复某人的消息 —— 把他移出名单（不论角色）。"""
    if not await _require_admin(cli, msg):
        return

    lang, config = await _context(msg)
    _t = t_[lang]

    target = await _resolve_target(cli, msg)
    if target is None:
        await MessageSender(cli, msg, config).text(_t("请回复某人的消息，或给出用户 ID"))
        return

    # 配置那层删不掉 (它在 .env 里) —— 必须说清, 不能谎称"已移除"而其实还在白名单
    if AdminUserService.is_configured(target):
        await MessageSender(cli, msg, config).text(
            _t(f"{target} 在配置 (ADMIN_USERS) 里，需改 .env 才能移除")
        )
        return

    async with get_session() as session:
        removed = await AdminUserService(session).remove(target)

    label = _t("已移出白名单") if removed else _t("不在白名单中")
    await MessageSender(cli, msg, config).text(f"{label}: {target}")
