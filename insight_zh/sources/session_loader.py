from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from insight_zh.domain.session import NormalizedSession, coerce_int, get_git_push_count
from insight_zh.sources.facets_source import load_facet
from insight_zh.sources.jsonl_source import iter_project_jsonl_paths, parse_jsonl_session
from insight_zh.sources.session_meta_source import load_session_meta


def _parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def merge_session_sources(session_id: str, jsonl_path: Path, parsed: Dict[str, object], facet: Dict[str, object], meta: Dict[str, object]) -> Optional[NormalizedSession]:
    start_time = _parse_datetime(str(meta.get("start_time", ""))) or parsed.get("first_ts")
    end_time = parsed.get("last_ts") or start_time
    if start_time is None:
        return None

    report_date = end_time.date() if end_time else start_time.date()
    project_path = str(meta.get("project_path") or parsed.get("cwd") or jsonl_path.parent)
    tool_counts = meta.get("tool_counts") or parsed.get("tool_counts") or {}
    all_user_texts = [entry["text"] for entry in parsed.get("user_msg_texts", [])]

    normalized_meta = dict(meta)
    normalized_meta["session_id"] = session_id
    normalized_meta["project_path"] = project_path
    normalized_meta["start_time"] = (start_time.isoformat() if start_time else "")
    normalized_meta["git_pushes"] = get_git_push_count(normalized_meta)
    normalized_meta["tool_counts"] = tool_counts
    normalized_meta["first_prompt"] = normalized_meta.get("first_prompt") or parsed.get("first_user") or ""

    return NormalizedSession(
        session_id=session_id,
        project_path=project_path,
        start_time=start_time,
        end_time=end_time,
        report_date=report_date,
        duration_minutes=coerce_int(meta.get("duration_minutes"), coerce_int(int((end_time - start_time).total_seconds() / 60) if end_time else 0)),
        user_message_count=coerce_int(meta.get("user_message_count"), coerce_int(parsed.get("user_msgs"))),
        assistant_message_count=coerce_int(meta.get("assistant_message_count"), coerce_int(parsed.get("assistant_msgs"))),
        tool_counts={str(k): coerce_int(v) for k, v in tool_counts.items()},
        input_tokens=coerce_int(meta.get("input_tokens")),
        output_tokens=coerce_int(meta.get("output_tokens")),
        git_pushes=get_git_push_count(normalized_meta),
        first_prompt=str(normalized_meta.get("first_prompt", "")),
        all_user_texts=all_user_texts,
        facet=dict(facet),
        meta=normalized_meta,
        raw_jsonl=parsed,
        jsonl_path=jsonl_path,
        version=str(parsed.get("version", "")),
        git_branch=str(parsed.get("git_branch", "")),
    )


def load_sessions_from_workspace(start_date: Optional[date], end_date: date, claude_dir: Path) -> List[NormalizedSession]:
    sessions: List[NormalizedSession] = []
    for jsonl_path in iter_project_jsonl_paths(claude_dir):
        parsed = parse_jsonl_session(jsonl_path)
        first_ts = parsed.get("first_ts")
        last_ts = parsed.get("last_ts") or first_ts
        if first_ts is None or not parsed.get("user_msgs"):
            continue

        session_start_date = first_ts.date()
        session_end_date = last_ts.date() if last_ts else session_start_date
        if start_date and session_end_date < start_date:
            continue
        if session_start_date > end_date:
            continue

        first_prompt = (parsed.get("first_user") or "").strip()
        if first_prompt.startswith("<") and ">" in first_prompt:
            continue

        session_id = jsonl_path.stem
        facet = load_facet(session_id, claude_dir)
        meta = load_session_meta(session_id, claude_dir)
        session = merge_session_sources(session_id, jsonl_path, parsed, facet, meta)
        if session is not None:
            sessions.append(session)

    sessions.sort(key=lambda item: item.end_time or item.start_time or datetime.min, reverse=True)
    return sessions
