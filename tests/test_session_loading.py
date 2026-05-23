import json
import tempfile
import unittest
from datetime import date
from pathlib import Path


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class SessionLoadingTest(unittest.TestCase):
    def test_merge_jsonl_meta_facet(self):
        from insight_zh.sources.session_loader import load_sessions_from_workspace

        with tempfile.TemporaryDirectory() as tmp:
            claude_dir = Path(tmp)
            session_id = "sess-001"
            jsonl_path = claude_dir / "projects" / "demo-project" / f"{session_id}.jsonl"
            meta_path = claude_dir / "usage-data" / "session-meta" / f"{session_id}.json"
            facet_path = claude_dir / "usage-data" / "facets" / f"{session_id}.json"

            _write_jsonl(
                jsonl_path,
                [
                    {
                        "timestamp": "2026-05-20T10:00:00Z",
                        "type": "user",
                        "message": {"content": "帮我看这个项目结构"},
                        "cwd": "/tmp/demo-project",
                        "version": "2.1.140",
                        "gitBranch": "main",
                    },
                    {
                        "timestamp": "2026-05-20T10:01:00Z",
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Read",
                                    "input": {"file_path": "/tmp/demo-project/README.md"},
                                }
                            ]
                        },
                    },
                ],
            )

            _write_json(
                meta_path,
                {
                    "session_id": session_id,
                    "project_path": "/tmp/demo-project",
                    "start_time": "2026-05-20T10:00:00+00:00",
                    "duration_minutes": 18,
                    "user_message_count": 4,
                    "assistant_message_count": 7,
                    "tool_counts": {"Read": 3, "Bash": 1},
                    "git_pushes": 2,
                    "input_tokens": 120,
                    "output_tokens": 240,
                    "first_prompt": "来自 meta 的 prompt",
                },
            )

            _write_json(
                facet_path,
                {
                    "session_id": session_id,
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
            )

            sessions = load_sessions_from_workspace(
                start_date=date(2026, 5, 20),
                end_date=date(2026, 5, 20),
                claude_dir=claude_dir,
            )

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session.session_id, session_id)
            self.assertEqual(session.project_path, "/tmp/demo-project")
            self.assertEqual(session.duration_minutes, 18)
            self.assertEqual(session.git_pushes, 2)
            self.assertEqual(session.facet["underlying_goal"], "来自 facet 的目标")
            self.assertEqual(session.facet["outcome"], "mostly_achieved")
            self.assertEqual(session.meta["first_prompt"], "来自 meta 的 prompt")
            self.assertEqual(session.report_date, date(2026, 5, 20))

    def test_cross_day_session_in_range(self):
        from insight_zh.sources.session_loader import load_sessions_from_workspace

        with tempfile.TemporaryDirectory() as tmp:
            claude_dir = Path(tmp)
            session_id = "sess-cross-day"
            jsonl_path = claude_dir / "projects" / "demo-project" / f"{session_id}.jsonl"

            _write_jsonl(
                jsonl_path,
                [
                    {
                        "timestamp": "2026-05-20T23:55:00Z",
                        "type": "user",
                        "message": {"content": "晚上先开个头"},
                        "cwd": "/tmp/demo-project",
                    },
                    {
                        "timestamp": "2026-05-21T00:10:00Z",
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Write",
                                    "input": {"file_path": "/tmp/demo-project/a.py"},
                                }
                            ]
                        },
                    },
                ],
            )

            sessions = load_sessions_from_workspace(
                start_date=date(2026, 5, 21),
                end_date=date(2026, 5, 21),
                claude_dir=claude_dir,
            )

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].session_id, session_id)
            self.assertEqual(sessions[0].report_date, date(2026, 5, 21))


if __name__ == "__main__":
    unittest.main()