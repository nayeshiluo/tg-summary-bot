"""运行配置与群组状态持久化。

所有敏感值只从环境变量读取，代码内不保留任何真实 Token、Owner ID 或私有端点。
群组状态（授权群、功能开关、定时规则）存放在 data/state.json，原子写入 + 文件锁。
"""
import copy
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # 极端精简环境无 tzdata 时兜底
    BEIJING_TZ = timezone(timedelta(hours=8))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
DB_PATH = DATA_DIR / "messages.db"
LLM_ENDPOINTS_FILE = Path(os.getenv("LLM_ENDPOINTS_FILE") or (DATA_DIR / "llm_endpoints.json"))

logger = logging.getLogger("summary-bot.config")


def _env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        logger.warning("环境变量 %s 不是整数，已使用默认值", name)
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        logger.warning("环境变量 %s 不是数字，已使用默认值", name)
        return default


def _env_list(name: str) -> list:
    raw = os.getenv(name) or ""
    return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]


BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
OWNER_ID = _env_int("OWNER_ID", 0)

# 可选初始授权群，逗号分隔；运行期也可由 Owner 用 /auth 增删。
INITIAL_GROUPS = _env_list("AUTHORIZED_GROUPS")
# 普通群友的取数上限，Owner / 群管理员不受此限制。
MEMBER_MAX_MESSAGES = _env_int("MEMBER_MAX_MESSAGES", 500)
MEMBER_MAX_HOURS = _env_float("MEMBER_MAX_HOURS", 24.0)
PRIVILEGED_MAX_MESSAGES = _env_int("PRIVILEGED_MAX_MESSAGES", 3000)
PRIVILEGED_MAX_HOURS = _env_float("PRIVILEGED_MAX_HOURS", 168.0)
DEFAULT_MESSAGE_LIMIT = _env_int("DEFAULT_MESSAGE_LIMIT", 80)
MIN_MESSAGES = _env_int("MIN_MESSAGES", 3)
# 单群消息保留天数；0 表示不自动清理。
MESSAGE_RETENTION_DAYS = _env_int("MESSAGE_RETENTION_DAYS", 30)

DEFAULT_SUMMARY = {
    "enabled": False,
    "mode": "daily_time",
    "time_points": ["22:00"],
    "interval_hours": 4.0,
    "lookback_hours": 4.0,
    "last_run_timestamp": 0.0,
    "last_run_slot": "",
    "last_attempt_timestamp": 0.0,
    "last_attempt_slot": "",
    "last_attempt_state": "",
    "last_error": "",
}

DEFAULT_DAILY = {
    "enabled": False,
    "time_points": ["08:00", "20:00"],
    "lookback_hours": 12.0,
    "last_run_timestamp": 0.0,
    "last_run_slot": "",
    "last_error": "",
}

DEFAULT_FEATURES = {"summary": True, "daily_report": True}

_DEFAULT_STATE = {
    "authorized_groups": {},
    "group_features": {},
    "scheduled_summary": {},
    "daily_report": {},
}

_STATE_LOCK = threading.RLock()
_STATE_CACHE: Optional[dict] = None
_STATE_MTIME: Optional[int] = None


