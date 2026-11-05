"""指令参数解析：/sum、/autosum、/autodaily。"""
import re
from datetime import datetime, timedelta
from typing import List, Tuple

from config import (
    BEIJING_TZ,
    DEFAULT_MESSAGE_LIMIT,
    MEMBER_MAX_HOURS,
    MEMBER_MAX_MESSAGES,
    PRIVILEGED_MAX_HOURS,
    PRIVILEGED_MAX_MESSAGES,
)


def parse_summary_arg(arg_str: str, is_privileged: bool = False) -> Tuple[str, int, str]:
    """返回 (模式, 数值, 中文描述)。普通成员与管理员的上限不同。"""
    if not arg_str:
        return "count", DEFAULT_MESSAGE_LIMIT, f"最近 {DEFAULT_MESSAGE_LIMIT} 条发言"
    text = arg_str.strip().lower()

    max_count = PRIVILEGED_MAX_MESSAGES if is_privileged else MEMBER_MAX_MESSAGES
    max_hours = PRIVILEGED_MAX_HOURS if is_privileged else MEMBER_MAX_HOURS
    max_days = max_hours / 24.0

    match = re.match(r"^(\d+)(?:条)?$", text)
    if match:
        value = int(match.group(1))
        if value > max_count:
            value = max_count
            desc = f"最近 {value} 条发言" if is_privileged else f"最近 {value} 条发言（已达普通上限）"
        else:
            value = max(10, value)
            desc = f"最近 {value} 条发言"
        return "count", value, desc

    if text in ("今天", "今日", "today"):
        now = datetime.now(BEIJING_TZ)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds = max(60, int((now - start_of_day).total_seconds()))
        return "time", seconds, "今日发言（自 0 点起）"

    if text in ("昨天", "昨日", "yesterday"):
        now = datetime.now(BEIJING_TZ)
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds = int((now - start_of_today).total_seconds()) + 86400
        max_seconds = int(max_hours * 3600)
        capped = seconds > max_seconds
        if capped:
            seconds = max_seconds
        desc = f"最近 {round(seconds / 3600, 1):g} 小时发言（含昨日）"
        if capped and not is_privileged:
            desc = f"最近 {round(seconds / 3600, 1):g} 小时发言（已达普通上限）"
        return "time", seconds, desc

    if text in ("半小时", "0.5h", "0.5小时"):
        return "time", 1800, "最近 30 分钟发言"

    match = re.match(r"^(\d+(?:\.\d+)?)\s*(?:m|min|mins|分|分钟)$", text)
    if match:
        minutes = max(1.0, min(float(match.group(1)), max_hours * 60))
        return "time", int(minutes * 60), f"最近 {minutes:g} 分钟发言"

    match = re.match(r"^(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours|时|小时)$", text)
    if match:
        hours = max(0.1, min(float(match.group(1)), max_hours))
        desc = f"最近 {hours:g} 小时发言"
        if not is_privileged and hours >= max_hours:
            desc += "（已达普通上限）"
        return "time", int(hours * 3600), desc

    match = re.match(r"^(\d+(?:\.\d+)?)\s*(?:d|day|days|天)$", text)
    if match:
        days = max(0.1, min(float(match.group(1)), max_days))
        desc = f"最近 {days:g} 天发言"
        if not is_privileged and days >= max_days:
            desc += "（已达普通上限）"
        return "time", int(days * 86400), desc

    return "count", DEFAULT_MESSAGE_LIMIT, f"最近 {DEFAULT_MESSAGE_LIMIT} 条发言"


