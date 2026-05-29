import json
import runpy
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")


class InsightLoaderIntegrationTest(unittest.TestCase):
    def test_load_data_from_jsonl_writes_and_reuses_zh_cache(self):
        module_globals = runpy.run_path(str(ROOT / "insight-zh.py"), run_name="not_main")
        load_data_from_jsonl = module_globals["load_data_from_jsonl"]

        with tempfile.TemporaryDirectory() as tmp:
            claude_dir = Path(tmp)
            load_data_from_jsonl.__globals__["CLAUDE_DIR"] = claude_dir
            session_id = "sess-001"
            jsonl_path = claude_dir / "projects" / "demo-project" / f"{session_id}.jsonl"
            _write_jsonl(
                jsonl_path,
                [
                    {
                        "timestamp": "2026-05-20T10:00:00Z",
                        "type": "user",
                        "message": {"content": "帮我实现一个功能"},
                        "cwd": "/tmp/demo-project",
                        "version": "2.1.140",
                        "gitBranch": "main",
                    },
                    {
                        "timestamp": "2026-05-20T10:08:00Z",
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Edit",
                                    "input": {"file_path": "/tmp/demo-project/a.py"},
                                },
                                {
                                    "type": "tool_use",
                                    "name": "Write",
                                    "input": {"file_path": "/tmp/demo-project/b.py"},
                                },
                            ]
                        },
                    },
                ],
            )

            first_items = load_data_from_jsonl(
                start_d=date(2026, 5, 20),
                end_d=date(2026, 5, 20),
            )
            second_items = load_data_from_jsonl(
                start_d=date(2026, 5, 20),
                end_d=date(2026, 5, 20),
            )

            self.assertTrue((claude_dir / "usage-data-zh" / "session-meta" / f"{session_id}.json").exists())
            self.assertTrue((claude_dir / "usage-data-zh" / "facets" / f"{session_id}.json").exists())
            self.assertTrue((claude_dir / "usage-data-zh" / "index.json").exists())
            cached_facet = json.loads((claude_dir / "usage-data-zh" / "facets" / f"{session_id}.json").read_text(encoding="utf-8"))
            self.assertRegex(cached_facet["analyzer_version"], r"^insight-zh-facets-[0-9a-f]{16}$")

        self.assertEqual(len(first_items), 1)
        self.assertEqual(len(second_items), 1)
        item = first_items[0]
        self.assertEqual(item["date"], date(2026, 5, 20))
        self.assertEqual(item["meta"]["assistant_message_count"], 1)
        self.assertEqual(item["facet"]["underlying_goal"], "帮我实现一个功能")
        self.assertEqual(item["facet"]["painting_stage"], "coloring")
        self.assertEqual(second_items[0].get("_cache_hit"), True)


if __name__ == "__main__":
    unittest.main()