def _read_state_file() -> dict:
    if not STATE_FILE.exists():
        return copy.deepcopy(_DEFAULT_STATE)
    with open(STATE_FILE, encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("state.json 结构损坏，拒绝覆盖")
    for key, default in _DEFAULT_STATE.items():
        data.setdefault(key, copy.deepcopy(default))
    return data


def load_state() -> dict:
    """按 mtime 热加载；外部编辑后下一次调用立即生效。"""
    global _STATE_CACHE, _STATE_MTIME
    with _STATE_LOCK:
        try:
            mtime = STATE_FILE.stat().st_mtime_ns if STATE_FILE.exists() else 0
        except OSError:
            mtime = 0
        if _STATE_CACHE is not None and _STATE_MTIME == mtime:
            return _STATE_CACHE
        try:
            _STATE_CACHE = _read_state_file()
            _STATE_MTIME = mtime
        except Exception as exc:
            logger.error("读取 state.json 失败：%s", exc)
            if _STATE_CACHE is None:
                _STATE_CACHE = copy.deepcopy(_DEFAULT_STATE)
        return _STATE_CACHE


def save_state(state: dict) -> None:
    """原子写入，避免半截文件。"""
    global _STATE_CACHE, _STATE_MTIME
    with _STATE_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, STATE_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        _STATE_CACHE = copy.deepcopy(state)
        _STATE_MTIME = STATE_FILE.stat().st_mtime_ns


def _mutate(mutator) -> dict:
    with _STATE_LOCK:
        state = copy.deepcopy(load_state())
        mutator(state)
        save_state(state)
        return state


def bootstrap() -> None:
    """首次启动写入初始授权群，不覆盖已有状态。"""
    state = load_state()
    changed = False
    for raw in INITIAL_GROUPS:
        try:
            gid = str(int(raw))
        except ValueError:
            logger.warning("AUTHORIZED_GROUPS 中存在非法群 ID，已忽略")
            continue
        if gid not in state["authorized_groups"]:
            state["authorized_groups"][gid] = {"title": "", "added_at": datetime.now(BEIJING_TZ).timestamp()}
            changed = True
    if changed:
        save_state(state)


def is_authorized(chat_id) -> bool:
    return str(chat_id) in load_state()["authorized_groups"]


def authorize_group(chat_id, title: str = "") -> None:
    gid = str(chat_id)

    def _apply(state):
        entry = state["authorized_groups"].setdefault(gid, {})
        if title:
            entry["title"] = title
        entry.setdefault("added_at", datetime.now(BEIJING_TZ).timestamp())

    _mutate(_apply)


def revoke_group(chat_id) -> None:
    gid = str(chat_id)
    for key in ("authorized_groups", "group_features", "scheduled_summary", "daily_report"):
        _mutate(lambda state, k=key: state[k].pop(gid, None))


def get_group_title(chat_id, fallback: str = "") -> str:
    entry = load_state()["authorized_groups"].get(str(chat_id)) or {}
    return entry.get("title") or fallback or str(chat_id)


def set_group_title(chat_id, title: str) -> None:
    if not title:
        return
    gid = str(chat_id)

    def _apply(state):
        entry = state["authorized_groups"].setdefault(gid, {})
        if entry.get("title") != title:
            entry["title"] = title

    _mutate(_apply)


def get_features(chat_id) -> dict:
    features = load_state()["group_features"].get(str(chat_id)) or {}
    return {**DEFAULT_FEATURES, **features}


def is_feature_enabled(chat_id, feature: str) -> bool:
    return bool(get_features(chat_id).get(feature, True))


def set_feature(chat_id, feature: str, enabled: bool) -> None:
    gid = str(chat_id)

    def _apply(state):
        bucket = state["group_features"].setdefault(gid, {})
        bucket[feature] = bool(enabled)

    _mutate(_apply)


def get_schedule(chat_id) -> dict:
    stored = load_state()["scheduled_summary"].get(str(chat_id)) or {}
    return {**DEFAULT_SUMMARY, **stored}


def set_schedule(chat_id, updates: dict) -> dict:
    gid = str(chat_id)
    result: Dict[str, Any] = {}

    def _apply(state):
        bucket = state["scheduled_summary"].setdefault(gid, {})
        bucket.update(updates)
        result.update({**DEFAULT_SUMMARY, **bucket})

    _mutate(_apply)
    return result


def get_daily(chat_id) -> dict:
    stored = load_state()["daily_report"].get(str(chat_id)) or {}
    return {**DEFAULT_DAILY, **stored}


def set_daily(chat_id, updates: dict) -> dict:
    gid = str(chat_id)
    result: Dict[str, Any] = {}

    def _apply(state):
        bucket = state["daily_report"].setdefault(gid, {})
        bucket.update(updates)
        result.update({**DEFAULT_DAILY, **bucket})

    _mutate(_apply)
    return result


def authorized_group_ids() -> list:
    return list(load_state()["authorized_groups"].keys())


def validate_runtime() -> list:
    """启动自检，返回致命错误列表。"""
    errors = []
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN 未设置或格式不正确")
    if not OWNER_ID:
        errors.append("OWNER_ID 未设置（必须填你的 Telegram 数字 ID）")
    if not LLM_ENDPOINTS_FILE.exists():
        errors.append(f"缺少 LLM 端点配置：{LLM_ENDPOINTS_FILE}")
    return errors
