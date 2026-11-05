"""Bot 入口：消息采集 + 总结 / 日报指令 + 定时调度。"""
import asyncio
import logging
import sys

from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from handlers import commands as cmd
from handlers.messages import collect_group_message
from scheduler import scheduler_loop
from services.llm import describe_chain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("summary-bot")


async def post_init(application: Application) -> None:
    config.bootstrap()
    application.bot_data["scheduler_task"] = asyncio.create_task(scheduler_loop(application.bot))

    group_commands = [
        BotCommand("sum", "总结群聊发言（可带条数或时长）"),
        BotCommand("autosum", "设置定时总结"),
        BotCommand("autodaily", "设置群日报"),
        BotCommand("settings", "功能面板"),
        BotCommand("id", "查看群 ID"),
        BotCommand("help", "使用说明"),
    ]
    private_commands = [
        BotCommand("start", "开始"),
        BotCommand("help", "使用说明"),
        BotCommand("id", "查看我的 ID"),
        BotCommand("auth", "授权群（Owner）"),
        BotCommand("unauth", "撤销授权（Owner）"),
        BotCommand("status", "模型链状态（Owner）"),
        BotCommand("settings", "已授权群列表（Owner）"),
    ]
    try:
        await application.bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
    except Exception as exc:
        logger.warning("设置指令菜单失败：%s", exc)

    chain = describe_chain()
    logger.info("模型链共 %s 个节点", len(chain))
    for node in chain:
        logger.info("  - %s (%s) key=%s", node["name"], node["model"], "ok" if node["key_ready"] else "missing")


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("scheduler_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("处理更新失败：%s", context.error, exc_info=context.error)


def main() -> None:
    errors = config.validate_runtime()
    if errors:
        for error in errors:
            logger.error("启动检查失败：%s", error)
        sys.exit(2)

    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CallbackQueryHandler(cmd.on_callback))
    application.add_handler(CommandHandler("start", cmd.cmd_start))
    application.add_handler(CommandHandler("help", cmd.cmd_help))
    application.add_handler(CommandHandler("id", cmd.cmd_id))
    application.add_handler(CommandHandler("auth", cmd.cmd_auth))
    application.add_handler(CommandHandler("unauth", cmd.cmd_unauth))
    application.add_handler(CommandHandler("status", cmd.cmd_status))
    application.add_handler(CommandHandler("settings", cmd.cmd_settings))
    application.add_handler(CommandHandler("sum", cmd.cmd_sum))
    application.add_handler(CommandHandler("autosum", cmd.cmd_autosum))
    application.add_handler(CommandHandler("autodaily", cmd.cmd_autodaily))
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, collect_group_message),
        group=1,
    )
    application.add_error_handler(on_error)

    logger.info("Bot 启动，开始轮询")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
