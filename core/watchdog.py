import asyncio
import os
import re
import sys

from pyrogram import Client
from pyrogram.session import Session

from core.alert import send_alert
from core.config import bs, ws
from core.disconnect import DisconnectKind, classify, needs_session_reset, should_reconnect
from log import logger

logger = logger.bind(name="Watchdog")

#: 限流退避的默认秒数（异常里没给具体值时用它）
DEFAULT_FLOOD_WAIT = 60


def _flood_seconds(exc: BaseException | None) -> int:
    """从限流异常里取要等多久。

    pyrogram 的 ``FloodWait`` 带 ``value`` 属性（秒）。取不到就用默认值 ——
    宁可多等，也别在惩罚期内反复撞。
    """
    if exc is not None:
        for attr in ("value", "seconds", "x"):
            wait = getattr(exc, attr, None)
            if isinstance(wait, int) and wait > 0:
                return min(wait, 3600)  # 封顶 1 小时，别睡死
        # 文本里可能有 "wait of 30 seconds"
        m = re.search(r"(\d+)\s*(?:second|秒)", str(exc), re.I)
        if m:
            return min(int(m.group(1)), 3600)
    return DEFAULT_FLOOD_WAIT

#: 探活间隔（秒）。Telegram 的 recv_task 死掉后**不会**触发 disconnect 回调，
#: 光等回调会一直挂着（2026-11-07 静默 40 小时就是这么来的）。
HEARTBEAT_INTERVAL = 120

#: 连续几次探活失败才认定连接已死。给网络抖动留余地。
HEARTBEAT_MAX_FAILS = 3


async def reset_count_task() -> None:
    """重置重启次数任务"""
    if ws.restart_count:
        logger.info(f"第 {ws.restart_count} 次重启成功, 稳定运行 10 分钟后重置重启次数")
    elif ws.disconnect_count:
        logger.info("Bot 重连成功, 稳定运行 10 分钟后重置断开连接次数")

    await asyncio.sleep(600)
    ws.reset_bot_disconnect_count()
    ws.reset_bot_restart_count()
    ws.reset_bot_session_reset_count()
    logger.info("已稳定运行 10 分钟, 次数已重置")


async def on_connect(_: Client, session: Session) -> None:
    """Bot 连接成功回调函数"""

    if session.is_media:
        return

    ws.is_running = True
    logger.success("Bot 开始运行...")

    if ws.restart_count or ws.disconnect_count:
        asyncio.create_task(reset_count_task())


async def on_disconnect(cli: Client, session: Session) -> None:
    """Bot 断开连接回调函数。

    ⚠️ 这个回调**不是万能的**：``AuthKeyNotFound`` 抛出在 pyrogram 的
    ``recv_task`` 里，而 ``Session._stop()`` 在 ``await self.recv_task``
    那一步就会抛，走不到调用 ``disconnect_handler`` 的地方 —— 也就是说
    会话死亡那类**根本不会进这里**。它由 :func:`heartbeat_task` 兜底。
    """

    if session.is_media:
        return

    if ws.exit_flag:
        ws.is_running = False

    # 正常退出
    if ws.exit_flag and not ws.is_running:
        logger.info("Bot 已结束运行")
        return

    # 启动失败
    if not ws.is_running and not ws.restart_count:
        send_alert(
            "Bot 启动失败",
            "连不上 Telegram，请检查服务器网络和（如果配置了）代理。",
        )
        exit("Bot 连接失败, 请检查设备网络和代理配置")

    # 走统一处置（分类 → 决定删不删 session / 退避 / 退出）
    await handle_disconnect(cli, None, source="disconnect 回调")


