from datetime import date, datetime
from pathlib import Path
import subprocess
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


def _git_root(path: str) -> str:
    if not path:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _git_commit_hashes(project_path: str, start_time: Optional[datetime], end_time: Optional[datetime]) -> List[str]:
    root = _git_root(project_path)
    if not root or not start_time or not end_time:
        return []
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                root,
                "log",
                "--all",
                "--format=%H",
                f"--since={start_time.isoformat()}",
                f"--until={end_time.isoformat()}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def merge_session_sources(session_id: str, jsonl_path: Path, parsed: Dict[str, object], facet: Dict[str, object], meta: Dict[str, object]) -> Optional[NormalizedSession]:
    start_time = parsed.get("window_first_ts") or _parse_datetime(str(meta.get("start_time", ""))) or parsed.get("first_ts")
    end_time = parsed.get("window_last_ts") or parsed.get("last_ts") or start_time
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
    normalized_meta["tool_counts"] = tool_counts
    normalized_meta["first_prompt"] = normalized_meta.get("first_prompt") or parsed.get("first_user") or ""
    elapsed_minutes = coerce_int(parsed.get("elapsed_duration_minutes"), coerce_int(int((end_time - start_time).total_seconds() / 60) if end_time else 0))
    active_minutes = coerce_int(parsed.get("active_duration_minutes"), elapsed_minutes)
    real_user_messages = coerce_int(parsed.get("user_msgs"))
    normalized_meta["duration_minutes"] = active_minutes
    normalized_meta["active_duration_minutes"] = active_minutes
    normalized_meta["elapsed_duration_minutes"] = elapsed_minutes
    normalized_meta["user_message_count"] = real_user_messages
    normalized_meta["jsonl_user_row_count"] = coerce_int(parsed.get("jsonl_user_rows"))
    normalized_meta["tool_result_message_count"] = coerce_int(parsed.get("tool_result_user_rows"))
    normalized_meta["system_user_message_count"] = coerce_int(parsed.get("system_user_rows"))
    normalized_meta["assistant_message_count"] = coerce_int(parsed.get("assistant_msgs"))
    git_hashes = _git_commit_hashes(project_path, start_time, end_time)
    git_commits = len(git_hashes)
    normalized_meta["git_repo_root"] = _git_root(project_path)
    normalized_meta["git_commit_hashes"] = git_hashes
    normalized_meta["git_pushes"] = get_git_push_count(meta)
    normalized_meta["git_commits"] = git_commits
    normalized_meta["git_activity_count"] = git_commits

    return NormalizedSession(
        session_id=session_id,
        project_path=project_path,
        start_time=start_time,
        end_time=end_time,
        report_date=report_date,
        duration_minutes=active_minutes,
        user_message_count=real_user_messages,
        assistant_message_count=coerce_int(parsed.get("assistant_msgs")),
        tool_counts={str(k): coerce_int(v) for k, v in tool_counts.items()},
        input_tokens=coerce_int(meta.get("input_tokens")),
        output_tokens=coerce_int(meta.get("output_tokens")),
        git_pushes=git_commits,
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
        parsed = parse_jsonl_session(jsonl_path, start_date=start_date, end_date=end_date)
        first_ts = parsed.get("first_ts")
        last_ts = parsed.get("last_ts") or first_ts
        if first_ts is None:
            continue

        session_start_date = first_ts.date()
        session_end_date = last_ts.date() if last_ts else session_start_date
        if start_date and session_end_date < start_date:
            continue
        if session_start_date > end_date:
            continue
        if not parsed.get("user_msgs"):
            continue

        session_id = jsonl_path.stem
        facet = load_facet(session_id, claude_dir)
        meta = load_session_meta(session_id, claude_dir)
        session = merge_session_sources(session_id, jsonl_path, parsed, facet, meta)
        if session is not None:
            sessions.append(session)

    sessions.sort(key=lambda item: item.end_time or item.start_time or datetime.min, reverse=True)
    return sessions
