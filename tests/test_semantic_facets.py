import unittest
from datetime import date, datetime

from insight_zh.analysis.session_inference import build_legacy_report_item
from insight_zh.domain.session import NormalizedSession


class SemanticFacetTest(unittest.TestCase):
    def test_fallback_fills_insights_like_fields_without_official_facet(self):
        session = NormalizedSession(
            session_id="sess-semantic",
            project_path="/tmp/demo",
            start_time=datetime(2026, 5, 29, 10, 0),
            end_time=datetime(2026, 5, 29, 10, 45),
            report_date=date(2026, 5, 29),
            duration_minutes=35,
            user_message_count=3,
            assistant_message_count=4,
            tool_counts={"Read": 3, "Edit": 2, "Bash": 1},
            git_pushes=0,
            first_prompt="帮我分析这个报告为什么统计不准确，然后改一下缓存逻辑",
            all_user_texts=[
                "帮我分析这个报告为什么统计不准确，然后改一下缓存逻辑",
                "这个消息数不对，你从源头看一下",
                "好，继续改",
            ],
            raw_jsonl={
                "edited_files": [{"path": "/tmp/demo/insight.py"}],
                "written_files": [],
                "read_files": [{"path": "/tmp/demo/insight.py"}],
            },
        )

        item = build_legacy_report_item(session)
        facet = item["facet"]

        self.assertEqual(facet["_semantic_source"], "insight-zh-semantic-v1")
        self.assertEqual(facet["semantic_confidence"], "heuristic")
        self.assertEqual(facet["outcome"], "partially_achieved")
        self.assertEqual(facet["claude_helpfulness"], "moderately_helpful")
        self.assertIn("用户明确提出卡点或纠偏", facet["friction_detail"])
        self.assertIn("统计不准确", facet["brief_summary"])


if __name__ == "__main__":
    unittest.main()
