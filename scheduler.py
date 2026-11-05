"""定时调度与执行。

槽位（slot）设计：
- 固定时间点：`daily:2026-01-01_22:00`，同一槽位只成功执行一次。
- 循环模式：`interval:14400:<bucket>`，bucket 由上次成功时间 + 间隔推导，
  因此重试不会漂移，成功后才推进下一次。
- 失败重试：同一槽位最多 3 次、间隔 5 分钟；投递结果不确定时不自动重发。
"""
import asyncio
import datetime
import logging
import math
import time
from typing import Optional

import config
from services import delivery
from services.rich import rich_to_html
from summarize import DAILY_REPORT_NO_CONTENT, split_summary_chunks, summarize_messages
from storage import get_messages_by_time, prune_old_messages

logger = logging.getLogger("summary-bot.scheduler")
BEIJING_TZ = config.BEIJING_TZ

DAILY_REPORT_MAX_MESSAGES = 8000
MAX_RETRIES = 3
RETRY_COOLDOWN = 300.0

_inflight: set = set()
_retry_state: dict = {}


def slot_window_end(slot: str, schedule: Optional[dict] = None) -> Optional[float]:
    """把槽位还原成不可变的时间窗口终点。"""
    if not isinstance(slot, str) or not slot:
        return None
    if slot.startswith("daily:"):
        slot = slot[6:]
    if slot.startswith("interval:"):
        parts = slot.split(":")
        if len(parts) != 3 or not schedule:
            return None
        seconds, bucket = int(parts[1]), int(parts[2])
        due = float(schedule.get("last_run_timestamp", 0)) + seconds
        if seconds < 1800 or not math.isfinite(due) or due <= 0 or int(due // seconds) != bucket:
            return None
        return due
    try:
        dt = datetime.datetime.strptime(slot, "%Y-%m-%d_%H:%M")
    except ValueError:
        return None
    return dt.replace(tzinfo=BEIJING_TZ).timestamp()


def _daily_slot_due(target_dt, now, last_check_dt, last_run_slot, slot, retryable) -> bool:
    if last_run_slot == slot:
        return False
    crossed = (
        last_check_dt < target_dt <= now
        if last_check_dt is not None
        else 0 <= (now - target_dt).total_seconds() <= 300
    )
    return crossed or (target_dt <= now and retryable)


def _retryable(chat_id, slot: str) -> bool:
    attempts, last_ts = _retry_state.get((str(chat_id), slot), (0, 0.0))
    if attempts >= MAX_RETRIES:
        return False
    return time.time() - last_ts >= RETRY_COOLDOWN


def _mark_attempt(chat_id, slot: str) -> None:
    key = (str(chat_id), slot)
    attempts, _ = _retry_state.get(key, (0, 0.0))
    _retry_state[key] = (attempts + 1, time.time())


def _clear_attempts(chat_id, slot: str) -> None:
    _retry_state.pop((str(chat_id), slot), None)


def _cleanup_retry_state() -> None:
    cutoff = time.time() - 86400
    for key, (_attempts, last_ts) in list(_retry_state.items()):
        if last_ts < cutoff:
            _retry_state.pop(key, None)


async def _deliver(bot, chat_id, content: str, status_msg=None) -> None:
    chunks = split_summary_chunks(content, max_chars=3800)
    delivered = 0
    if status_msg is not None:
        try:
            await bot.edit_message_text(
                text="",
                chat_id=chat_id,
                message_id=status_msg.message_id,
                api_kwargs={"rich_message": {"markdown": chunks[0]}},
            )
            delivered = 1
        except Exception as exc:
            if delivery.is_ambiguous_delivery_error(exc):
                raise delivery.DeliveryUncertain("status edit outcome unknown") from exc
            try:
                await status_msg.delete()
            except Exception:
                pass

    for index, chunk in enumerate(chunks):
        if index == 0 and delivered:
            continue
        try:
            await delivery.send_rich_or_html(bot, chat_id, chunk)
        except delivery.DeliveryUncertain:
            raise
        except Exception as exc:
            if delivered:
                raise delivery.DeliveryUncertain("partial summary already sent; no full replay") from exc
            # 首段富文本与 HTML 都失败时，退回纯文本，至少让用户看到内容
            plain = rich_to_html(chunk)
            await bot.send_message(chat_id=chat_id, text=plain[:4000], disable_web_page_preview=True)
        delivered += 1


async def execute_scheduled_summary(
    bot,
    chat_id: int,
    chat_title: Optional[str] = None,
    lookback_hours: Optional[float] = None,
    is_test: bool = False,
    scheduled_slot: Optional[str] = None,
) -> bool:
    chat_title = chat_title or config.get_group_title(chat_id)
    schedule = config.get_schedule(chat_id)
    if not is_test and not schedule.get("enabled"):
        return True
    if not config.is_feature_enabled(chat_id, "summary"):
        if is_test:
            await bot.send_message(chat_id=chat_id, text="⚠️ 本群已关闭「群聊总结」开关。")
        return True

    key = (str(chat_id), scheduled_slot or "test")
    if not is_test:
        if key in _inflight:
            return True
        _inflight.add(key)
        _mark_attempt(chat_id, scheduled_slot or "")

    hours = float(lookback_hours if lookback_hours is not None else schedule.get("lookback_hours", 4.0))
    desc = f"前 {hours:g} 小时"
    status_msg = None
    succeeded = False
    try:
        if is_test:
            try:
                status_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ 正在生成【{chat_title}】定时总结测试（回溯 {desc}）...",
                )
            except Exception as exc:
                logger.warning("测试状态消息发送失败：%s", exc)

        window_end = None if is_test else slot_window_end(scheduled_slot, schedule)
        if window_end is None:
            messages = await asyncio.to_thread(get_messages_by_time, chat_id, int(hours * 3600))
        else:
            cutoff_label = datetime.datetime.fromtimestamp(window_end, BEIJING_TZ).strftime("%m-%d %H:%M")
            desc = f"截至 {cutoff_label} 的前 {hours:g} 小时"
            messages = await asyncio.to_thread(
                get_messages_by_time, chat_id, int(hours * 3600), 8000, end_timestamp=window_end
            )

        if len(messages) < config.MIN_MESSAGES:
            text = f"ℹ️ 【{desc}】发言仅 {len(messages)} 条，不足以生成总结。"
            if status_msg:
                await status_msg.edit_text(text)
            else:
                logger.info("定时总结样本不足 chat=%s count=%s", chat_id, len(messages))
            succeeded = True
            return True

        content, error = await summarize_messages(
            chat_id=chat_id,
            chat_title=chat_title,
            target_desc=desc,
            messages=messages,
            is_scheduled=True,
        )
        if not content:
            logger.error("定时总结生成失败 chat=%s error=%s", chat_id, error)
            if status_msg:
                await status_msg.edit_text(f"❌ 定时总结生成失败：{error or '模型未返回内容'}")
            return False

        await _deliver(bot, chat_id, content, status_msg)
        logger.info("定时总结已投递 chat=%s samples=%s", chat_id, len(messages))
        succeeded = True
        return True
    except delivery.DeliveryUncertain as exc:
        logger.error("定时总结投递结果未知 chat=%s slot=%s: %s；已禁止自动重发", chat_id, scheduled_slot, exc)
        succeeded = False
        return False
    except Exception as exc:
        logger.error("定时总结执行异常 chat=%s: %s", chat_id, exc, exc_info=True)
        if status_msg:
            try:
                await status_msg.edit_text(f"❌ 定时总结执行异常：{exc}")
            except Exception:
                pass
        return False
    finally:
        if not is_test:
            _inflight.discard(key)
            now_ts = time.time()
            state = {
                "last_attempt_timestamp": now_ts,
                "last_attempt_slot": scheduled_slot,
                "last_attempt_state": "succeeded" if succeeded else "failed",
            }
            if succeeded:
                state.update(
                    last_run_timestamp=now_ts,
                    last_run_slot=scheduled_slot,
                    last_error="",
                )
                _clear_attempts(chat_id, scheduled_slot or "")
            else:
                state["last_error"] = "生成或投递失败"
            config.set_schedule(chat_id, state)


