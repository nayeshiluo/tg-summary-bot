"""多节点 LLM 兜底调用。

端点配置（data/llm_endpoints.json）示例：
[
  {"id": "primary", "name": "主节点", "url": "https://api.example.com/v1/chat/completions",
   "key_env": "LLM_API_KEY", "model": "your-model", "timeout": 40},
  {"id": "backup", "name": "备用节点", "url": "https://api2.example.com/v1/chat/completions",
   "key": "sk-...", "model": "your-model-2", "timeout": 30}
]

规则：
- 数组顺序即调用优先级；某个节点失败自动落到下一个。
- 优先用 key_env 从环境变量取密钥，避免把密钥写进 JSON。
- 单个节点超时受总预算约束，不会让第一跳吃掉全部时间。
- 连续失败 2 次的节点熔断 60 秒（最长 180 秒），到期后自动半开重试。
"""
import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import LLM_ENDPOINTS_FILE

logger = logging.getLogger("summary-bot.llm")

LARGE_REQUEST_BUDGET = 100.0
SMALL_REQUEST_BUDGET = 30.0
FINALIZATION_MARGIN = 0.5
MAX_ATTEMPTS = 5
CIRCUIT_FAILURE_THRESHOLD = 2
CIRCUIT_MIN_COOLDOWN = 60.0
CIRCUIT_MAX_COOLDOWN = 180.0
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_CIRCUIT_LOCK = threading.Lock()
_CIRCUIT: Dict[str, Dict[str, float]] = {}
_CONFIG_CACHE: Dict[str, Any] = {"mtime": None, "endpoints": []}


