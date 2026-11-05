"""指令与面板。

权限模型：
- Owner（OWNER_ID）：全部能力，可授权/撤销群、查看模型链。
- 群主 / 群管理员：本群总结、定时规则与面板。
- 普通群员：仅 /sum，且取数有上限。
"""
import asyncio
import html
import logging
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

import args as arg_parser
import config
from scheduler import execute_daily_report, execute_scheduled_summary
from services.delivery import DeliveryUncertain, is_ambiguous_delivery_error, send_chunks
from services.llm import describe_chain
from summarize import split_summary_chunks, summarize_messages
from storage import get_messages_by_time, get_recent_messages

logger = logging.getLogger("summary-bot.commands")

HELP_TEXT = """<b>群聊总结 Bot</b>

<b>总结</b>
<code>/sum</code> 最近 80 条
<code>/sum 300</code> 最近 300 条
<code>/sum 4h</code> 最近 4 小时
<code>/sum 今天</code> / <code>/sum 昨天</code>

<b>定时总结</b>（群主 / 管理员）
<code>/autosum 22:00 4h</code> 每天 22:00 总结前 4 小时
<code>/autosum 4h 4h</code> 每 4 小时总结前 4 小时
<code>/autosum test</code> 立即测试
<code>/autosum off</code> 关闭

<b>群日报</b>（群主 / 管理员）
<code>/autodaily 08:00,20:00 12h</code>
<code>/autodaily test</code> / <code>/autodaily off</code>

<b>面板</b>
<code>/settings</code> 开关与预设
<code>/id</code> 查看当前群 ID

首次使用：把 Bot 拉进群 → Owner 私聊或群内发 <code>/auth</code> 授权。"""


def _is_owner(user_id) -> bool:
    return bool(config.OWNER_ID) and int(user_id or 0) == int(config.OWNER_ID)