async def execute_daily_report(
    bot,
    chat_id: int,
    chat_title: Optional[str] = None,
    lookback_hours: Optional[float] = None,
    is_test: bool = False,
    scheduled_slot: Optional[str] = None,
) -> bool:
    chat_title = chat_title or config.get_group_title(chat_id)
    report = config.get_daily(chat_id)
    if not is_test and not report.get("enabled"):
        return True
    if not config.is_feature_enabled(chat_id, "daily_report"):
        if is_test:
            await bot.send_message(chat_id=chat_id, text="⚠️ 本群已关闭「群日报」开关。")
        return True

    key = (str(chat_id), scheduled_slot or "daily-test")
    if not is_test:
        if key in _inflight:
            return True
        _inflight.add(key)
        _mark_attempt(chat_id, scheduled_slot or "")

    hours = float(lookback_hours if lookback_hours is not None else report.get("lookback_hours", 12.0))
    succeeded = False
    try:
        window_end = None if is_test else slot_window_end(scheduled_slot or "")
        if window_end is None:
            messages = await asyncio.to_thread(
                get_messages_by_time, chat_id, int(hours * 3600), DAILY_REPORT_MAX_MESSAGES
            )
            desc = f"前 {hours:g} 小时"
        else:
            messages = await asyncio.to_thread(
                get_messages_by_time, chat_id, int(hours * 3600), DAILY_REPORT_MAX_MESSAGES,
                end_timestamp=window_end,
            )
            desc = f"截至 {datetime.datetime.fromtimestamp(window_end, BEIJING_TZ):%m-%d %H:%M} 的前 {hours:g} 小时"

        if len(messages) < config.MIN_MESSAGES:
            logger.info("日报样本不足 chat=%s count=%s", chat_id, len(messages))
            if is_test:
                await bot.send_message(chat_id=chat_id, text=f"ℹ️ 【{desc}】发言仅 {len(messages)} 条，已跳过日报。")
            succeeded = True
            return True

        content, error = await summarize_messages(
            chat_id=chat_id,
            chat_title=chat_title,
            target_desc=desc,
            messages=messages,
            is_scheduled=True,
            style="daily",
        )
        if error == DAILY_REPORT_NO_CONTENT:
            logger.info("日报无可报道话题 chat=%s", chat_id)
            if is_test:
                await bot.send_message(chat_id=chat_id, text="ℹ️ 当前时段可验证的有效话题不足，已跳过日报。")
            succeeded = True
            return True
        if not content:
            logger.error("日报生成失败 chat=%s error=%s", chat_id, error)
            if is_test:
                await bot.send_message(chat_id=chat_id, text=f"❌ 日报生成失败：{error}")
            return False

        await delivery.send_chunks(bot, chat_id, split_summary_chunks(content, max_chars=3800))
        logger.info("日报已投递 chat=%s samples=%s", chat_id, len(messages))
        succeeded = True
        return True
    except delivery.DeliveryUncertain as exc:
        logger.error("日报投递结果未知 chat=%s slot=%s: %s；已禁止自动重发", chat_id, scheduled_slot, exc)
        return False
    except Exception as exc:
        logger.error("日报执行异常 chat=%s: %s", chat_id, exc, exc_info=True)
        return False
    finally:
        if not is_test:
            _inflight.discard(key)
            if succeeded:
                config.set_daily(chat_id, {
                    "last_run_timestamp": time.time(),
                    "last_run_slot": scheduled_slot,
                    "last_error": "",
                })
                _clear_attempts(chat_id, scheduled_slot or "")
            else:
                config.set_daily(chat_id, {"last_error": "生成或投递失败"})