def _read_env_val(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value:
        return value.strip()
    # 兼容把密钥集中放在项目根目录 .env 的部署方式
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_file, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, raw = line.partition("=")
                if name.strip() == key:
                    return raw.strip().strip('"').strip("'")
    except OSError:
        pass
    return default


def _normalize_endpoints(data) -> List[Dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("endpoints") or []
    if not isinstance(data, list):
        raise ValueError("端点配置必须是数组")
    endpoints = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        model = str(item.get("model") or "").strip()
        if not url or not model:
            logger.warning("忽略第 %s 个端点：缺少 url 或 model", index + 1)
            continue
        node_id = str(item.get("id") or f"node{index + 1}").strip()
        endpoints.append(
            {
                "id": node_id,
                "name": str(item.get("name") or node_id),
                "url": url,
                "model": model,
                "key": str(item.get("key") or "").strip(),
                "key_env": str(item.get("key_env") or "").strip(),
                "timeout": float(item.get("timeout") or 40.0),
                "extra_headers": item.get("extra_headers") if isinstance(item.get("extra_headers"), dict) else {},
            }
        )
    return endpoints


def load_endpoints(force: bool = False) -> List[Dict[str, Any]]:
    """按 mtime 热加载，改完 JSON 不用重启。"""
    try:
        mtime = LLM_ENDPOINTS_FILE.stat().st_mtime_ns
    except OSError:
        mtime = 0
    if not force and _CONFIG_CACHE["mtime"] == mtime:
        return _CONFIG_CACHE["endpoints"]
    if not LLM_ENDPOINTS_FILE.exists():
        logger.error("端点配置文件不存在：%s", LLM_ENDPOINTS_FILE)
        _CONFIG_CACHE.update(mtime=mtime, endpoints=[])
        return []
    try:
        with open(LLM_ENDPOINTS_FILE, encoding="utf-8") as stream:
            endpoints = _normalize_endpoints(json.load(stream))
    except Exception as exc:
        logger.error("读取端点配置失败：%s", exc)
        endpoints = _CONFIG_CACHE["endpoints"]
    _CONFIG_CACHE.update(mtime=mtime, endpoints=endpoints)
    return endpoints


def _resolve_key(endpoint: Dict[str, Any]) -> str:
    if endpoint.get("key"):
        return endpoint["key"]
    if endpoint.get("key_env"):
        return _read_env_val(endpoint["key_env"])
    return ""


def _circuit_open(node_id: str) -> bool:
    with _CIRCUIT_LOCK:
        entry = _CIRCUIT.get(node_id)
        if not entry:
            return False
        return time.monotonic() < entry.get("until", 0)


def _record_failure(node_id: str) -> None:
    with _CIRCUIT_LOCK:
        entry = _CIRCUIT.setdefault(node_id, {"failures": 0, "until": 0, "cooldown": CIRCUIT_MIN_COOLDOWN})
        entry["failures"] += 1
        if entry["failures"] >= CIRCUIT_FAILURE_THRESHOLD:
            cooldown = min(entry["cooldown"], CIRCUIT_MAX_COOLDOWN)
            entry["until"] = time.monotonic() + cooldown
            entry["cooldown"] = min(cooldown * 2, CIRCUIT_MAX_COOLDOWN)
            entry["failures"] = 0
            logger.warning("节点 %s 熔断 %.0fs", node_id, cooldown)


def _record_success(node_id: str) -> None:
    with _CIRCUIT_LOCK:
        entry = _CIRCUIT.get(node_id)
        if entry:
            entry.update(failures=0, until=0, cooldown=CIRCUIT_MIN_COOLDOWN)


def circuit_status() -> Dict[str, str]:
    now = time.monotonic()
    with _CIRCUIT_LOCK:
        return {
            node_id: f"熔断中（剩余 {max(0, entry.get('until', 0) - now):.0f}s）"
            for node_id, entry in _CIRCUIT.items()
            if entry.get("until", 0) > now
        }


async def _call_one(endpoint: Dict[str, Any], system_prompt: str, user_prompt: str,
                    max_tokens: int, temperature: float, timeout: float) -> Tuple[Optional[str], str]:
    key = _resolve_key(endpoint)
    if not key:
        return None, "missing_key"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": _BROWSER_UA,
    }
    headers.update({str(k): str(v) for k, v in (endpoint.get("extra_headers") or {}).items()})
    payload = {
        "model": endpoint["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint["url"], headers=headers, json=payload)
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]

    if response.status_code != 200:
        body = (response.text or "")[:400].replace("\n", " ")
        logger.warning("LLM 请求失败 HTTP %s (node=%s) body=%s", response.status_code, endpoint["id"], body)
        return None, f"http_{response.status_code}"

    try:
        data = response.json()
    except Exception:
        return None, "invalid_json"
    choices = data.get("choices") or []
    if not choices:
        return None, "empty_choices"
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # 少数兼容端点返回分段结构
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    content = (content or "").strip()
    if not content:
        # 推理型模型可能只回 reasoning_content，那是思维链，不能当总结正文。
        return None, "empty_content"
    return content, ""


async def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 2500,
                   temperature: float = 0.2) -> Tuple[Optional[str], Optional[str]]:
    endpoints = load_endpoints()
    if not endpoints:
        return None, "未配置任何 LLM 端点"

    large_request = max_tokens >= 2000
    budget = LARGE_REQUEST_BUDGET if large_request else SMALL_REQUEST_BUDGET
    deadline = time.monotonic() + budget - FINALIZATION_MARGIN
    candidates = endpoints[:MAX_ATTEMPTS]
    attempts = 3 if large_request else 2
    used = 0
    last_error = ""

    for index, endpoint in enumerate(candidates):
        if used >= attempts:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            last_error = last_error or "总预算耗尽"
            break
        if _circuit_open(endpoint["id"]):
            logger.info("跳过熔断节点 %s", endpoint["id"])
            continue

        slots = max(1, attempts - used)
        node_timeout = min(float(endpoint.get("timeout") or 40.0), max(1.0, remaining / slots))
        started = time.monotonic()
        used += 1
        content, error = await _call_one(
            endpoint, system_prompt, user_prompt, max_tokens, temperature, node_timeout
        )
        elapsed = time.monotonic() - started
        if content:
            _record_success(endpoint["id"])
            logger.info(
                "LLM 实际使用节点: %s (%s); fallback_index=%s; elapsed=%.1fs",
                endpoint["name"], endpoint["model"], index, elapsed,
            )
            return content, None

        _record_failure(endpoint["id"])
        last_error = error or "unknown_error"
        logger.warning(
            "LLM 节点失败 node=%s outcome=%s elapsed=%.1fs",
            endpoint["id"], last_error, elapsed,
        )

    return None, last_error or "全部模型节点均不可用"


def describe_chain() -> List[Dict[str, Any]]:
    endpoints = load_endpoints()
    status = circuit_status()
    return [
        {
            "id": ep["id"],
            "name": ep["name"],
            "model": ep["model"],
            "key_ready": bool(_resolve_key(ep)),
            "circuit": status.get(ep["id"], ""),
        }
        for ep in endpoints
    ]
