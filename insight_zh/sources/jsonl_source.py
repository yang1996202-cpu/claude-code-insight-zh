import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable


BASH_READ_CMDS = re.compile(r'^(cat\s|head\s|tail\s|less\s|more\s|wc\s)', re.I)
BASH_EXPLORE_CMDS = re.compile(r'^(cd\s|pwd\s|which\s|whereis\s|uname\s|date\s|env\s)', re.I)
BASH_DANGEROUS = re.compile(r'\brm\s+-rf\b|\brm\s+.*\*\b|\bgit\s+(reset|clean)\b', re.I)


def iter_project_jsonl_paths(claude_dir: Path) -> Iterable[Path]:
    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        return []
    return projects_dir.rglob("*.jsonl")


def parse_jsonl_session(path: Path) -> Dict[str, object]:
    tool_counts = Counter()
    first_user = None
    first_ts = None
    last_ts = None
    user_msgs = 0
    assistant_msgs = 0
    interruptions = 0
    compact_count = 0
    bash_commands = []
    read_files = []
    message_turns = []
    tools_by_ts = []
    user_msg_ts = []
    user_msg_texts = []
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

            msg_type = payload.get("type")
            if msg_type == "user":
                user_msgs += 1
                if dt:
                    user_msg_ts.append(dt)
                msg_text = ""
                content = payload.get("message", {}).get("content")
                if isinstance(content, str):
                    msg_text = content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            msg_text += blk.get("text", "") + " "
                msg_text = msg_text.strip()
                has_system_markers = (
                    "<local-command-" in msg_text or
                    "<command-message>" in msg_text or
                    "<command-name>" in msg_text or
                    "[2m" in msg_text or
                    "[22m" in msg_text or
                    "session is being continued" in msg_text.lower() or
                    "summary below covers" in msg_text.lower() or
                    "context was compacted" in msg_text.lower()
                )
                if msg_text and dt and not has_system_markers and 3 <= len(msg_text) <= 1000:
                    user_msg_texts.append({"ts": dt, "text": msg_text})
                if "/compact" in msg_text or "/compact" in line:
                    compact_count += 1
                if first_user is None and msg_text:
                    first_user = msg_text
                has_tool_result = False
                if isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "tool_result":
                            has_tool_result = True
                            break
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

    return {
        "tool_counts": dict(tool_counts),
        "first_user": first_user,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
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
