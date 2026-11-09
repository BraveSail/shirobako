import asyncio
import shutil
from typing import Any

import pillow_heif
from pyrogram import Client
from pyrogram.handlers import ConnectHandler, DisconnectHandler
from pyrogram.types import BotCommand

from core import bs, heartbeat_task, on_connect, on_disconnect, ws
from db.engine import close_db
from db.init import init_db
from i18n import ISO639_MAP
from log import enable_library_diagnostics, logger, setup_logging
from plugins.helpers import COMMANDS
from utils.event_loop import setup_optimized_event_loop

pillow_heif.register_heif_opener()

setup_logging(debug=bs.debug)
if bs.debug:
    logger.enable("parsehub")
# 即使不开 debug 也要放开下载器的诊断日志 (库默认把自己静默, 见 log.py 的说明)
enable_library_diagnostics()

setup_optimized_event_loop()
loop = asyncio.new_event_loop()

#: 并发处理 update 的槽位数 (见 Client 构造处的说明)
BOT_WORKERS = 32


class Bot(Client):
    def __init__(self) -> None:
        self.cfg = bs

        super().__init__(
            self.cfg.bot_session_name,
            api_id=self.cfg.api_id,
            api_hash=self.cfg.api_hash,
            bot_token=self.cfg.bot_token,
            plugins={"root": "plugins"},
            proxy=self.cfg.bot_proxy,
            loop=loop,
            workdir=self.cfg.sessions_path,
            # 并发处理 update 的槽位数。默认是 min(32, cpu+4) —— 4 核机器只有 8,
            # 而一个解析任务会**占满一个槽位直到结束** (大视频下载可能几分钟到几十分钟),
            # 连发几条就会把所有槽位占满, 后面的请求只能静默排队。
            # 这些任务是 IO 密集 (网络下载/上传), 槽位开大不耗 CPU。
            workers=BOT_WORKERS,
        )

    async def start(self, *args: Any, **kwargs: Any) -> "Bot":
        self.init_watchdog()

        logger.info("初始化数据库...")
        await init_db()
        logger.success("数据库初始化完成")

        # 缓存换 Redis 后 TTL 由 redis 自己管, 不再需要进程内的定期清理任务
        await super().start(*args, **kwargs)

        # ★ 主动探活：``AuthKeyNotFound`` 会让 pyrogram 的 recv_task 静默死掉，
        # 且**不触发** disconnect 回调 —— 光靠回调会一直挂着（2026-11-07 挂了
        # 40 小时）。这里起一个后台任务定期 GetState，死了就按类型处置。
        asyncio.create_task(heartbeat_task(self))

        await self.set_menu()
        return self

    async def stop(self, *args: Any, **kwargs: Any) -> Client:
        ws.exit_flag = True
        await super().stop(*args, **kwargs)
        await close_db()
        # 结束时清理下载残留
        if self.cfg.download_dir.exists() and not self.cfg.debug_skip_cleanup:
            shutil.rmtree(self.cfg.download_dir)
        return self

    def init_watchdog(self) -> None:
        self.add_handler(ConnectHandler(on_connect))
        self.add_handler(DisconnectHandler(on_disconnect))

    async def set_menu(self) -> None:
        commands = await self.get_bot_commands()
        if len(commands) == len(COMMANDS) and all(c.description in str(COMMANDS.values()) for c in commands):
            logger.debug("菜单无变化, 跳过设置")
            return

        for iso639, bcp47 in ISO639_MAP.items():
            tc = {k: v[bcp47] for k, v in COMMANDS.items()}
            await self.set_bot_commands(
                [BotCommand(command=k, description=v) for k, v in tc.items()],
                language_code=iso639,
            )
            logger.debug(f"{iso639 or '默认'} 菜单已设置: {tc}")
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    bot = Bot()
    bot.run()
