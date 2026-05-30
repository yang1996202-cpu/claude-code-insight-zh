import runpy
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sample_items():
    return [
        {
            "date": date(2026, 5, 20),
            "facet": {
                "session_id": "sess-render-001",
                "session_type": "exploration",
                "underlying_goal": "修复 loader 适配",
                "brief_summary": "修复 loader 适配",
                "outcome": "fully_achieved",
                "claude_helpfulness": "helpful",
                "primary_success": "correct_code_edits",
                "friction_counts": {"misunderstood_request": 1},
                "friction_detail": "第一次方向偏了，后来修正",
                "goal_categories": {"debugging": 1, "feature_implementation": 1},
                "user_satisfaction_counts": {"satisfied": 1},
                "_source": "jsonl+facet",
                "painting_stage": "coloring",
                "energy_flow": "creating",
            },
            "meta": {
                "session_id": "sess-render-001",
                "project_path": "/tmp/demo-project",
                "start_time": "2026-05-20T10:00:00+00:00",
                "duration_minutes": 240,
                "user_message_count": 8,
                "assistant_message_count": 12,
                "tool_counts": {"Read": 4, "Edit": 3, "Write": 1},
                "git_activity_count": 2,
                "git_commits": 2,
                "git_commit_hashes": ["abc", "def"],
                "input_tokens": 120,
                "output_tokens": 240,
                "user_interruptions": 0,
                "first_prompt": "修复 loader 适配",
            },
        }
    ]


class ReportRenderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module_globals = runpy.run_path(str(ROOT / "insight-zh.py"), run_name="not_main")

    def test_generate_report_uses_commit_metrics_and_goal_labels(self):
        generate_report = self.module_globals["generate_report"]

        report = generate_report(sample_items(), translations={})

        self.assertIn("2  个 commit", report)
        self.assertIn("| Git commit | 2 |", report)
        self.assertIn("## 你在做什么", report)
        self.assertIn("调试与排障：1", report)
        self.assertIn("代码与实现：1", report)

    def test_generate_html_report_uses_commit_labels_and_rate(self):
        generate_html_report = self.module_globals["generate_html_report"]
        generate_html_report.__globals__["generate_coaching_advice"] = lambda *args, **kwargs: []

        html = generate_html_report(sample_items(), translations={}, force_regenerate_advice=False)

        self.assertIn("2  个 commit", html)
        self.assertIn("<div class=\"ov-label\">Commit</div>", html)
        self.assertIn("Commit 率", html)
        self.assertIn("120 分钟/commit", html)
        self.assertIn("查看 1 个有 commit 的会话详情", html)
        self.assertIn("调试与排障", html)
        self.assertEqual(html.count("<h2>画室观察笔记</h2>"), 1)
        self.assertIn("明天可以试的", html)
        self.assertIn("主要工作流", html)
        self.assertIn("使用方式画像", html)
        self.assertIn("可复用的协作规则", html)
        self.assertIn("语义来源", html)
        self.assertNotIn("Push 率", html)

    def test_generate_coaching_advice_defaults_to_local_rules(self):
        generate_coaching_advice = self.module_globals["generate_coaching_advice"]
        stats = {
            "first_date": date(2026, 5, 20),
            "last_date": date(2026, 5, 20),
            "n": 1,
            "total_dur": 240,
            "total_user_msgs": 8,
            "total_commits": 0,
            "bash": 20,
            "read": 4,
            "edit": 3,
            "write": 1,
            "interruptions": 0,
            "morning": 1,
            "afternoon": 0,
            "night": 0,
            "midnight": 0,
            "frictions": {},
            "friction_details": [],
            "goals": {},
            "outcomes": {},
            "habits": [],
        }

        self.module_globals["API_KEY"] = "fake-key-that-should-not-be-used"
        advice = generate_coaching_advice(stats, use_external_llm=False)

        self.assertTrue(advice)
        self.assertIn("title", advice[0])


if __name__ == "__main__":
    unittest.main()