async def is_group_admin(bot, chat_id, user_id) -> bool:
    if _is_owner(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


def _group_only(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    await _reply(
        update,
        f"群 ID：<code>{chat.id}</code>\n你的 ID：<code>{user.id}</code>\n"
        f"当前群授权：{'✅ 已授权' if config.is_authorized(chat.id) else '❌ 未授权'}",
    )


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not _is_owner(user.id):
        return
    target = None
    if context.args:
        try:
            target = int(context.args[0])
        except ValueError:
            await _reply(update, "群 ID 必须是数字，例如 <code>/auth -1001234567890</code>")
            return
    elif chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        target = chat.id
    if target is None:
        await _reply(update, "请在群内发 <code>/auth</code>，或私聊 <code>/auth &lt;群ID&gt;</code>")
        return
    title = chat.title if (chat and chat.id == target) else ""
    config.authorize_group(target, title or "")
    await _reply(update, f"✅ 已授权群 <code>{target}</code>，现在开始积累该群消息。")


async def cmd_unauth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not _is_owner(user.id):
        return
    target = None
    if context.args:
        try:
            target = int(context.args[0])
        except ValueError:
            await _reply(update, "群 ID 必须是数字")
            return
    elif chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        target = chat.id
    if target is None:
        await _reply(update, "用法：<code>/unauth &lt;群ID&gt;</code>")
        return
    config.revoke_group(target)
    await _reply(update, f"已撤销群 <code>{target}</code> 的授权与全部配置。")


async def cmd_sum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await _reply(update, "⚠️ <code>/sum</code> 只能在群聊里使用。")
        return
    if not config.is_authorized(chat.id):
        return
    if not config.is_feature_enabled(chat.id, "summary"):
        await _reply(update, "⚠️ 本群已关闭群聊总结功能。")
        return

    privileged = await is_group_admin(context.bot, chat.id, user.id)
    arg_str = " ".join(context.args) if context.args else ""
    mode, value, desc = arg_parser.parse_summary_arg(arg_str, is_privileged=privileged)

    status = await message.reply_text(
        f"⏳ {'👑 ' if privileged else ''}正在归纳【{desc}】，请稍候..."
    )
    try:
        if mode == "time":
            messages = await asyncio.to_thread(get_messages_by_time, chat.id, value)
        else:
            messages = await asyncio.to_thread(get_recent_messages, chat.id, value)

        if len(messages) < config.MIN_MESSAGES:
            await status.edit_text(f"ℹ️ 【{desc}】暂无足够发言（{len(messages)} 条），多聊几句再试。")
            return

        content, error = await summarize_messages(
            chat_id=chat.id,
            chat_title=chat.title or "群聊",
            target_desc=desc,
            messages=messages,
        )
        if not content:
            await status.edit_text(f"❌ 总结生成失败：{html.escape(str(error or '模型未返回内容'))}")
            return

        chunks = split_summary_chunks(content, max_chars=3800)
        try:
            await context.bot.edit_message_text(
                text="",
                chat_id=chat.id,
                message_id=status.message_id,
                api_kwargs={"rich_message": {"markdown": chunks[0]}},
            )
            delivered = 1
        except Exception as exc:
            if is_ambiguous_delivery_error(exc):
                logger.error("手动总结编辑结果未知 chat=%s；不重发", chat.id)
                return
            try:
                await status.delete()
            except Exception:
                pass
            delivered = 0

        if delivered:
            await send_chunks(context.bot, chat.id, chunks[1:])
        else:
            await send_chunks(context.bot, chat.id, chunks)
    except DeliveryUncertain as exc:
        logger.error("手动总结投递结果未知 chat=%s: %s", chat.id, exc)
    except Exception as exc:
        logger.error("手动总结执行异常 chat=%s: %s", chat.id, exc, exc_info=True)
        try:
            await status.edit_text("❌ 生成总结时发生异常，请检查日志。")
        except Exception:
            pass


async def _resolve_target_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群内直接返回当前群；Owner 私聊时允许首个参数是群 ID。"""
    chat = update.effective_chat
    user = update.effective_user
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return chat.id, context.args or []
    if chat and chat.type == ChatType.PRIVATE and user and _is_owner(user.id):
        if context.args:
            try:
                return int(context.args[0]), context.args[1:]
            except ValueError:
                return None, []
    return None, []


async def cmd_autosum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    target, rest = await _resolve_target_chat(update, context)
    if target is None:
        await _reply(update, "用法：群内 <code>/autosum 22:00 4h</code>，或 Owner 私聊 <code>/autosum &lt;群ID&gt; 22:00 4h</code>")
        return
    if not _is_owner(user.id) and not await is_group_admin(context.bot, target, user.id):
        return
    if not config.is_authorized(target):
        await _reply(update, f"⚠️ 群 <code>{target}</code> 尚未授权。")
        return

    ok, payload, message = arg_parser.parse_autosum_args(rest)
    if not ok:
        await _reply(update, message)
        return
    if payload.get("action") == "test":
        await _reply(update, "⚡ 已触发一次定时总结测试，请稍候。")
        await execute_scheduled_summary(context.bot, target, config.get_group_title(target), is_test=True)
        return

    if payload.get("enabled") and payload.get("mode") == "interval":
        payload["last_run_timestamp"] = time.time()
    config.set_schedule(target, payload)
    schedule = config.get_schedule(target)
    await _reply(
        update,
        f"✅ {message}\n下次预计：<code>{arg_parser.next_run_desc(schedule)}</code>",
    )


async def cmd_autodaily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    target, rest = await _resolve_target_chat(update, context)
    if target is None:
        await _reply(update, "用法：群内 <code>/autodaily 08:00,20:00 12h</code>，或 Owner 私聊带群 ID")
        return
    if not _is_owner(user.id) and not await is_group_admin(context.bot, target, user.id):
        return
    if not config.is_authorized(target):
        await _reply(update, f"⚠️ 群 <code>{target}</code> 尚未授权。")
        return

    ok, payload, message = arg_parser.parse_autodaily_args(rest)
    if not ok:
        await _reply(update, message)
        return
    if payload.get("action") == "test":
        await _reply(update, "⚡ 已触发一次日报测试，请稍候。")
        await execute_daily_report(context.bot, target, config.get_group_title(target), is_test=True)
        return

    config.set_daily(target, payload)
    report = config.get_daily(target)
    await _reply(
        update,
        f"✅ {message}\n下次预计：<code>{arg_parser.next_run_desc({**report, 'mode': 'daily_time'})}</code>",
    )


SCHEDULE_PRESETS = {
    "daily_22_4h": ("🌙 每天 22:00（前4h）", {"enabled": True, "mode": "daily_time", "time_points": ["22:00"], "lookback_hours": 4.0}),
    "interval_4h": ("🔄 4h 整点循环（前4h）", {"enabled": True, "mode": "daily_time", "time_points": ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"], "lookback_hours": 4.0}),
    "interval_8h": ("🔄 8h 循环（7/15/23点）", {"enabled": True, "mode": "daily_time", "time_points": ["07:00", "15:00", "23:00"], "lookback_hours": 8.0}),
}

DAILY_PRESETS = {
    "morning_evening": ("📰 每天 08:00 & 20:00（前12h）", {"enabled": True, "time_points": ["08:00", "20:00"], "lookback_hours": 12.0}),
    "daily_9_24h": ("📰 每天 09:00（前24h）", {"enabled": True, "time_points": ["09:00"], "lookback_hours": 24.0}),
}


def build_panel(chat_id) -> tuple:
    features = config.get_features(chat_id)
    schedule = config.get_schedule(chat_id)
    daily = config.get_daily(chat_id)
    title = config.get_group_title(chat_id)

    sched_line = "已关闭"
    if schedule.get("enabled"):
        if schedule.get("mode") == "interval":
            sched_line = f"每 {float(schedule.get('interval_hours', 4)):g}h（前 {float(schedule.get('lookback_hours', 4)):g}h）"
        else:
            sched_line = f"每天 {', '.join(schedule.get('time_points') or [])}（前 {float(schedule.get('lookback_hours', 4)):g}h）"
    daily_line = "已关闭"
    if daily.get("enabled"):
        daily_line = f"每天 {', '.join(daily.get('time_points') or [])}（前 {float(daily.get('lookback_hours', 12)):g}h）"

    text = (
        f"⚙️ <b>【群功能面板】</b>\n\n"
        f"👥 <b>{html.escape(title)}</b> (<code>{chat_id}</code>)\n"
        f"• 群聊总结：{'🟢 开启中' if features.get('summary') else '🔴 已关闭'}\n"
        f"• 群日报：{'🟢 开启中' if features.get('daily_report') else '🔴 已关闭'}\n"
        f"• 定时总结：<b>{sched_line}</b>\n"
        f"• 日报规则：<b>{daily_line}</b>\n"
        f"• 下次定时总结：<code>{arg_parser.next_run_desc(schedule)}</code>\n"
        f"• 下次日报：<code>{arg_parser.next_run_desc({**daily, 'mode': 'daily_time'})}</code>"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("总结 🟢" if features.get("summary") else "总结 🔴", callback_data="tog:summary"),
            InlineKeyboardButton("日报 🟢" if features.get("daily_report") else "日报 🔴", callback_data="tog:daily"),
        ],
        [
            InlineKeyboardButton("🌙 22:00 前4h", callback_data="sched:daily_22_4h"),
            InlineKeyboardButton("🔄 4h 循环", callback_data="sched:interval_4h"),
        ],
        [
            InlineKeyboardButton("🔄 8h 循环", callback_data="sched:interval_8h"),
            InlineKeyboardButton("🛑 关闭定时总结", callback_data="sched:off"),
        ],
        [
            InlineKeyboardButton("📰 08:00 & 20:00", callback_data="daily:morning_evening"),
            InlineKeyboardButton("📰 09:00 前24h", callback_data="daily:daily_9_24h"),
        ],
        [
            InlineKeyboardButton("🛑 关闭日报", callback_data="daily:off"),
            InlineKeyboardButton("⚡ 测试总结", callback_data="run:summary"),
        ],
        [
            InlineKeyboardButton("⚡ 测试日报", callback_data="run:daily"),
            InlineKeyboardButton("🔄 刷新", callback_data="panel"),
        ],
    ])
    return text, keyboard


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        if _is_owner(user.id):
            groups = config.authorized_group_ids()
            lines = [f"已授权群 {len(groups)} 个："]
            for gid in groups[:30]:
                lines.append(f"• <code>{gid}</code> {html.escape(config.get_group_title(gid))}")
            lines.append("\n在群里发 <code>/settings</code> 可打开面板。")
            await _reply(update, "\n".join(lines))
        return
    if not config.is_authorized(chat.id):
        await _reply(update, "⚠️ 本群尚未授权，请 Owner 发 <code>/auth</code>。")
        return
    if not await is_group_admin(context.bot, chat.id, user.id):
        return
    text, keyboard = build_panel(chat.id)
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await query.answer("仅群内可用", show_alert=True)
        return
    if not config.is_authorized(chat.id) or not await is_group_admin(context.bot, chat.id, user.id):
        await query.answer("只有群主 / 管理员可以操作", show_alert=True)
        return

    data = query.data or ""
    action, _, value = data.partition(":")

    if action == "tog":
        current = config.get_features(chat.id).get(value, True)
        config.set_feature(chat.id, value, not current)
        await query.answer("已开启" if not current else "已关闭")
    elif action == "sched":
        if value == "off":
            config.set_schedule(chat.id, {"enabled": False})
            await query.answer("已关闭定时总结")
        elif value in SCHEDULE_PRESETS:
            _label, payload = SCHEDULE_PRESETS[value]
            config.set_schedule(chat.id, dict(payload))
            await query.answer("已应用预设")
    elif action == "daily":
        if value == "off":
            config.set_daily(chat.id, {"enabled": False})
            await query.answer("已关闭日报")
        elif value in DAILY_PRESETS:
            _label, payload = DAILY_PRESETS[value]
            config.set_daily(chat.id, dict(payload))
            await query.answer("已应用预设")
    elif action == "run":
        await query.answer("已触发，请稍候")
        if value == "summary":
            await execute_scheduled_summary(
                context.bot, chat.id, config.get_group_title(chat.id), is_test=True
            )
        else:
            await execute_daily_report(
                context.bot, chat.id, config.get_group_title(chat.id), is_test=True
            )
    elif action == "panel":
        await query.answer()

    text, keyboard = build_panel(chat.id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.debug("刷新面板失败：%s", exc)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner 查看模型链与授权群数量。"""
    user = update.effective_user
    if not user or not _is_owner(user.id):
        return
    chain = describe_chain()
    lines = [f"<b>模型链（{len(chain)} 个节点）</b>"]
    for index, node in enumerate(chain, 1):
        key_state = "✅" if node["key_ready"] else "❌ 缺少密钥"
        extra = f" {node['circuit']}" if node.get("circuit") else ""
        lines.append(f"{index}. {html.escape(node['name'])} · <code>{html.escape(node['model'])}</code> {key_state}{extra}")
    lines.append(f"\n授权群：{len(config.authorized_group_ids())} 个")
    await _reply(update, "\n".join(lines))