async def scheduler_loop(bot) -> None:
    """每 30 秒轮询一次；所有时间按北京时间计算。"""
    logger.info("定时调度器已启动")
    await asyncio.sleep(5)
    last_check_dt = None
    last_prune_day = ""

    while True:
        try:
            now = datetime.datetime.now(BEIJING_TZ)
            today_str = now.strftime("%Y-%m-%d")
            now_ts = now.timestamp()

            if today_str != last_prune_day:
                removed = await asyncio.to_thread(prune_old_messages)
                if removed:
                    logger.info("已清理 %s 条超过保留期的消息", removed)
                last_prune_day = today_str

            _cleanup_retry_state()

            for gid in config.authorized_group_ids():
                try:
                    daily = config.get_daily(gid)
                    if daily.get("enabled") and config.is_feature_enabled(gid, "daily_report"):
                        for point in daily.get("time_points") or ["08:00", "20:00"]:
                            try:
                                hour, minute = map(int, point.split(":"))
                                target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                                slot = f"daily:{today_str}_{point}"
                                if _daily_slot_due(
                                    target_dt, now, last_check_dt,
                                    daily.get("last_run_slot"), slot, _retryable(gid, slot),
                                ):
                                    logger.info("触发日报 chat=%s slot=%s", gid, slot)
                                    asyncio.create_task(execute_daily_report(
                                        bot=bot, chat_id=int(gid),
                                        chat_title=config.get_group_title(gid),
                                        lookback_hours=float(daily.get("lookback_hours", 12.0)),
                                        scheduled_slot=slot,
                                    ))
                                    break
                            except Exception as exc:
                                logger.warning("日报时间点解析失败 chat=%s point=%s: %s", gid, point, exc)

                    schedule = config.get_schedule(gid)
                    if not schedule.get("enabled") or not config.is_feature_enabled(gid, "summary"):
                        continue

                    should_run = False
                    slot_to_mark = ""
                    if schedule.get("mode", "daily_time") == "daily_time":
                        for point in schedule.get("time_points") or ["22:00"]:
                            try:
                                hour, minute = map(int, point.split(":"))
                                target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                                slot = f"{today_str}_{point}"
                                if _daily_slot_due(
                                    target_dt, now, last_check_dt,
                                    schedule.get("last_run_slot"), slot, _retryable(gid, slot),
                                ):
                                    should_run = True
                                    slot_to_mark = slot
                                    break
                            except Exception as exc:
                                logger.warning("定时总结时间点解析失败 chat=%s point=%s: %s", gid, point, exc)
                    else:
                        interval_seconds = max(1800, int(float(schedule.get("interval_hours", 4.0)) * 3600))
                        last_ts = float(schedule.get("last_run_timestamp", 0))
                        if last_ts == 0.0:
                            config.set_schedule(gid, {"last_run_timestamp": now_ts})
                        elif now_ts - last_ts >= interval_seconds:
                            should_run = True
                            due_ts = last_ts + interval_seconds
                            slot_to_mark = f"interval:{interval_seconds}:{int(due_ts // interval_seconds)}"

                    if should_run:
                        logger.info("触发定时总结 chat=%s slot=%s", gid, slot_to_mark)
                        asyncio.create_task(execute_scheduled_summary(
                            bot=bot, chat_id=int(gid),
                            chat_title=config.get_group_title(gid),
                            lookback_hours=float(schedule.get("lookback_hours", 4.0)),
                            scheduled_slot=slot_to_mark,
                        ))
                except Exception as exc:
                    logger.error("检查群 %s 失败：%s", gid, exc)

            last_check_dt = now
        except asyncio.CancelledError:
            logger.info("定时调度器已停止")
            break
        except Exception as exc:
            logger.error("调度循环异常：%s", exc, exc_info=True)

        await asyncio.sleep(30)
