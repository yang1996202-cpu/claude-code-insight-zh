from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_git_push_count(payload: Dict[str, Any]) -> int:
    return coerce_int(payload.get("git_activity_count", payload.get("git_commits", payload.get("git_pushes", 0))))


@dataclass
class NormalizedSession:
    session_id: str
    project_path: str
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    report_date: date
    duration_minutes: int
    user_message_count: int
    assistant_message_count: int
    tool_counts: Dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    git_pushes: int = 0
    first_prompt: str = ""
    all_user_texts: List[str] = field(default_factory=list)
    facet: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    raw_jsonl: Dict[str, Any] = field(default_factory=dict)
    jsonl_path: Optional[Path] = None
    version: str = ""
    git_branch: str = ""

    def get_tool_count(self, name: str) -> int:
        return coerce_int(self.tool_counts.get(name, 0))
