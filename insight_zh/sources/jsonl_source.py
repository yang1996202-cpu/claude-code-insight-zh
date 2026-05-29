import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable


BASH_READ_CMDS = re.compile(r'^(cat\s|head\s|tail\s|less\s|more\s|wc\s)', re.I)
BASH_EXPLORE_CMDS = re.compile(r'^(cd\s|pwd\s|which\s|whereis\s|uname\s|date\s|env\s)', re.I)
BASH_DANGEROUS = re.compile(r'\brm\s+-rf\b|\brm\s+.*\*\b|\bgit\s+(reset|clean)\b', re.I)
ACTIVE_GAP_CAP_SECONDS = 15 * 60
MEANINGFUL_TEXT = re.compile(r'[\w\u4e00-\u9fff]')


def iter_project_jsonl_paths(claude_dir: Path) -> Iterable[Path]:
    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        return []
    return projects_dir.rglob("*.jsonl")


def _estimate_active_minutes(timestamps):
    """Estimate active time by summing event gaps capped at 15 minutes.

    Claude Code sessions can stay open across hours or days. Wall-clock span is
    useful for audit, but it badly overstates actual work time in a daily report.
    """
    ordered = sorted(ts for ts in timestamps if ts)
    if len(ordered) < 2:
        return 0
    active_seconds = 0
    for prev, cur in zip(ordered, ordered[1:]):
        gap = max(0, (cur - prev).total_seconds())
        active_seconds += min(gap, ACTIVE_GAP_CAP_SECONDS)
    return int(active_seconds / 60)


def parse_jsonl_session(path: Path, start_date=None, end_date=None) -> Dict[str, object]:
    tool_counts = Counter()
    first_user = None
    first_ts = None
    last_ts = None
    window_first_ts = None
    window_last_ts = None
    jsonl_user_rows = 0
    real_user_msgs = 0
    tool_result_user_rows = 0
    system_user_rows = 0
    assistant_msgs = 0
    interruptions = 0
    compact_count = 0
    bash_commands = []
    read_files = []
    message_turns = []
    tools_by_ts = []
    user_msg_ts = []
    user_msg_texts = []
    event_timestamps = []
    edited_files = []
    written_files = []
    cwd = ""
    version = ""
    git_branch = ""

    with path.open(encoding="utf-8", errors="ignore") as fp:
        for line in fp:
            try:
                payload = json.loads(line)
            except Exception:
                continue

            ts = payload.get("timestamp")
            dt = None
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                    if first_ts is None:
                        first_ts = dt
                    last_ts = dt
                except Exception:
                    pass

            if not cwd:
                cwd = payload.get("cwd", "")
            if not version:
                version = payload.get("version", "")
            if not git_branch:
                git_branch = payload.get("gitBranch", "")

            in_window = True
            if dt and start_date and dt.date() < start_date:
                in_window = False
            if dt and end_date and dt.date() > end_date:
                in_window = False
            if not in_window:
                continue
            if dt:
                if window_first_ts is None:
                    window_first_ts = dt
                window_last_ts = dt

            msg_type = payload.get("type")
            if msg_type == "user":
                jsonl_user_rows += 1
                msg_text = ""
                content = payload.get("message", {}).get("content")
                has_tool_result = False
                if isinstance(content, str):
                    msg_text = content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "tool_result":
                            has_tool_result = True
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            msg_text += blk.get("text", "") + " "
                msg_text = msg_text.strip()
                has_system_markers = (
                    "<local-command-" in msg_text or
                    "<command-message>" in msg_text or
                    "<command-name>" in msg_text or
                    "<task-notification>" in msg_text or
                    msg_text.startswith("Base directory for this skill:") or
                    msg_text.startswith("Please analyze this codebase and create a CLAUDE.md file") or
                    "[2m" in msg_text or
                    "[22m" in msg_text or
                    "session is being continued" in msg_text.lower() or
                    "summary below covers" in msg_text.lower() or
                    "context was compacted" in msg_text.lower()
                )
                if has_tool_result:
                    tool_result_user_rows += 1
                elif has_system_markers:
                    system_user_rows += 1
                elif msg_text and dt and MEANINGFUL_TEXT.search(msg_text):
                    real_user_msgs += 1
                    user_msg_ts.append(dt)
                    user_msg_texts.append({"ts": dt, "text": msg_text})
                    if first_user is None:
                        first_user = msg_text
                if "/compact" in msg_text or "/compact" in line:
                    compact_count += 1
                message_turns.append({"ts": dt, "type": "user", "has_tool": has_tool_result})

            elif msg_type == "assistant":
                assistant_msgs += 1
                blocks = payload.get("message", {}).get("content", [])
                has_tool_use = False
                if isinstance(blocks, list):
                    for block in blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            has_tool_use = True
                            tool_name = block.get("name", "?")
                            tool_counts[tool_name] += 1
                            if dt:
                                tools_by_ts.append((dt, tool_name))
                            tool_input = block.get("input", {})

                            if tool_name == "Bash" and isinstance(tool_input, dict):
                                cmd = tool_input.get("command", "")
                                if cmd:
                                    bash_commands.append({
                                        "ts": dt,
                                        "cmd": cmd,
                                        "could_be_read": bool(BASH_READ_CMDS.search(cmd)),
                                        "is_explore": bool(BASH_EXPLORE_CMDS.search(cmd)),
                                        "is_dangerous": bool(BASH_DANGEROUS.search(cmd)),
                                    })
                            elif tool_name == "Read" and isinstance(tool_input, dict):
                                file_path = tool_input.get("file_path", "")
                                if file_path:
                                    read_files.append({"ts": dt, "path": file_path})
                            elif tool_name == "Edit" and isinstance(tool_input, dict):
                                file_path = tool_input.get("file_path", "")
                                if file_path:
                                    edited_files.append({"ts": dt, "path": file_path})
                            elif tool_name == "Write" and isinstance(tool_input, dict):
                                file_path = tool_input.get("file_path", "")
                                if file_path:
                                    written_files.append({"ts": dt, "path": file_path})
                message_turns.append({"ts": dt, "type": "assistant", "has_tool": has_tool_use})

            if '"interrupted":true' in line:
                interruptions += 1
            if dt:
                event_timestamps.append(dt)

    elapsed_minutes = 0
    if first_ts and last_ts:
        elapsed_minutes = int(max(0, (last_ts - first_ts).total_seconds()) / 60)

    return {
        "tool_counts": dict(tool_counts),
        "first_user": first_user,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "window_first_ts": window_first_ts,
        "window_last_ts": window_last_ts,
        "user_msgs": real_user_msgs,
        "jsonl_user_rows": jsonl_user_rows,
        "tool_result_user_rows": tool_result_user_rows,
        "system_user_rows": system_user_rows,
        "assistant_msgs": assistant_msgs,
        "elapsed_duration_minutes": elapsed_minutes,
        "active_duration_minutes": _estimate_active_minutes(event_timestamps),
        "interruptions": interruptions,
        "compact_count": compact_count,
        "bash_commands": bash_commands,
        "read_files": read_files,
        "edited_files": edited_files,
        "written_files": written_files,
        "message_turns": message_turns,
        "tools_by_ts": tools_by_ts,
        "user_msg_ts": user_msg_ts,
        "user_msg_texts": user_msg_texts,
        "cwd": cwd,
        "version": version,
        "git_branch": git_branch,
    }