async def handle_disconnect(cli: Client, exc: BaseException | None, *, source: str) -> None:
    """按**断线类型**决定怎么救 —— 不要一律重连。

    分类见 :mod:`core.disconnect`。这里的关键差别：

    - 会话死亡 → **立刻删 session 重新认证**，不等断满几次
    - 限流 → 退避等待，不重启（重启会加重惩罚）
    - 配置错 → 直接退出，重启只会掩盖问题
    """
    kind = classify(exc)
    logger.warning(f"检测到断线 [{source}] 类型={kind.value}" + (f" 原因={type(exc).__name__}: {exc}" if exc else ""))

    if needs_session_reset(kind):
        # ★ 这一类重连一万次都没用：密钥已被服务端作废。
        # 旧逻辑要「断 3 次 + 重启 3 次」才删 session，而这类错误
        # 只会有 1 次回调（甚至 0 次），永远等不到。
        ws.update_bot_session_reset_count()

        # ★ 防死循环：如果刚重登完又立刻失效，说明不是偶发作废，
        # 而是 token 被重置 / 有别的实例在抢同一个 bot。无限重登会
        # 把 Telegram 的登录频率打上去，反而更难恢复。
        if ws.session_reset_count > ws.max_session_reset_count:
            send_alert(
                "Bot 反复会话失效，已停止自动重登",
                f"短时间内已重登 {ws.session_reset_count} 次仍失效。\n"
                "可能原因: BOT_TOKEN 已在 @BotFather 重置，或有另一个实例在用同一 token。\n"
                "需要人工介入 —— 已停止自动重登，避免触发更严的风控。",
            )
            exit(f"会话反复失效（已重登 {ws.session_reset_count} 次），需人工介入")

        logger.error("会话已失效，删除 session 文件以便重新认证")
        send_alert(
            "Bot 会话已失效，正在自动重登",
            f"类型: {kind.value}\n原因: {type(exc).__name__ + ': ' + str(exc) if exc else '未知'}\n"
            f"已自动删除 session 文件并重新认证（第 {ws.session_reset_count} 次），无需人工介入。\n"
            "若反复收到这条，说明 bot token 可能被重置。",
        )
        await remove_session_file(cli)
        await restart_process("会话失效，已删 session")

    if not should_reconnect(kind):
        send_alert(
            "Bot 配置错误，已停止",
            f"类型: {kind.value}\n原因: {type(exc).__name__ if exc else '未知'}\n"
            "重启和重登都无效，需要人工检查 .env 里的 api_id / api_hash / BOT_TOKEN。",
        )
        exit(f"配置错误（{type(exc).__name__ if exc else kind.value}），重启无效，请检查配置")

    if kind is DisconnectKind.FLOOD:
        # ★ 限流要**等**，不要重启：重启会把登录/请求频率再打一遍，
        # 惩罚期只会更长。Telegram 的 FloodWait 自带秒数，照着等。
        wait = _flood_seconds(exc)
        logger.warning(f"被限流，退避 {wait} 秒后再说（不重启）")
        send_alert(
            "Bot 被 Telegram 限流",
            f"类型: {kind.value}\n退避 {wait} 秒后自动恢复，无需人工介入。\n"
            f"原因: {type(exc).__name__}: {exc}",
        )
        await asyncio.sleep(wait)
        return

    # 计数只在「真的需要重启」时累加
    if ws.restart_count >= ws.max_restart_count:
        send_alert(
            "Bot 重启次数已达上限，进程退出",
            f"已重启 {ws.restart_count} 次仍未恢复。\n类型: {kind.value}\n"
            f"最后错误: {type(exc).__name__ if exc else '无'}\n需要人工介入。",
        )
        exit(f"重启次数已达上限 ({ws.max_restart_count} 次), 结束进程")

    if ws.disconnect_count < ws.max_disconnect_count and exc is None:
        # 普通断开：先给 pyrogram 自己的重连留机会
        ws.update_bot_disconnect_count()
        logger.warning(f"Bot 已断开连接... | {ws.disconnect_count}/{ws.max_disconnect_count}")
        return

    if bs.debug:
        exit("Bot 已断开连接, 目前处于调试模式, 已跳过重启")

    await restart_process(f"断线（{kind.value}）")


async def restart_process(reason: str) -> None:
    """重启当前进程（execv，保留环境变量里的计数）。"""
    try:
        ws.update_bot_restart_count()
        logger.warning(f"重启中: {reason} | {ws.restart_count}/{ws.max_restart_count}")
        python = sys.executable
        os.execv(python, [python] + sys.argv)
    except Exception as e:
        logger.exception(e)
        send_alert(
            "Bot 重启失败，进程即将退出",
            f"原因: {reason}\n错误: {type(e).__name__}: {e}",
        )
        exit("重启失败, 结束进程, 以上为错误信息")


async def heartbeat_task(cli: Client) -> None:
    """主动探活 —— 兜住「不触发 disconnect 回调」的死亡。

    ``AuthKeyNotFound`` 会让 pyrogram 的 ``recv_task`` 静默死掉：连接对象还在、
    ``is_connected`` 可能仍是 True，但任何请求都不会再回来。只靠 disconnect
    回调会永远挂着（2026-11-07 挂了 40 小时无人知）。

    所以定期发一次最轻的 ``GetState``：连续失败到阈值就当作连接已死，
    拿最后一次的异常去分类处置（会话死亡 → 删 session 重登）。
    """
    from pyrogram import raw

    fails = 0
    last_exc: BaseException | None = None

    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)

        if ws.exit_flag:
            return

        try:
            await asyncio.wait_for(cli.invoke(raw.functions.updates.GetState()), timeout=30)
            if fails:
                logger.info(f"探活恢复正常（之前连续失败 {fails} 次）")
            fails = 0
            last_exc = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fails += 1
            last_exc = e
            logger.warning(f"探活失败 {fails}/{HEARTBEAT_MAX_FAILS}: {type(e).__name__}: {e}")

            if fails >= HEARTBEAT_MAX_FAILS:
                logger.error("连续探活失败，判定连接已死")
                send_alert(
                    "Bot 连接疑似中断",
                    f"连续 {fails} 次探活失败（GetState）。\n"
                    f"最后错误: {type(last_exc).__name__}: {last_exc}\n"
                    "正在按断线类型自动处置。",
                )
                await handle_disconnect(cli, last_exc, source="探活")
                fails = 0


async def remove_session_file(cli: Client) -> None:
    """删除会话文件"""
    logger.warning("尝试删除会话文件...")
    try:
        if cli.session is not None:
            await cli.session.stop()
        await cli.storage.close()
        if (session := cli.workdir / f"{cli.name}.session") and session.exists():
            os.remove(session)
            logger.warning(f"会话文件已移除: {session}")
    except Exception as e:
        logger.error(f"移除会话文件失败: {e}")
