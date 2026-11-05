"""总结 / 日报的生成核心。

流程：取数 → 构建带 msg_id 与回复链的对话 → 调模型 → 清洗思考痕迹 →
来源白名单校验 → 日报再做人物归属与数字校验（不足则补选一次）→ 渲染。
"""
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from prompts import DAILY_REPORT_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from services.llm import call_llm
from services.rich import escape_rich_text, sanitize_rich

logger = logging.getLogger("summary-bot.summarize")

DAILY_REPORT_MAX_TOPICS = 8
DAILY_REPORT_MIN_TOPICS = 5
DAILY_REPORT_NO_CONTENT = "daily_report_no_content"
MAX_DIALOG_CHARS = 48000
TARGET_DIALOG_CHARS = 47000


def link_prefix_for(chat_id) -> str:
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"c/{raw[4:]}"
    if raw.startswith("-"):
        return f"c/{raw[1:]}"
    return f"c/{raw}"


def _clean_card_content(content: str) -> str:
    """净化卡片内容：剥代码块、清思考标签、砍掉卡片外的自言自语。"""
    if not content:
        return ""
    content = content.strip()

    if content.startswith("```html"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = re.sub(r"^```[a-zA-Z]*\n", "", content)
    content = re.sub(r"\n```$", "", content)
    content = content.strip()

    # 旧版 blockquote 卡片统一转成 details
    content = re.sub(r"<blockquote expandable><b>(.*?)</b>", r"<details>\n<summary>\1</summary>", content)
    content = content.replace("</blockquote>", "</details>")

    # 显式思考标签
    content = re.sub(
        r"<(?:think|thought|thinking|reasoning|reason)>[\s\S]*?</(?:think|thought|thinking|reasoning|reason)>",
        "", content, flags=re.IGNORECASE,
    )
    content = re.sub(r"```(?:thought|thinking|reasoning|think)[\s\S]*?```", "", content, flags=re.IGNORECASE)

    # 伪装成折叠卡片的思考 / 分析过程
    content = re.sub(
        r"<details>\s*<summary>[^<]*(?:思考|思维|thought|thinking|reasoning|思路梳理|分析思路|分析过程|分析记录|推理|推导|内心OS|思维链|Chain-of-thought|CoT)[^<]*</summary>[\s\S]*?</details>",
        "", content, flags=re.IGNORECASE,
    )

    if "<details" in content:
        content = re.sub(r"<(?:think|thought)>[\s\S]*?(?=<details)", "", content, flags=re.IGNORECASE)
        first_idx = content.find("<details")
        if first_idx != -1:
            content = content[first_idx:]
        last_idx = content.rfind("</details>")
        if last_idx != -1:
            content = content[: last_idx + len("</details>")]
    else:
        content = re.sub(r"<(?:think|thought)>[\s\S]*?(?=\n\n|$)", "", content, flags=re.IGNORECASE)
        content = re.sub(
            r"^(\*{1,3})?(思考过程|思维过程|分析思路|Thinking Process|Thought Process)(\*{1,3})?[：:]?.*?(\n\n|\n|$)",
            "", content, flags=re.IGNORECASE,
        )

    return content.strip()


def _normalize_summary_sources(
    content: str,
    allowed_message_ids: Optional[set] = None,
    link_prefix: Optional[str] = None,
) -> str:
    """每条要点只保留一个、且能追溯到本次记录的直接来源。"""
    source_link = re.compile(r"\[来源\]\((https?://[^)\s]+)\)")
    telegram_message_url = re.compile(r"https?://t\.me/(?:c/)?[^/\s]+/(\d+)(?:\?[^)\s]+)?$")
    allowed = set(allowed_message_ids or ())

    def normalize_source(url: str) -> str:
        if allowed_message_ids is None or not link_prefix:
            return ""
        parsed = telegram_message_url.fullmatch(url)
        if not parsed:
            return ""
        message_id = int(parsed.group(1))
        if message_id not in allowed or not re.fullmatch(
            r"https?://t\.me/" + re.escape(link_prefix) + r"/" + str(message_id) + r"(?:\?[^)\s]+)?", url
        ):
            return ""
        return f"[来源](https://t.me/{link_prefix}/{message_id})"

    normalized = []
    for line in content.splitlines():
        seen = False

        def keep_first(match):
            nonlocal seen
            candidate = normalize_source(match.group(1))
            if not candidate or seen:
                return ""
            seen = True
            return candidate

        normalized.append(source_link.sub(keep_first, line))
    return "\n".join(normalized).strip()


def _restore_speaker_codes(content: str, speaker_codes: Dict[str, str]) -> str:
    """把提示词里的 U12 等代号还原成真实昵称。"""
    if not content or not speaker_codes:
        return content
    replacements = {
        str(code).strip(): escape_rich_text(str(display_name).strip())
        for display_name, code in speaker_codes.items()
        if str(code or "").strip() and str(display_name or "").strip()
        and str(display_name).strip() != str(code).strip()
    }
    if not replacements:
        return content
    codes = "|".join(sorted((re.escape(code) for code in replacements), key=len, reverse=True))
    pattern = re.compile(rf"(?<![A-Za-z0-9_])(?:{codes})(?![A-Za-z0-9_])")
    return pattern.sub(lambda match: replacements.get(match.group(0), match.group(0)), content)


def _build_daily_speaker_maps(messages: List[Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[int, str]]:
    """为日报建立稳定身份代码，并记录每条消息的真实作者代码。"""
    identity_to_code: Dict[Tuple[str, str], str] = {}
    code_to_name: Dict[str, str] = {}
    message_to_code: Dict[int, str] = {}

    for message in messages:
        name = str(message.get("user_name") or "群友").strip() or "群友"
        raw_user_id = message.get("user_id")
        if raw_user_id not in (None, 0, "0", ""):
            identity = ("id", str(raw_user_id))
        else:
            identity = ("name", name)

        code = identity_to_code.get(identity)
        if not code:
            code = f"U{len(identity_to_code) + 1}"
            identity_to_code[identity] = code
            code_to_name[code] = name

        try:
            message_to_code[int(message.get("message_id"))] = code
        except (TypeError, ValueError):
            continue

    return code_to_name, message_to_code


def _restore_daily_speaker_codes(content: str, code_to_name: Dict[str, str]) -> str:
    if not content or not code_to_name:
        return content
    codes = "|".join(sorted((re.escape(code) for code in code_to_name), key=len, reverse=True))
    pattern = re.compile(rf"(?<![A-Za-z0-9_])(?:{codes})(?![A-Za-z0-9_])")
    return pattern.sub(lambda match: escape_rich_text(code_to_name.get(match.group(0), match.group(0))), content)


def _daily_source_excerpt(message: Dict[str, Any], message_by_id: Dict[int, Dict[str, Any]], max_chars: int = 220) -> str:
    """只从已验证的真实来源消息生成日报正文。"""
    raw_text = str(message.get("text") or "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    if len(lines) > 1:
        picked = []
        for line in lines:
            candidate = "；".join(picked + [line])
            if picked and len(candidate) > max_chars:
                break
            picked.append(line)
            if len(picked) >= 4:
                break
        text = "；".join(picked)
    else:
        text = lines[0]

    reply_id = message.get("reply_to_msg_id")
    if len(text) <= 16 and reply_id is not None:
        try:
            parent = message_by_id.get(int(reply_id)) or {}
        except (TypeError, ValueError):
            parent = {}
        parent_text = re.sub(r"\s+", " ", str(parent.get("text") or "")).strip()
        if parent_text:
            parent_text = parent_text[:80] + ("…" if len(parent_text) > 80 else "")
            text = f"回复「{parent_text}」：{text}"

    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _validate_daily_report_attribution(
    content: str,
    messages: List[Dict[str, Any]],
    message_to_code: Dict[int, str],
) -> str:
    """硬校验日报的“主发言人 → 来源消息”归属，宁可丢条也不张冠李戴。"""
    if not content:
        return ""

    message_by_id = {}
    for message in messages:
        try:
            message_by_id[int(message.get("message_id"))] = message
        except (TypeError, ValueError):
            continue

    block_re = re.compile(
        r"(?ms)^\s*(?:(?:\*\*)?\d+\.\s*|#{1,6}\s+)"
        r"(?P<title>[^\n]+?)(?:\*\*)?\s*\n"
        r"\s*👉\s*(?P<body>.*?)"
        r"(?=^\s*(?:(?:\*\*)?\d+\.\s+|#{1,6}\s+)|\Z)"
    )
    source_re = re.compile(r"\[来源\]\((?P<url>https?://[^)\s]+/(?P<msg_id>\d+)(?:\?[^)\s]+)?)\)")
    number_re = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")

    validated = []
    for match in block_re.finditer(content.strip()):
        title = match.group("title").strip()
        raw_body = match.group("body").strip()
        sources = list(source_re.finditer(raw_body))
        if len(sources) != 1:
            logger.warning("日报归属校验丢条：来源数=%s 标题=%r", len(sources), title)
            continue

        source = sources[0]
        source_id = int(source.group("msg_id"))
        code = message_to_code.get(source_id)
        if not code:
            logger.warning("日报归属校验丢条：未知来源 msg_id=%s", source_id)
            continue

        source_message = message_by_id.get(source_id) or {}
        source_excerpt = _daily_source_excerpt(source_message, message_by_id)
        if not source_excerpt or re.fullmatch(r"\[(?:图片|视频|GIF动图|贴纸[^\]]*)\]", source_excerpt):
            logger.warning("日报归属校验丢条：来源正文不可用 msg_id=%s", source_id)
            continue

        evidence = str(source_message.get("text") or "")
        reply_id = source_message.get("reply_to_msg_id")
        try:
            if reply_id is not None:
                evidence += " " + str(message_by_id.get(int(reply_id), {}).get("text") or "")
        except (TypeError, ValueError):
            pass

        body = source_re.sub("", raw_body)
        body = re.sub(r"</?[^>]+>", "", body)
        body = re.sub(r"^\s*👉\s*", "", body)
        body = re.sub(r"\bU\d+\s*[：:]\s*", "", body)
        body = re.sub(r"\bU\d+\b", "相关群友", body)
        body = re.sub(r"^(?:有|某|一位)群友", "", body).lstrip("，。； ")
        body = re.sub(r"^群内围绕", "围绕", body)
        body = re.sub(r"\s+", " ", body).strip(" *_-：:")
        if len(body) > 180:
            body = body[:180].rstrip() + "…"
        if not body:
            body = escape_rich_text(source_excerpt)

        # 标题和概括中的数字必须能从来源或其直接回复的上一条消息找到
        claim_numbers = set(number_re.findall(title + " " + body))
        evidence_numbers = set(number_re.findall(evidence))
        if not claim_numbers.issubset(evidence_numbers):
            if set(number_re.findall(title)).issubset(evidence_numbers):
                logger.warning("日报正文数字无据，回退原文：标题=%r", title)
                body = escape_rich_text(source_excerpt)
            else:
                logger.warning("日报标题数字无据，丢条：标题=%r", title)
                continue

        title = title.strip("*_# ").strip()
        validated.append((title, code, body, source.group(0)))

    if not validated:
        logger.warning("日报归属校验拒绝了全部条目；原始输出前 2000 字=%r", content[:2000])
        return ""

    return "\n\n".join(
        f"{idx}. {title}\n👉 {code}：{body} {source}"
        for idx, (title, code, body, source) in enumerate(validated, 1)
    ).strip()


def _daily_validated_items(content: str) -> List[str]:
    return [part.strip() for part in re.split(r"(?m)^\d+\.\s+", content) if part.strip()]


def _merge_daily_validated_reports(original: str, supplement: str) -> str:
    """合并两轮结果，按来源与标题去重，最多保留 8 条。"""
    items, seen_sources, seen_titles = [], set(), set()
    for part in _daily_validated_items(original) + _daily_validated_items(supplement):
        source = re.search(r"\[来源\]\(https?://[^)\s]+/(\d+)(?:\?[^)\s]+)?\)", part)
        title = re.sub(r"\W+", "", part.split("\n", 1)[0]).casefold()
        if not source or source.group(1) in seen_sources or title in seen_titles:
            continue
        seen_sources.add(source.group(1))
        seen_titles.add(title)
        items.append(part)
        if len(items) == DAILY_REPORT_MAX_TOPICS:
            break
    return "\n\n".join(f"{i}. {part}" for i, part in enumerate(items, 1))


async def _refill_daily_report(content, *, sys_prompt, user_prompt, messages,
                               message_to_code, allowed_source_ids, link_prefix):
    """条目不足时做一次有边界的补选，校验规则完全相同。"""
    content = _merge_daily_validated_reports(content, "")
    count = len(_daily_validated_items(content))
    if count >= DAILY_REPORT_MIN_TOPICS:
        return content
    instructions = (
        "\n【补选要求】上一轮来源校验后只保留了 " + str(count) + " 个主题。请从上述记录中补选 "
        + str(DAILY_REPORT_MIN_TOPICS - count) + "～" + str(DAILY_REPORT_MAX_TOPICS - count)
        + " 个不同的新主题，只输出补选主题。已有主题及来源如下，不要重复其话题或换一个来源重复讲同一件事：\n"
        + (content or "无")
        + "\n每个主题必须有且仅有一个真实来源链接，标题和正文中的数字都必须由该来源或直接回复的上一条消息支持。"
        "缺乏足够有价值的真实话题时可以少选，绝不凑数。"
    )
    try:
        extra, _err = await asyncio.wait_for(
            call_llm(system_prompt=sys_prompt, user_prompt=user_prompt + instructions,
                     max_tokens=2500, temperature=0.1),
            timeout=35.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("日报补选失败 type=%s 已保留=%s", type(exc).__name__, count)
        return content
    if not extra:
        logger.warning("日报补选无内容 已保留=%s", count)
        return content
    if extra.strip() == "NO_REPORTABLE_TOPICS":
        return content
    extra = _normalize_summary_sources(_clean_card_content(extra), allowed_source_ids, link_prefix)
    extra = _validate_daily_report_attribution(extra, messages, message_to_code)
    merged = _merge_daily_validated_reports(content, extra)
    logger.info("日报补选完成 补选前=%s 补选后=%s", count, len(_daily_validated_items(merged)))
    return merged


async def summarize_messages(
    chat_id: int,
    chat_title: str,
    target_desc: str,
    messages: List[Dict[str, Any]],
    is_scheduled: bool = False,
    style: str = "interactive",
) -> Tuple[Optional[str], Optional[str]]:
    """返回 (可直接投递的富文本, 错误描述)。style='daily' 时走日报链路。"""
    if not messages:
        if style == "daily":
            return None, DAILY_REPORT_NO_CONTENT
        return "近期暂无足够的群聊发言可供总结哦～", None

    link_prefix = link_prefix_for(chat_id)

    # 历史数据里 user_id=0 的转发记录无法证明“当前群里是谁发的”，不参与日报归属。
    report_messages = messages
    if style == "daily":
        report_messages = [m for m in messages if m.get("user_id") not in (None, 0, "0", "")]
        if len(report_messages) < 3:
            return None, DAILY_REPORT_NO_CONTENT

    speaker_codes: Dict[str, str] = {}
    daily_code_to_name: Dict[str, str] = {}
    daily_message_to_code: Dict[int, str] = {}
    if style == "daily":
        daily_code_to_name, daily_message_to_code = _build_daily_speaker_maps(report_messages)

    dialog_lines: List[str] = []
    for message in report_messages:
        msg_id = message.get("message_id")
        user = message.get("user_name") or "群友"
        if style == "daily":
            try:
                code = daily_message_to_code[int(msg_id)]
            except (TypeError, ValueError, KeyError):
                continue
        else:
            if user not in speaker_codes:
                speaker_codes[user] = f"U{len(speaker_codes) + 1}"
            code = speaker_codes[user]

        text = (message.get("text") or "").replace("\n", " ").strip()
        if style == "daily" and len(text) > 260:
            text = text[:200] + " …（中段省略）… " + text[-50:]
        elif len(text) > 160:
            text = text[:160] + "..."

        reply_id = message.get("reply_to_msg_id")
        reply_info = f">{reply_id}" if reply_id else ""
        if style == "daily":
            dialog_lines.append(f"{msg_id}{reply_info}|{text}")
        else:
            dialog_lines.append(f"{msg_id}|{code}{reply_info}|{text}")

    # 超长时均匀抽样，保证整段时间都有代表样本
    raw_dialog = "\n".join(dialog_lines)
    if len(raw_dialog) > MAX_DIALOG_CHARS and len(dialog_lines) > 200:
        original_count = len(dialog_lines)
        keep = max(200, int(original_count * TARGET_DIALOG_CHARS / len(raw_dialog)))
        keep = min(original_count, keep)
        if keep < original_count:
            indexes = sorted({round(i * (original_count - 1) / (keep - 1)) for i in range(keep)})
            dialog_lines = [dialog_lines[i] for i in indexes]

    sampled_count = len(dialog_lines)
    speaker_legend = "; ".join(f"{code}={name}" for name, code in speaker_codes.items())
    dialog_text = "\n".join(dialog_lines)

    prompt_template = DAILY_REPORT_SYSTEM_PROMPT if style == "daily" else SUMMARY_SYSTEM_PROMPT
    sys_prompt = prompt_template.replace("{link_prefix}", link_prefix)
    sample_desc = (
        f"有效 {len(report_messages)} 条，均匀采样 {sampled_count} 条"
        if sampled_count < len(report_messages)
        else f"共 {len(report_messages)} 条"
    )
    if style == "daily":
        user_prompt = f"""【群聊名称】：{chat_title}
【时间/范围】：{target_desc}
【直链前缀】：{link_prefix}
【记录格式】：msg_id>被回复msg_id|内容；无 > 表示非回复。系统会按来源 msg_id 自动补全真实作者。
【聊天记录（{sample_desc}）】：
{dialog_text}
"""
    else:
        user_prompt = f"""【群聊名称】：{chat_title}
【时间/范围】：{target_desc}
【直链前缀】：{link_prefix}
【发言人映射】：{speaker_legend}
【记录格式】：msg_id|发言人代码>被回复msg_id|内容；无 > 表示非回复。记录按时间正序。
【聊天记录（{sample_desc}）】：
{dialog_text}
"""

    content, error = await call_llm(
        system_prompt=sys_prompt,
        user_prompt=user_prompt,
        max_tokens=2500,
        temperature=0.1 if style == "daily" else 0.3,
    )
    if not content:
        return None, error

    if style == "daily" and content.strip() == "NO_REPORTABLE_TOPICS":
        logger.info("日报没有可报道话题，正常跳过")
        return None, DAILY_REPORT_NO_CONTENT

    allowed_source_ids = set()
    for message in report_messages:
        candidates = (
            (message.get("message_id"),)
            if style == "daily"
            else (message.get("message_id"), message.get("reply_to_msg_id"))
        )
        for raw_source_id in candidates:
            try:
                if raw_source_id is not None:
                    allowed_source_ids.add(int(raw_source_id))
            except (TypeError, ValueError):
                continue

    content = _normalize_summary_sources(_clean_card_content(content), allowed_source_ids, link_prefix)
    if style == "daily":
        content = _validate_daily_report_attribution(content, report_messages, daily_message_to_code)
        content = await _refill_daily_report(
            content, sys_prompt=sys_prompt, user_prompt=user_prompt,
            messages=report_messages, message_to_code=daily_message_to_code,
            allowed_source_ids=allowed_source_ids, link_prefix=link_prefix,
        )
        if not content:
            return None, "日报人物/来源事实校验未通过，已阻止发送以避免张冠李戴"
        content = sanitize_rich(
            content,
            allowed_urls={f"https://t.me/{link_prefix}/{mid}" for mid in allowed_source_ids},
        )
        content = _restore_daily_speaker_codes(content, daily_code_to_name)
    else:
        content = sanitize_rich(
            content,
            allowed_urls={f"https://t.me/{link_prefix}/{mid}" for mid in allowed_source_ids},
        )
        content = _restore_speaker_codes(content, speaker_codes)

    sample_label = (
        f"{sampled_count}/{len(report_messages)} 条"
        if sampled_count < len(report_messages)
        else f"{len(report_messages)} 条"
    )
    if style == "daily":
        header = f"#日报 📰 **【{escape_rich_text(chat_title)}】群组日报（{escape_rich_text(target_desc)} / 样本 {sample_label}）**\n\n"
    elif is_scheduled:
        header = f"#总结 ⏰ **【{escape_rich_text(chat_title)}】群聊定时总结（{escape_rich_text(target_desc)} / 样本 {sample_label}）**\n\n"
    else:
        header = f"#总结 📊 **【{escape_rich_text(chat_title)}】群聊总结（{escape_rich_text(target_desc)} / 样本 {sample_label}）**\n\n"
    return header + content, None


def split_summary_chunks(text: str, max_chars: int = 3800) -> List[str]:
    """按折叠卡片边界拆包，避免击穿 Telegram 单条 4096 字符上限。"""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    blocks = re.split(r"(</details>\s*\n*)", text)
    current_chunk = ""

    for block in blocks:
        if len(current_chunk) + len(block) > max_chars and current_chunk:
            chunks.append(current_chunk.strip())
            current_chunk = block
        else:
            current_chunk += block

    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    if not chunks:
        chunks = [text]

    safe_chunks = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            safe_chunks.append(chunk)
            continue
        while len(chunk) > max_chars:
            cut = chunk.rfind(" ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            safe_chunks.append(chunk[:cut].strip())
            chunk = chunk[cut:].lstrip()
        if chunk:
            safe_chunks.append(chunk)
    return safe_chunks
