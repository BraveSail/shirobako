from pyrogram import Client, filters
from pyrogram.types import Message

from i18n import t_
from plugins.helpers import format_label
from services import UserService


@Client.on_message(filters.command("lang"))
async def show_lang(_: Client, msg: Message) -> None:
    """显示当前生效的语言。

    **语言完全跟随 Telegram 客户端**（用户指令：删掉入库、每次按 TG 语言适配），
    所以这里只是把「这次算出来的是什么」告诉用户，没有任何可写的状态。
    想换语言就改 Telegram 自己的界面语言，下一条消息立刻生效。
    """
    if not msg.from_user:
        return

    lang = UserService.lang_for(msg.from_user.language_code)
    await msg.reply_text(
        format_label(t_[lang]("语言跟随 Telegram 客户端，在 Telegram 设置里修改后，下一条消息即生效。"))
    )
