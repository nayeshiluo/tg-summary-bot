"""投递层：优先原生富文本，失败时降级 HTML；不确定结果绝不自动重发。"""
import asyncio
import logging

from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError

from services.rich import rich_to_html

log = logging.getLogger("summary-bot.delivery")


class DeliveryUncertain(RuntimeError):
    """Telegram 可能已经收到消息，自动重发有重复风险。"""


def is_ambiguous_delivery_error(exc) -> bool:
    # BadRequest 是 NetworkError 的子类，但它代表 Telegram 明确拒绝，不算不确定。
    if isinstance(exc, BadRequest):
        return False
    return isinstance(exc, (NetworkError, TimeoutError, ConnectionError, asyncio.TimeoutError))


async def send_rich_or_html(bot, chat_id, chunk: str):
    try:
        return await bot.do_api_request(
            "sendRichMessage",
            api_kwargs={"chat_id": chat_id, "rich_message": {"markdown": chunk}},
        )
    except Exception as exc:
        if is_ambiguous_delivery_error(exc):
            log.warning("富文本投递结果未知 chat=%s type=%s；不降级重发", chat_id, type(exc).__name__)
            raise DeliveryUncertain("rich send outcome unknown; no automatic resend") from exc
        log.warning("富文本被拒绝 chat=%s type=%s；改用 HTML", chat_id, type(exc).__name__)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=rich_to_html(chunk),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        if is_ambiguous_delivery_error(exc):
            log.warning("HTML 投递结果未知 chat=%s type=%s；不重试", chat_id, type(exc).__name__)
            raise DeliveryUncertain("HTML send outcome unknown; no automatic retry") from exc
        raise


async def send_chunks(bot, chat_id, chunks) -> None:
    """分段投递；后续段失败时标记为不确定，避免整份重发造成重复。"""
    for idx, chunk in enumerate(chunks):
        try:
            await send_rich_or_html(bot, chat_id, chunk)
        except DeliveryUncertain:
            raise
        except Exception as exc:
            if idx:
                raise DeliveryUncertain("partial summary already sent; no full replay") from exc
            raise
