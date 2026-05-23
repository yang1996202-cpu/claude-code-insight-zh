import runpy
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from insight_zh.domain.session import NormalizedSession


ROOT = Path(__file__).resolve().parents[1]


class InsightLoaderIntegrationTest(unittest.TestCase):
    def test_load_data_from_jsonl_delegates_to_shared_loader(self):
        module_globals = runpy.run_path(str(ROOT / "insight-zh.py"), run_name="not_main")
        load_data_from_jsonl = module_globals["load_data_from_jsonl"]
        fake_session = NormalizedSession(
            session_id="sess-001",
            project_path="/tmp/demo-project",
            start_time=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 20, 10, 18, tzinfo=timezone.utc),
            report_date=date(2026, 5, 20),
            duration_minutes=18,
            user_message_count=4,
            assistant_message_count=7,
            tool_counts={"Read": 3, "Edit": 2},
            input_tokens=120,
            output_tokens=240,
            git_pushes=2,
            first_prompt="来自共享 loader 的 prompt",
            all_user_texts=["帮我看这个项目结构", "顺手改一下这个文件"],
            facet={
                "session_type": "exploration",
                "underlying_goal": "来自 facet 的目标",
                "brief_summary": "来自 facet 的摘要",
                "outcome": "mostly_achieved",
                "claude_helpfulness": "very_helpful",
                "primary_success": "good_explanations",
                "friction_counts": {"misunderstood_request": 1},
                "friction_detail": "facet 里的摩擦细节",
                "goal_categories": {"question_answering": 1},
                "user_satisfaction_counts": {"satisfied": 1},
            },
            meta={
                "session_id": "sess-001",
                "project_path": "/tmp/demo-project",
                "start_time": "2026-05-20T10:00:00+00:00",
                "duration_minutes": 18,
                "user_message_count": 4,
                "assistant_message_count": 7,
                "tool_counts": {"Read": 3, "Edit": 2},
                "git_pushes": 2,
                "input_tokens": 120,
                "output_tokens": 240,
                "first_prompt": "来自 meta 的 prompt",
            },
            raw_jsonl={
                "compact_count": 1,
                "edited_files": [{"path": "/tmp/demo-project/a.py"}],
                "written_files": [{"path": "/tmp/demo-project/b.py"}],
            },
            version="2.1.140",
            git_branch="main",
        )

        calls = []

        def fake_loader(start_date, end_date, claude_dir):
            calls.append((start_date, end_date, claude_dir))
            return [fake_session]

        load_data_from_jsonl.__globals__["load_sessions_from_workspace"] = fake_loader

        with tempfile.TemporaryDirectory() as tmp:
            load_data_from_jsonl.__globals__["CLAUDE_DIR"] = Path(tmp)
            items = load_data_from_jsonl(
                start_d=date(2026, 5, 20),
                end_d=date(2026, 5, 20),
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["date"], date(2026, 5, 20))
        self.assertEqual(item["meta"]["git_pushes"], 2)
        self.assertEqual(item["meta"]["assistant_message_count"], 7)
        self.assertEqual(item["facet"]["underlying_goal"], "来自 facet 的目标")
        self.assertEqual(item["facet"]["painting_stage"], "coloring")
        self.assertEqual(item["facet"]["has_compact"], True)


if __name__ == "__main__":
    unittest.main()