def parse_autosum_args(args: List[str]) -> Tuple[bool, dict, str]:
    """解析 /autosum：支持固定时间点、整点循环、测试与关闭。"""
    if not args:
        return False, {}, "用法：<code>/autosum 22:00 4h</code>、<code>/autosum 4h 4h</code> 或 <code>/autosum off</code>"

    text = " ".join(args).strip().lower()

    if text in ("off", "0", "close", "关", "关闭", "stop", "disable"):
        return True, {"enabled": False}, "已关闭定时总结"
    if text in ("test", "run", "now", "测试", "立即执行"):
        return True, {"action": "test"}, "立即测试"

    # 循环模式：/autosum 4h 4h
    match = re.match(
        r"^(?:每|every\s*)?(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours|时|小时)\s*"
        r"(?:(?:前|过去)?(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours|时|小时)?)?$",
        text,
    )
    if match and ":" not in text:
        interval_hours = float(match.group(1))
        if interval_hours < 0.5 or interval_hours > 72:
            return False, {}, "间隔周期建议在 0.5 ~ 72 小时之间"
        lookback = float(match.group(2)) if match.group(2) else interval_hours
        if lookback < 0.5 or lookback > 168:
            return False, {}, "回溯时长建议在 0.5 ~ 168 小时之间"
        return True, {
            "enabled": True,
            "mode": "interval",
            "interval_hours": interval_hours,
            "lookback_hours": lookback,
        }, f"每 {interval_hours:g} 小时自动总结过去 {lookback:g} 小时发言"

    time_points = []
    lookback_hours = None
    for token in re.split(r"[,，\s]+", text):
        if not token:
            continue
        match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", token)
        if match:
            time_points.append(f"{int(match.group(1)):02d}:{int(match.group(2)):02d}")
            continue
        match = re.match(r"^(?:前|过去)?(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours|时|小时)?$", token)
        if match and lookback_hours is None:
            value = float(match.group(1))
            if value > 0:
                lookback_hours = value

    if not time_points:
        return False, {}, "未能识别有效时间点，格式示例：<code>22:00</code> 或 <code>12:00,22:00</code>"

    time_points = sorted(set(time_points))
    if lookback_hours is None:
        lookback_hours = 4.0
    elif lookback_hours < 0.5 or lookback_hours > 168:
        return False, {}, "回溯时长建议在 0.5 ~ 168 小时之间"

    return True, {
        "enabled": True,
        "mode": "daily_time",
        "time_points": time_points,
        "lookback_hours": lookback_hours,
    }, f"每天 {', '.join(time_points)}（北京时间）自动总结过去 {lookback_hours:g} 小时发言"


def parse_autodaily_args(args: List[str]) -> Tuple[bool, dict, str]:
    """解析 /autodaily：日报式定时总结。"""
    if not args:
        return False, {}, "用法：<code>/autodaily 08:00,20:00 12h</code> 或 <code>/autodaily off</code>"
    text = " ".join(args).strip().lower()

    if text in ("off", "0", "close", "关", "关闭", "stop", "disable"):
        return True, {"enabled": False}, "已关闭日报"
    if text in ("test", "run", "now", "测试", "立即执行"):
        return True, {"action": "test"}, "立即测试日报"

    time_points = []
    lookback = None
    for token in re.split(r"[,，\s]+", text):
        if not token:
            continue
        match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", token)
        if match:
            time_points.append(f"{int(match.group(1)):02d}:{int(match.group(2)):02d}")
            continue
        match = re.fullmatch(r"(?:前|过去)?(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours|时|小时)", token)
        if match:
            lookback = float(match.group(1))

    if not time_points:
        return False, {}, "时间格式错误，示例：<code>/autodaily 08:00,20:00 12h</code>"
    if lookback is None:
        lookback = 12.0
    if not 0.5 <= lookback <= 168:
        return False, {}, "回溯范围应为 0.5 ~ 168 小时"

    time_points = sorted(set(time_points))
    return True, {
        "enabled": True,
        "time_points": time_points,
        "lookback_hours": lookback,
    }, f"每天 {', '.join(time_points)}（北京时间）回溯 {lookback:g} 小时生成日报"


def next_run_desc(config: dict) -> str:
    if not config.get("enabled"):
        return "—（已停用）"
    now = datetime.now(BEIJING_TZ)
    if config.get("mode", "daily_time") == "daily_time":
        points = sorted(config.get("time_points") or ["22:00"])
        for point in points:
            try:
                hour, minute = map(int, point.split(":"))
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target > now:
                    return f"今日 {point}"
            except Exception:
                continue
        return f"明日 {points[0]}"
    interval_hours = float(config.get("interval_hours", 4.0))
    last_ts = max(float(config.get("last_run_timestamp", 0)), float(config.get("last_attempt_timestamp", 0)))
    next_ts = (now.timestamp() + interval_hours * 3600) if last_ts == 0 else last_ts + interval_hours * 3600
    if next_ts <= now.timestamp():
        next_ts = now.timestamp() + 60
    next_dt = datetime.fromtimestamp(next_ts, tz=BEIJING_TZ)
    if next_dt.date() == now.date():
        return f"今日 {next_dt.strftime('%H:%M')}"
    if next_dt.date() == (now.date() + timedelta(days=1)):
        return f"明日 {next_dt.strftime('%H:%M')}"
    return next_dt.strftime("%m-%d %H:%M")
