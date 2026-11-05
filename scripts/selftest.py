#!/usr/bin/env python3
"""离线自检：不连 Telegram，用本地 mock LLM 跑通「取数 → 总结 → 校验 → 渲染」。

用途：部署后先跑这个，确认代码、SQLite、提示词、来源校验、HTML 降级都正常，
再填真实 Token 上线。也可以用来对比不同模型的实际输出。

用法：
    python scripts/selftest.py
    python scripts/selftest.py --endpoint https://api.example.com/v1/chat/completions \
        --model your-model --key-env LLM_API_KEY
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CHAT_ID = -1001234567890
LINK_PREFIX = "c/1234567890"

SUMMARY_FIXTURE = """<details>
<summary>🖥 服务器升级</summary>

• 张三提到服务器今天升级到 2.0 版本，计划晚上八点重启 [来源](https://t.me/c/1234567890/101)
• 李四准备先做备份再配合升级 [来源](https://t.me/c/1234567890/102)
</details>

<details>
<summary>📌 核心共识与后续</summary>

• 先备份，晚八点窗口内完成升级。
</details>"""

DAILY_FIXTURE = """1. 服务器升级窗口
👉 升级到 2.0 版本，计划晚上八点重启 [来源](https://t.me/c/1234567890/101)

2. 备份先行
👉 先完成备份再配合升级 [来源](https://t.me/c/1234567890/102)

3. 监控补齐
👉 升级后补上磁盘告警 [来源](https://t.me/c/1234567890/103)

4. 回滚方案
👉 保留旧版本镜像以便回滚 [来源](https://t.me/c/1234567890/104)

5. 编造的话题
👉 这条来源不存在，应该被丢弃 [来源](https://t.me/c/1234567890/999)

6. 数字无据
👉 升级后性能提升 88 倍 [来源](https://t.me/c/1234567890/105)"""


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        messages = payload.get("messages") or []
        system_prompt = messages[0].get("content", "") if messages else ""
        user_prompt = messages[-1].get("content", "") if messages else ""
        if "补选要求" in user_prompt:
            content = "NO_REPORTABLE_TOPICS"
        elif "群组日报编辑" in system_prompt:
            content = DAILY_FIXTURE
        else:
            content = SUMMARY_FIXTURE
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"total_tokens": 100},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _start_mock() -> tuple:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/v1/chat/completions"


def _prepare(workdir: Path, endpoint: str, model: str, key_env: str) -> None:
    os.environ["DATA_DIR"] = str(workdir)
    os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:TEST-TOKEN-REPLACE-ME"
    os.environ["OWNER_ID"] = "123456789"
    endpoints_file = workdir / "llm_endpoints.json"
    endpoints_file.write_text(json.dumps([{
        "id": "selftest",
        "name": "自检节点",
        "url": endpoint,
        "key_env": key_env,
        "model": model,
        "timeout": 30,
    }], ensure_ascii=False), encoding="utf-8")
    os.environ["LLM_ENDPOINTS_FILE"] = str(endpoints_file)
    os.environ.setdefault(key_env, "selftest-key")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="", help="真实端点；留空则使用本地 mock")
    parser.add_argument("--model", default="selftest-model")
    parser.add_argument("--key-env", default="LLM_API_KEY")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="summary-bot-selftest-"))
    server = None
    endpoint = args.endpoint
    if not endpoint:
        server, endpoint = _start_mock()

    _prepare(workdir, endpoint, args.model, args.key_env)
    import storage
    import summarize
    from services.rich import rich_to_html

    samples = [
        (101, 1, "张三", "服务器今天升级到 2.0 版本，计划晚上八点重启", None),
        (102, 2, "李四", "收到，我先做备份再配合", 101),
        (103, 1, "张三", "升级后把磁盘告警也补上", 101),
        (104, 3, "王五", "保留旧版本镜像，方便回滚", 101),
        (105, 4, "赵六", "晚上八点我有空，可以盯监控", 101),
        (106, 2, "李四", "备份完成了，随时可以开始", 104),
    ]
    for message_id, user_id, name, text, reply_to in samples:
        storage.save_message(CHAT_ID, message_id, user_id, name, text, reply_to)

    messages = storage.get_messages_by_time(CHAT_ID, 3600)
    assert len(messages) == len(samples), f"取数异常：{len(messages)}"

    async def run():
        summary, summary_err = await summarize.summarize_messages(
            chat_id=CHAT_ID, chat_title="自检群", target_desc="前 1 小时", messages=messages,
        )
        daily, daily_err = await summarize.summarize_messages(
            chat_id=CHAT_ID, chat_title="自检群", target_desc="前 1 小时", messages=messages,
            is_scheduled=True, style="daily",
        )
        return summary, summary_err, daily, daily_err

    summary, summary_err, daily, daily_err = asyncio.run(run())
    if server:
        server.shutdown()

    ok = True

    def check(name, condition, detail=""):
        nonlocal ok
        mark = "PASS" if condition else "FAIL"
        if not condition:
            ok = False
        print(f"[{mark}] {name}{(' — ' + detail) if detail else ''}")

    check("总结生成", bool(summary), str(summary_err or ""))
    if summary:
        check("总结带 #总结 标签", summary.startswith("#总结"))
        check("保留真实来源", f"https://t.me/{LINK_PREFIX}/101" in summary)
        check("无思考残留", "思考" not in summary and "think" not in summary.lower())
        html_text = rich_to_html(summary)
        check("HTML 降级为折叠引用", "<blockquote expandable>" in html_text and "<details>" not in html_text)

    check("日报生成", bool(daily), str(daily_err or ""))
    if daily:
        check("日报带 #日报 标签", daily.startswith("#日报"))
        check("丢弃编造来源", "/999" not in daily)
        check("丢弃无据数字", "88 倍" not in daily)
        check("条目数在 3~8 之间", 3 <= daily.count("[来源]") <= 8, f"实际 {daily.count('[来源]')} 条")

    print("\n----- 总结输出 -----")
    print(summary or f"(空) 错误：{summary_err}")
    print("\n----- 日报输出 -----")
    print(daily or f"(空) 错误：{daily_err}")
    print(f"\n临时目录：{workdir}")
    print("结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
