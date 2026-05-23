import runpy
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from insight_zh.domain.session import NormalizedSession


ROOT = Path(__file__).resolve().parents[1]


class DailyLoaderIntegrationTest(unittest.TestCase):
    def test_load_sessions_delegates_to_shared_loader(self):
        module_globals = runpy.run_path(str(ROOT / "di-review.py"), run_name="not_main")
        load_sessions = module_globals["load_sessions"]
        fake_session = NormalizedSession(
            session_id="sess-daily-001",
            project_path="/tmp/demo-project",
            start_time=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc),
            report_date=date(2026, 5, 20),
            duration_minutes=30,
            user_message_count=3,
            assistant_message_count=4,
            tool_counts={"Read": 2, "Bash": 1},
            git_pushes=1,
            first_prompt="先帮我看看这个目录",
            facet={"outcome": "mostly_achieved"},
            raw_jsonl={
                "tool_counts": {"Read": 2, "Bash": 1},
                "first_user": "先帮我看看这个目录",
                "first_ts": datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
                "last_ts": datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc),
                "user_msgs": 3,
                "assistant_msgs": 4,
                "interruptions": 0,
                "compact_count": 0,
                "bash_commands": [],
                "read_files": [],
                "edited_files": [],
                "written_files": [],
                "message_turns": [],
                "tools_by_ts": [],
                "user_msg_ts": [],
                "user_msg_texts": [],
            },
            jsonl_path=Path("/tmp/demo-project/sess-daily-001.jsonl"),
        )

        calls = []

        def fake_loader(start_date, end_date, claude_dir):
            calls.append((start_date, end_date, claude_dir))
            return [fake_session]

        load_sessions.__globals__["load_sessions_from_workspace"] = fake_loader

        with tempfile.TemporaryDirectory() as tmp:
            load_sessions.__globals__["PROJECTS_DIR"] = Path(tmp) / "projects"
            load_sessions.__globals__["FACETS_DIR"] = Path(tmp) / "usage-data" / "facets"
            load_sessions.__globals__["CLAUDE_DIR"] = Path(tmp)
            items = load_sessions(date(2026, 5, 20), date(2026, 5, 20))

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["session_id"], "sess-daily-001")
        self.assertEqual(items[0]["parsed"]["tool_counts"]["Read"], 2)
        self.assertEqual(items[0]["facet"]["outcome"], "mostly_achieved")
        self.assertEqual(items[0]["session_date"], date(2026, 5, 20))


if __name__ == "__main__":
    unittest.main()