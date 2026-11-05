"""群消息采集：为总结与日报积累素材。"""
import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

import config
from storage import save_message

logger = logging.getLogger("summary-bot.messages")

_MEDIA_LABELS = (
    ("photo", "[图片]"),
    ("video", "[视频]"),
    ("animation", "[GIF动图]"),
    ("sticker", "[贴纸]"),
    ("voice", "[语音]"),
    ("audio", "[音频]"),
    ("video_note", "[视频消息]"),
    ("document", "[文件]"),
)


def _message_text(message) -> str:
    text = (message.text or message.caption or "").strip()
    if text:
        return text
    for attr, label in _MEDIA_LABELS:
        if getattr(message, attr, None):
            return label
    return ""


async def collect_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not config.is_authorized(chat.id):
        return

    text = _message_text(message)
    if not text:
        return
    # 指令本身不算讨论内容
    if text.startswith("/"):
        return

    user = message.from_user
    if user is not None:
        user_id = user.id
        user_name = user.full_name or user.username or "群友"
    else:
        # 频道身份 / 匿名管理员：保留内容，但不参与日报人物归属
        sender_chat = message.sender_chat
        user_id = 0
        user_name = (sender_chat.title if sender_chat else "") or "频道"

    reply_to = None
    if message.reply_to_message is not None:
        reply_to = message.reply_to_message.message_id

    save_message(
        chat_id=chat.id,
        message_id=message.message_id,
        user_id=user_id,
        user_name=user_name,
        text=text,
        reply_to_msg_id=reply_to,
    )

    if chat.title:
        config.set_group_title(chat.id, chat.title)
