"""离线单测：不联网、不连接 Telegram。

运行：
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import time
import unittest

# 必须在导入 config/storage 之前指定临时数据目录
_TMP = tempfile.mkdtemp(prefix="summary-bot-test-")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:TEST-TOKEN-REPLACE-ME")
os.environ.setdefault("OWNER_ID", "123456789")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import args as arg_parser  # noqa: E402
import config  # noqa: E402
import storage  # noqa: E402
import summarize  # noqa: E402
from scheduler import slot_window_end  # noqa: E402
from services.rich import rich_to_html, sanitize_rich  # noqa: E402

LINK_PREFIX = "c/1234567890"


def make_message(message_id, text, user_id=1, user_name="张三", reply_to=None):
    return {
        "message_id": message_id,
        "user_id": user_id,
        "user_name": user_name,
        "text": text,
        "reply_to_msg_id": reply_to,
        "created_at": time.time(),
    }


class ParseSummaryArgTests(unittest.TestCase):
    def test_default(self):
        mode, value, _desc = arg_parser.parse_summary_arg("")
        self.assertEqual((mode, value), ("count", config.DEFAULT_MESSAGE_LIMIT))

    def test_count_capped_for_member(self):
        _mode, value, desc = arg_parser.parse_summary_arg("99999", is_privileged=False)
        self.assertEqual(value, config.MEMBER_MAX_MESSAGES)
        self.assertIn("上限", desc)

    def test_count_not_capped_for_admin(self):
        _mode, value, _desc = arg_parser.parse_summary_arg("99999", is_privileged=True)
        self.assertEqual(value, config.PRIVILEGED_MAX_MESSAGES)

    def test_hours_capped_for_member(self):
        _mode, value, _desc = arg_parser.parse_summary_arg("720h", is_privileged=False)
        self.assertEqual(value, int(config.MEMBER_MAX_HOURS * 3600))

    def test_yesterday_respects_member_cap(self):
        _mode, value, _desc = arg_parser.parse_summary_arg("昨天", is_privileged=False)
        self.assertLessEqual(value, int(config.MEMBER_MAX_HOURS * 3600))

    def test_minutes(self):
        mode, value, _desc = arg_parser.parse_summary_arg("30m")
        self.assertEqual((mode, value), ("time", 1800))

    def test_unknown_falls_back_to_default(self):
        mode, value, _desc = arg_parser.parse_summary_arg("随便说点什么")
        self.assertEqual((mode, value), ("count", config.DEFAULT_MESSAGE_LIMIT))


class ParseScheduleTests(unittest.TestCase):
    def test_autosum_daily_points(self):
        ok, payload, _msg = arg_parser.parse_autosum_args(["12:00,22:00", "6h"])
        self.assertTrue(ok)
        self.assertEqual(payload["time_points"], ["12:00", "22:00"])
        self.assertEqual(payload["lookback_hours"], 6.0)

    def test_autosum_interval(self):
        ok, payload, _msg = arg_parser.parse_autosum_args(["4h", "4h"])
        self.assertTrue(ok)
        self.assertEqual(payload["mode"], "interval")
        self.assertEqual(payload["interval_hours"], 4.0)

    def test_autosum_off(self):
        ok, payload, _msg = arg_parser.parse_autosum_args(["off"])
        self.assertTrue(ok)
        self.assertFalse(payload["enabled"])

    def test_autosum_rejects_garbage(self):
        ok, _payload, _msg = arg_parser.parse_autosum_args(["abc"])
        self.assertFalse(ok)

    def test_autodaily_defaults_to_12h(self):
        ok, payload, _msg = arg_parser.parse_autodaily_args(["08:00,20:00"])
        self.assertTrue(ok)
        self.assertEqual(payload["lookback_hours"], 12.0)

    def test_autodaily_out_of_range(self):
        ok, _payload, _msg = arg_parser.parse_autodaily_args(["08:00", "999h"])
        self.assertFalse(ok)


class SlotTests(unittest.TestCase):
    def test_daily_slot(self):
        end = slot_window_end("daily:2026-01-01_22:00")
        self.assertIsNotNone(end)
        self.assertGreater(end, 0)

    def test_interval_slot_requires_matching_schedule(self):
        schedule = {"last_run_timestamp": 1767225600.0}
        due = 1767225600.0 + 14400
        end = slot_window_end(f"interval:14400:{int(due // 14400)}", schedule)
        self.assertIsNotNone(end)
        self.assertEqual(end, due)

    def test_bad_slot_returns_none(self):
        self.assertIsNone(slot_window_end("garbage"))


class CleanCardTests(unittest.TestCase):
    def test_strips_think_block(self):
        raw = "先想想怎么总结\n<details><summary>🎬 话题</summary>\n• 内容\n</details>\n多余的话"
        cleaned = summarize._clean_card_content(raw)
        self.assertTrue(cleaned.startswith("<details>"))
        self.assertTrue(cleaned.endswith("</details>"))
        self.assertNotIn("先想想", cleaned)

    def test_strips_fake_thinking_card(self):
        raw = (
            "<details><summary>💭 思考与分析思路</summary>\n内部草稿\n</details>\n"
            "<details><summary>🎬 正片话题</summary>\n• 内容\n</details>"
        )
        cleaned = summarize._clean_card_content(raw)
        self.assertNotIn("思考与分析思路", cleaned)
        self.assertIn("正片话题", cleaned)

    def test_strips_code_fence(self):
        raw = "```html\n<details><summary>🎬 话题</summary>\n• 内容\n</details>\n```"
        cleaned = summarize._clean_card_content(raw)
        self.assertFalse(cleaned.startswith("```"))
        self.assertIn("<details>", cleaned)


class SourceNormalizationTests(unittest.TestCase):
    def test_keeps_only_verified_source(self):
        content = "• 事实一 [来源](https://t.me/c/1234567890/101) [来源](https://t.me/c/1234567890/999)"
        normalized = summarize._normalize_summary_sources(content, {101}, LINK_PREFIX)
        self.assertIn("/101", normalized)
        self.assertNotIn("/999", normalized)

    def test_removes_foreign_prefix(self):
        content = "• 事实 [来源](https://t.me/c/9999999999/101)"
        normalized = summarize._normalize_summary_sources(content, {101}, LINK_PREFIX)
        self.assertNotIn("t.me", normalized)

    def test_keeps_first_of_multiple(self):
        content = "• 事实 [来源](https://t.me/c/1234567890/101) [来源](https://t.me/c/1234567890/102)"
        normalized = summarize._normalize_summary_sources(content, {101, 102}, LINK_PREFIX)
        self.assertEqual(normalized.count("[来源]"), 1)
        self.assertIn("/101", normalized)


class DailyAttributionTests(unittest.TestCase):
    def setUp(self):
        self.messages = [
            make_message(101, "服务器今天升级到 2.0 版本，晚上八点重启"),
            make_message(102, "收到，我这边先备份", user_id=2, user_name="李四", reply_to=101),
        ]
        _code_to_name, self.message_to_code = summarize._build_daily_speaker_maps(self.messages)

    def test_valid_item_survives(self):
        content = "1. 服务器升级\n👉 升级到 2.0 版本，晚上重启 [来源](https://t.me/c/1234567890/101)"
        result = summarize._validate_daily_report_attribution(content, self.messages, self.message_to_code)
        self.assertIn("服务器升级", result)
        self.assertIn("U1", result)

    def test_unknown_source_dropped(self):
        content = "1. 编造话题\n👉 不存在的内容 [来源](https://t.me/c/1234567890/999)"
        result = summarize._validate_daily_report_attribution(content, self.messages, self.message_to_code)
        self.assertEqual(result, "")

    def test_unsupported_number_dropped(self):
        content = "1. 服务器升级到 9.9\n👉 全新版本发布 [来源](https://t.me/c/1234567890/101)"
        result = summarize._validate_daily_report_attribution(content, self.messages, self.message_to_code)
        self.assertEqual(result, "")

    def test_unsupported_body_number_falls_back_to_source(self):
        content = "1. 服务器升级\n👉 升级到 9.9 版本 [来源](https://t.me/c/1234567890/101)"
        result = summarize._validate_daily_report_attribution(content, self.messages, self.message_to_code)
        self.assertIn("服务器今天升级到 2", result)
        self.assertNotIn("9.9", result)

    def test_merge_dedupes_by_source(self):
        first = "1. A\n👉 内容 [来源](https://t.me/c/1234567890/101)"
        second = "1. A 重复\n👉 内容 [来源](https://t.me/c/1234567890/101)\n\n2. B\n👉 内容 [来源](https://t.me/c/1234567890/102)"
        merged = summarize._merge_daily_validated_reports(first, second)
        self.assertEqual(merged.count("[来源]"), 2)


class ChunkTests(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(len(summarize.split_summary_chunks("abc")), 1)

    def test_splits_on_details_boundary(self):
        block = "<details><summary>标题</summary>\n" + "• 内容\n" * 200 + "</details>\n"
        text = block * 3
        chunks = summarize.split_summary_chunks(text, max_chars=1000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1200)


class RichRenderTests(unittest.TestCase):
    def test_details_become_expandable_blockquote(self):
        html_text = rich_to_html("<details><summary>🎬 话题</summary>\n• 内容\n</details>")
        self.assertIn("<blockquote expandable>", html_text)
        self.assertIn("<b>🎬 话题</b>", html_text)
        self.assertNotIn("<details>", html_text)

    def test_sanitize_blocks_unlisted_url(self):
        content = "• 事实 [来源](https://evil.example.com/x)"
        cleaned = sanitize_rich(content, allowed_urls={"https://t.me/c/1234567890/101"})
        self.assertNotIn("evil.example.com", cleaned)

    def test_sanitize_keeps_allowed_url(self):
        url = "https://t.me/c/1234567890/101"
        cleaned = sanitize_rich(f"• 事实 [来源]({url})", allowed_urls={url})
        self.assertIn(url, cleaned)


class StorageTests(unittest.TestCase):
    def test_roundtrip(self):
        storage.save_message(chat_id=-1001234567890, message_id=1, user_id=1, user_name="张三", text="第一条")
        storage.save_message(chat_id=-1001234567890, message_id=2, user_id=2, user_name="李四", text="第二条", reply_to_msg_id=1)
        recent = storage.get_recent_messages(-1001234567890, 10)
        ids = [m["message_id"] for m in recent]
        self.assertEqual(ids[-2:], [1, 2])
        self.assertEqual(recent[-1]["reply_to_msg_id"], 1)

        window = storage.get_messages_by_time(-1001234567890, 3600)
        self.assertGreaterEqual(len(window), 2)

    def test_duplicate_message_updates(self):
        storage.save_message(chat_id=-1001234567890, message_id=3, user_id=1, user_name="张三", text="旧")
        storage.save_message(chat_id=-1001234567890, message_id=3, user_id=1, user_name="张三", text="新")
        recent = [m for m in storage.get_recent_messages(-1001234567890, 20) if m["message_id"] == 3]
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["text"], "新")

    def test_empty_text_ignored(self):
        before = storage.count_messages(-1001234567890)
        storage.save_message(chat_id=-1001234567890, message_id=4, user_id=1, user_name="张三", text="   ")
        self.assertEqual(storage.count_messages(-1001234567890), before)


class StateTests(unittest.TestCase):
    def test_authorize_and_features(self):
        gid = -1009999999999
        config.authorize_group(gid, "测试群")
        self.assertTrue(config.is_authorized(gid))
        self.assertTrue(config.is_feature_enabled(gid, "summary"))
        config.set_feature(gid, "summary", False)
        self.assertFalse(config.is_feature_enabled(gid, "summary"))

    def test_schedule_roundtrip(self):
        gid = -1009999999998
        config.authorize_group(gid, "测试群2")
        config.set_schedule(gid, {"enabled": True, "mode": "daily_time", "time_points": ["22:00"], "lookback_hours": 4.0})
        schedule = config.get_schedule(gid)
        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["time_points"], ["22:00"])

    def test_daily_roundtrip(self):
        gid = -1009999999997
        config.authorize_group(gid, "测试群3")
        config.set_daily(gid, {"enabled": True, "time_points": ["08:00"], "lookback_hours": 12.0})
        report = config.get_daily(gid)
        self.assertTrue(report["enabled"])
        self.assertEqual(report["time_points"], ["08:00"])

    def test_revoke_cleans_all(self):
        gid = -1009999999996
        config.authorize_group(gid, "测试群4")
        config.set_feature(gid, "daily_report", False)
        config.set_daily(gid, {"enabled": True})
        config.revoke_group(gid)
        self.assertFalse(config.is_authorized(gid))
        self.assertEqual(config.get_daily(gid)["enabled"], False)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rich_rejected_falls_back_to_html(self):
        from telegram.error import BadRequest
        from services.delivery import send_rich_or_html

        class FakeBot:
            def __init__(self):
                self.sent = []
                self.rich_calls = 0

            async def do_api_request(self, method, api_kwargs=None):
                self.rich_calls += 1
                raise BadRequest("rich message not supported")

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)
                return None

        bot = FakeBot()
        await send_rich_or_html(bot, -1001234567890, "<details><summary>标题</summary>\n• 内容\n</details>")
        self.assertEqual(bot.rich_calls, 1)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("<blockquote expandable>", bot.sent[0]["text"])

    async def test_network_error_is_uncertain(self):
        from telegram.error import NetworkError
        from services.delivery import DeliveryUncertain, send_rich_or_html

        class FakeBot:
            async def do_api_request(self, method, api_kwargs=None):
                raise NetworkError("timed out")

            async def send_message(self, **kwargs):
                raise AssertionError("不确定结果时不应降级重发")

        with self.assertRaises(DeliveryUncertain):
            await send_rich_or_html(FakeBot(), -1001234567890, "内容")

    async def test_partial_chunk_failure_marks_uncertain(self):
        from telegram.error import NetworkError
        from services.delivery import DeliveryUncertain, send_chunks

        class FakeBot:
            def __init__(self):
                self.calls = 0

            async def do_api_request(self, method, api_kwargs=None):
                self.calls += 1
                if self.calls > 1:
                    raise NetworkError("second chunk timeout")
                return None

            async def send_message(self, **kwargs):
                raise AssertionError("不确定结果时不应降级重发")

        with self.assertRaises(DeliveryUncertain):
            await send_chunks(FakeBot(), -1001234567890, ["第一段", "第二段"])


class SlotDueTests(unittest.TestCase):
    def test_already_run_slot_is_skipped(self):
        from scheduler import _daily_slot_due
        import datetime
        now = datetime.datetime(2026, 1, 1, 22, 0, 10, tzinfo=config.BEIJING_TZ)
        target = now.replace(second=0)
        self.assertFalse(_daily_slot_due(target, now, target, "2026-01-01_22:00", "2026-01-01_22:00", False))

    def test_crossing_target_is_due(self):
        from scheduler import _daily_slot_due
        import datetime
        now = datetime.datetime(2026, 1, 1, 22, 0, 10, tzinfo=config.BEIJING_TZ)
        target = now.replace(second=0)
        last_check = target - datetime.timedelta(seconds=30)
        self.assertTrue(_daily_slot_due(target, now, last_check, "", "2026-01-01_22:00", False))

    def test_past_slot_without_retry_is_skipped(self):
        from scheduler import _daily_slot_due
        import datetime
        now = datetime.datetime(2026, 1, 1, 23, 0, 0, tzinfo=config.BEIJING_TZ)
        target = now.replace(hour=22)
        self.assertFalse(_daily_slot_due(target, now, now - datetime.timedelta(minutes=1), "", "2026-01-01_22:00", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
