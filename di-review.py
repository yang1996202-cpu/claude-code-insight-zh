#!/usr/bin/env python3
"""
Claude Code 每日报告生成器 — 自动写一份 markdown 日报到 ~/.claude/daily-reports/

用法：
  di-review                  生成今天的报告并打开
  di-review 2026-05-10       生成指定日期的报告
  di-review 7                生成今天的报告（但 stdout 同时显示最近 7 天趋势）
  di-review --week           本周趋势（stdout，不写文件）
  di-review --regen          强制重新生成（覆盖 Claude 写的部分，保留你的反思）
  di-review --quiet          不打开编辑器
  di-review --print-only     只输出到 stdout，不写文件
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
FACETS_DIR = CLAUDE_DIR / "usage-data/facets"
REPORTS_DIR = CLAUDE_DIR / "daily-reports"
REPORTS_DIR.mkdir(exist_ok=True)

HEALTHY_BASH_READ_RATIO = 2.0
HEALTHY_MSGS_PER_SESSION = 40  # 每会话阈值，比每天总量更合理
HEALTHY_ACHIEVEMENT_RATE = 0.70  # 达成率健康阈值

# 用户反思区域的分隔标记
USER_MARKER = "<!-- 下面是你的反思区，Claude 不会覆盖 -->"
REGEN_MARKER = "<!-- 以上由 Claude 自动生成，重新跑会被覆盖 -->"

# Bash 命令分类模式
BASH_READ_CMDS = re.compile(r'^(cat\s|head\s|tail\s|less\s|more\s|wc\s)', re.I)
BASH_WRITE_CMDS = re.compile(r'^(echo\s|printf\s|.*[<>].*)', re.I)
BASH_EXPLORE_CMDS = re.compile(r'^(cd\s|pwd\s|which\s|whereis\s|uname\s|date\s|env\s)', re.I)
BASH_DANGEROUS = re.compile(r'\brm\s+-rf\b|\brm\s+.*\*\b|\bgit\s+(reset|clean)\b', re.I)

# 摩擦类型 → 改进建议映射
FRICTION_ADVICE = {
    "misunderstood_request": {
        "观察": "Claude 频繁误解你的意图",
        "约束": "开会话前用 1 句话写明目标 + 关键约束，不要只扔关键词",
    },
    "wrong_approach": {
        "观察": "Claude 选择了错误的方法或路径",
        "约束": "要求 Claude 先给出方案假设，确认后再执行",
    },
    "excessive_changes": {
        "观察": "Claude 改了太多不相关的文件/代码",
        "约束": "明确指定要改的文件和函数，禁止动没点名的部分",
    },
    "buggy_code": {
        "观察": "生成的代码有 bug 需要反复修",
        "约束": "要求先写测试再写实现，或至少给出验证步骤",
    },
    "recurring_bug": {
        "观察": "同一个 bug 反复出现",
        "约束": "把常见 bug 写进 CLAUDE.md 的编码纪律，每次开工前扫一眼",
    },
    "slow_progress": {
        "观察": "任务推进太慢，反复兜圈子",
        "约束": "设定时间上限（如 30 分钟），到点没进展就换方案",
    },
    "user_rejected_action": {
        "观察": "Claude 提议的操作被你否决",
        "约束": "要求 Claude 做重要操作前必须征得同意",
    },
    "incomplete_solution": {
        "观察": "解决方案不完整，遗漏了边界情况",
        "约束": "要求 Claude 列出'还有什么没考虑到'再结束",
    },
    "unable_to_resolve": {
        "观察": "Claude 无法解决某个问题",
        "约束": "设定求助阈值，超过就转人工或换工具",
    },
    "api_errors": {
        "观察": "API 调用频繁报错",
        "约束": "检查 API 密钥和配额，先小批量测试再批量执行",
    },
    "tool_failure": {
        "观察": "工具调用失败（如文件不存在、命令报错）",
        "约束": "要求 Claude 先确认文件/环境存在再执行命令",
    },
}


def parse_args():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("target", nargs="?", default=None)
    p.add_argument("--week", action="store_true")
    p.add_argument("--regen", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--print-only", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    return p.parse_args()


def parse_target(s):
    """返回 ('day', date) 或 ('range', N)"""
    if s is None:
        return ("day", date.today())
    if re.fullmatch(r"\d+", s):
        return ("range", int(s))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return ("day", date(int(m[1]), int(m[2]), int(m[3])))
    print(f"无法识别参数：{s}", file=sys.stderr)
    sys.exit(2)


def parse_jsonl(path: Path):
    """解析会话 jsonl，返回详细的工具调用和消息数据"""
    tool_counts = Counter()
    first_user = None
    first_ts = None
    last_ts = None
    user_msgs = 0
    interruptions = 0
    compact_count = 0
    # 消息级数据
    bash_commands = []  # [(timestamp, command, could_be_read)]
    read_files = []     # [(timestamp, file_path)]
    message_turns = []  # [{'ts': datetime, 'type': 'user'|'assistant', 'has_tool': bool}]
    # 按日期分组的统计（用于跨天会话精准计算）
    tools_by_ts = []    # [(ts, tool_name)]
    user_msg_ts = []    # [ts]
    user_msg_texts = [] # [(ts, text)] 用户消息文本，用于内容分析
    edited_files = []   # [(ts, file_path)] Edit 修改的文件
    written_files = []  # [(ts, file_path)] Write 写入的文件

    for line in path.open(encoding="utf-8", errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue

        ts = d.get("timestamp")
        dt = None
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                if first_ts is None:
                    first_ts = dt
                last_ts = dt
            except Exception:
                pass

        msg_type = d.get("type")

        if msg_type == "user":
            user_msgs += 1
            if dt:
                user_msg_ts.append(dt)
            # 提取用户消息文本（过滤系统注入的 continuation 消息）
            msg_text = ""
            content = d.get("message", {}).get("content")
            if isinstance(content, str):
                msg_text = content
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        t = blk.get("text", "")
                        msg_text += t + " "
            msg_text = msg_text.strip()
            # 过滤系统消息和引用块（特征：包含特定标记）
            has_system_markers = (
                "<local-command-" in msg_text or
                "<command-message>" in msg_text or
                "<command-name>" in msg_text or
                "[2m" in msg_text or  # ANSI 转义码
                "[22m" in msg_text or
                "session is being continued" in msg_text.lower() or
                "summary below covers" in msg_text.lower() or
                "context was compacted" in msg_text.lower()
            )
            # 只保存看起来是真实用户输入的消息（用于内容分析）
            # 长度限制：3-1000 字，排除系统消息
            if msg_text and dt and not has_system_markers and 3 <= len(msg_text) <= 1000:
                user_msg_texts.append({"ts": dt, "text": msg_text})
            # 检测 /compact 命令
            if "/compact" in msg_text or "/compact" in line:
                compact_count += 1
            if first_user is None and msg_text:
                first_user = msg_text
            # 检查是否包含 tool_result（即对 assistant 工具的回应）
            has_tool_result = False
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        has_tool_result = True
                        break
            message_turns.append({"ts": dt, "type": "user", "has_tool": has_tool_result})

        elif msg_type == "assistant":
            blocks = d.get("message", {}).get("content", [])
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
                                could_be_read = bool(BASH_READ_CMDS.search(cmd))
                                bash_commands.append({
                                    "ts": dt,
                                    "cmd": cmd,
                                    "could_be_read": could_be_read,
                                    "is_explore": bool(BASH_EXPLORE_CMDS.search(cmd)),
                                    "is_dangerous": bool(BASH_DANGEROUS.search(cmd)),
                                })
                        elif tool_name == "Read" and isinstance(tool_input, dict):
                            fp = tool_input.get("file_path", "")
                            if fp:
                                read_files.append({"ts": dt, "path": fp})
                        elif tool_name == "Edit" and isinstance(tool_input, dict):
                            fp = tool_input.get("file_path", "")
                            if fp:
                                edited_files.append({"ts": dt, "path": fp})
                        elif tool_name == "Write" and isinstance(tool_input, dict):
                            fp = tool_input.get("file_path", "")
                            if fp:
                                written_files.append({"ts": dt, "path": fp})
            message_turns.append({"ts": dt, "type": "assistant", "has_tool": has_tool_use})

        if '"interrupted":true' in line:
            interruptions += 1

    return {
        "tool_counts": tool_counts,
        "first_user": first_user,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "user_msgs": user_msgs,
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
    }


def load_sessions(start_d, end_d):
    items = []
    if not PROJECTS_DIR.exists():
        return items
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        mtime = datetime.fromtimestamp(jsonl.stat().st_mtime).date()
        if mtime < start_d or mtime > end_d:
            continue
        sid = jsonl.stem
        parsed = parse_jsonl(jsonl)
        target_dates = set()
        if parsed["first_ts"]:
            target_dates.add(parsed["first_ts"].date())
        if parsed["last_ts"]:
            target_dates.add(parsed["last_ts"].date())
        if not any(start_d <= d <= end_d for d in target_dates):
            continue
        project = jsonl.parent.name.replace("-Users-yang", "~").replace("-", "/")
        facet = {}
        fp = FACETS_DIR / f"{sid}.json"
        if fp.exists():
            try:
                facet = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                pass
        items.append({
            "session_id": sid,
            "project": project,
            "parsed": parsed,
            "facet": facet,
            "session_date": parsed["last_ts"].date() if parsed["last_ts"] else mtime,
        })
    items.sort(key=lambda x: x["parsed"]["last_ts"] or datetime.min, reverse=True)
    return items


def is_noise(it):
    """空会话/测试输入过滤"""
    p = it["parsed"]
    total_tools = sum(p["tool_counts"].values())
    if total_tools == 0 and p["user_msgs"] <= 2:
        return True
    fu = (p["first_user"] or "").strip()
    if total_tools == 0 and len(fu) <= 3:
        return True
    return False


def daily_stats(items, target_date=None):
    """计算指定日期的统计（修复跨天会话时长问题）"""
    total = Counter()
    user_msgs = 0
    dur_min = 0
    all_bash = []
    all_reads = []
    all_turns = []
    all_user_texts = []  # 当天用户消息文本
    all_edited = []      # 当天 Edit 的文件
    all_written = []     # 当天 Write 的文件
    compact_count = 0    # 当天 /compact 次数

    for it in items:
        p = it["parsed"]
        # 只统计当天的工具调用和用户消息
        for ts, tool_name in p.get("tools_by_ts", []):
            if target_date is None or ts.date() == target_date:
                total[tool_name] += 1
        for ts in p.get("user_msg_ts", []):
            if target_date is None or ts.date() == target_date:
                user_msgs += 1

        # 修复时长计算：只统计目标日期内的消息时间，并分段（间隔>30分钟视为中断）
        if target_date:
            day_msgs = sorted([t["ts"] for t in p["message_turns"] if t["ts"] and t["ts"].date() == target_date])
            if len(day_msgs) >= 2:
                # 分段计算，间隔超过30分钟不计入
                GAP_SECONDS = 30 * 60
                active_seconds = 0
                segment_start = day_msgs[0]
                for i in range(1, len(day_msgs)):
                    gap = (day_msgs[i] - day_msgs[i-1]).total_seconds()
                    if gap > GAP_SECONDS:
                        active_seconds += (day_msgs[i-1] - segment_start).total_seconds()
                        segment_start = day_msgs[i]
                active_seconds += (day_msgs[-1] - segment_start).total_seconds()
                dur_min += active_seconds / 60
        else:
            # 兼容旧逻辑
            if p["first_ts"] and p["last_ts"]:
                dur_min += (p["last_ts"] - p["first_ts"]).total_seconds() / 60

        # 只收集当天的 Bash/Read/turns/文本/文件修改
        for bc in p["bash_commands"]:
            if target_date is None or (bc["ts"] and bc["ts"].date() == target_date):
                all_bash.append(bc)
        for rf in p["read_files"]:
            if target_date is None or (rf["ts"] and rf["ts"].date() == target_date):
                all_reads.append(rf)
        for ef in p.get("edited_files", []):
            if target_date is None or (ef["ts"] and ef["ts"].date() == target_date):
                all_edited.append(ef)
        for wf in p.get("written_files", []):
            if target_date is None or (wf["ts"] and wf["ts"].date() == target_date):
                all_written.append(wf)
        for ut in p.get("user_msg_texts", []):
            if target_date is None or (ut["ts"] and ut["ts"].date() == target_date):
                all_user_texts.append(ut)
        for turn in p["message_turns"]:
            if target_date is None or (turn["ts"] and turn["ts"].date() == target_date):
                all_turns.append(turn)
        # compact 次数（按 session 统计，不精确到消息时间）
        compact_count += p.get("compact_count", 0)

    # 计算当天最早和最晚消息时间
    all_day_ts = sorted([t["ts"] for t in all_turns if t["ts"]])
    earliest_ts = all_day_ts[0] if all_day_ts else None
    latest_ts = all_day_ts[-1] if all_day_ts else None

    return {
        "total": total,
        "dur_min": int(dur_min),
        "user_msgs": user_msgs,
        "bash_commands": all_bash,
        "read_files": all_reads,
        "edited_files": all_edited,
        "written_files": all_written,
        "user_msg_texts": all_user_texts,
        "message_turns": all_turns,
        "compact_count": compact_count,
        "earliest_ts": earliest_ts,
        "latest_ts": latest_ts,
    }


def short(s, n=70):
    if not s:
        return "(无)"
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def bash_read_ratio(total):
    bash = total.get("Bash", 0)
    read = total.get("Read", 0)
    if read == 0:
        return None if bash <= 5 else float("inf")
    return bash / read


def find_bash_clusters(bash_commands, gap_seconds=120):
    """找出 Bash 密集调用区段（连续调用间隔 < gap_seconds）"""
    if not bash_commands:
        return []
    sorted_cmds = sorted(bash_commands, key=lambda x: x["ts"] or datetime.min)
    clusters = []
    current = [sorted_cmds[0]]
    for cmd in sorted_cmds[1:]:
        if cmd["ts"] and current[-1]["ts"]:
            gap = (cmd["ts"] - current[-1]["ts"]).total_seconds()
            if gap < gap_seconds:
                current.append(cmd)
                continue
        if len(current) >= 3:
            clusters.append(current)
        current = [cmd]
    if len(current) >= 3:
        clusters.append(current)
    return clusters


def analyze_bash_quality(bash_commands):
    """分析 Bash 调用质量"""
    if not bash_commands:
        return {}

    could_be_read = [c for c in bash_commands if c["could_be_read"]]
    explore_only = [c for c in bash_commands if c["is_explore"] and not c["could_be_read"]]
    dangerous = [c for c in bash_commands if c["is_dangerous"]]

    clusters = find_bash_clusters(bash_commands)
    # 只找出 could_be_read 的密集区段
    cr_clusters = find_bash_clusters(could_be_read)
    total = len(bash_commands)

    return {
        "total": total,
        "could_be_read_count": len(could_be_read),
        "could_be_read_pct": len(could_be_read) / total * 100 if total else 0,
        "explore_count": len(explore_only),
        "dangerous_count": len(dangerous),
        "clusters": clusters,
        "worst_cluster": max(cr_clusters, key=len) if cr_clusters else None,
    }


def analyze_message_patterns(turns, target_date=None):
    """分析消息模式"""
    if target_date:
        day_turns = [t for t in turns if t["ts"] and t["ts"].date() == target_date]
    else:
        day_turns = turns

    # 找出连续用户短消息（< 20 字，连续 3+ 条）
    # 注意：turns 里只有类型，没有内容长度，这里简化处理
    # 统计用户-助手轮次比
    user_turns = [t for t in day_turns if t["type"] == "user"]
    assist_turns = [t for t in day_turns if t["type"] == "assistant"]

    # 找出连续 assistant tool_use（说明 Claude 在疯狂调用工具）
    tool_runs = 0
    max_consecutive_tools = 0
    current_tools = 0
    for t in day_turns:
        if t["type"] == "assistant" and t.get("has_tool"):
            current_tools += 1
            max_consecutive_tools = max(max_consecutive_tools, current_tools)
        else:
            if current_tools > 0:
                tool_runs += 1
            current_tools = 0

    return {
        "user_turns": len(user_turns),
        "assist_turns": len(assist_turns),
        "tool_runs": tool_runs,
        "max_consecutive_tools": max_consecutive_tools,
    }


# 用户消息中的摩擦/达成信号关键词
FRICTION_SIGNALS = [
    "不对", "错了", "重来", "不是", "不要", "停", "换个", "跑偏", "偏了",
    "没理解", "误解", "搞错", "搞混", "混乱", "不对头",
    "不要这样", "不是这样", "方向错了", "走偏", "不对劲儿",
]
ACHIEVEMENT_SIGNALS = [
    "好了", "可以了", "搞定", "完成", "谢谢", "完美", "不错", "OK", "ok",
    "解决了", "搞定了", "没问题", "成了", "满足了", "符合", "满意",
]
ABANDON_SIGNALS = [
    "算了", "先这样", "明天再说", "先放着", "暂时", "以后再", "回头",
    "搁置", "放下", "不搞了", "先不搞", "跳过",
]


def _safe_keyword_match(text, keyword):
    """安全的关键词匹配：避免子串误匹配（如「成了」匹配「生成了」）"""
    import re
    # 要求关键词前后不是中文字母或 ASCII 字母数字（即独立成词）
    pattern = r'(?<![一-鿿\w])' + re.escape(keyword) + r'(?![一-鿿\w])'
    return bool(re.search(pattern, text))


def analyze_message_content(items, target_date=None):
    """从用户消息文本推断摩擦和达成信号（替代/补充 facet）"""
    friction_hits = []  # [(ts, text, matched_keyword)]
    achievement_hits = []
    abandon_hits = []
    short_msgs = []  # 短消息（< 20 字），连续出现说明反复修正

    for it in items:
        p = it["parsed"]
        for ut in p.get("user_msg_texts", []):
            if target_date and ut["ts"] and ut["ts"].date() != target_date:
                continue
            text = ut["text"]
            # 摩擦信号（安全匹配）
            for kw in FRICTION_SIGNALS:
                if _safe_keyword_match(text, kw):
                    friction_hits.append({"ts": ut["ts"], "text": text, "kw": kw})
                    break
            # 达成信号（安全匹配）
            for kw in ACHIEVEMENT_SIGNALS:
                if _safe_keyword_match(text, kw):
                    achievement_hits.append({"ts": ut["ts"], "text": text, "kw": kw})
                    break
            # 放弃信号（安全匹配）
            for kw in ABANDON_SIGNALS:
                if _safe_keyword_match(text, kw):
                    abandon_hits.append({"ts": ut["ts"], "text": text, "kw": kw})
                    break
            # 短消息统计
            if len(text) < 20:
                short_msgs.append({"ts": ut["ts"], "text": text})

    # 推断结果
    inferred = {
        "has_friction": len(friction_hits) >= 2,
        "friction_count": len(friction_hits),
        "friction_examples": friction_hits[:3],
        "has_achievement": len(achievement_hits) >= 1,
        "achievement_count": len(achievement_hits),
        "achievement_examples": achievement_hits[:2],
        "has_abandon": len(abandon_hits) >= 1,
        "abandon_count": len(abandon_hits),
        "short_msg_count": len(short_msgs),
        "short_msg_examples": short_msgs[:3],
    }

    # 映射到伪 facet 摩擦类型（用于统一建议）
    if inferred["has_friction"]:
        if inferred["short_msg_count"] >= 5:
            inferred["top_fric_type"] = "misunderstood_request"
            inferred["top_fric_obs"] = "用户频繁短消息纠偏，Claude 可能误解了意图"
            inferred["top_fric_constraint"] = "开会话前用 1 句话写明目标 + 关键约束"
        else:
            inferred["top_fric_type"] = "user_rejected_action"
            inferred["top_fric_obs"] = "用户多次否定 Claude 的输出"
            inferred["top_fric_constraint"] = "要求 Claude 做重要操作前必须征得同意"
    else:
        inferred["top_fric_type"] = None

    return inferred


def compute_derived_metrics(stats):
    """计算衍生指标：compact、空转、文件产出"""
    turns = stats.get("message_turns", [])
    edited = stats.get("edited_files", [])
    written = stats.get("written_files", [])

    # 空转：assistant 消息但没调工具（说明在纯聊天）
    idle_turns = [t for t in turns if t["type"] == "assistant" and not t.get("has_tool")]

    # 文件修改产出：去重后的文件数
    edited_paths = {e["path"] for e in edited}
    written_paths = {w["path"] for w in written}
    all_modified = edited_paths | written_paths

    # 反复编辑的文件（被 Edit 2+ 次的文件）
    edit_counter = Counter(e["path"] for e in edited)
    repeat_edits = {p: c for p, c in edit_counter.items() if c >= 2}

    return {
        "compact_count": stats.get("compact_count", 0),
        "idle_turns": len(idle_turns),
        "idle_pct": len(idle_turns) / max(len(turns), 1) * 100,
        "unique_files_modified": len(all_modified),
        "edited_files_count": len(edited),
        "written_files_count": len(written),
        "repeat_edits": repeat_edits,
        "repeat_edit_count": len(repeat_edits),
    }


def extract_friction_advice(items):
    """从 facet 数据提取摩擦分析和改进建议"""
    all_frics = Counter()
    fric_details = []

    for it in items:
        f = it["facet"]
        if not f:
            continue
        fc = f.get("friction_counts", {})
        for k, v in fc.items():
            all_frics[k] += v
        detail = f.get("friction_detail")
        if detail:
            fric_details.append((it["session_id"][:8], detail))

    if not all_frics:
        return None

    # 找出最严重的摩擦类型
    top_fric = all_frics.most_common(1)[0]
    fric_type, fric_count = top_fric

    advice = FRICTION_ADVICE.get(fric_type, {
        "观察": f"出现 {fric_count} 次 {fric_type} 摩擦",
        "约束": "回顾今天的会话，找出触发点并设定规避策略",
    })

    return {
        "top_type": fric_type,
        "top_count": fric_count,
        "total_frics": dict(all_frics.most_common()),
        "details": fric_details,
        "observation": advice["观察"],
        "constraint": advice["约束"],
    }


def extract_outcome_stats(items):
    """从 facet 数据提取达成与结果统计"""
    outcomes = Counter()
    unachieved = []  # (session_id, brief_summary, friction_detail)
    total_satisfaction = Counter()
    sessions_with_facet = 0

    for it in items:
        f = it["facet"]
        if not f:
            continue
        sessions_with_facet += 1

        outcome = f.get("outcome")
        if outcome:
            outcomes[outcome] += 1

        # 收集未达成会话
        if outcome in ("not_achieved", "abandoned"):
            summary = f.get("brief_summary", "")
            fric_detail = f.get("friction_detail", "")
            unachieved.append((
                it["session_id"][:8],
                summary,
                fric_detail,
            ))

        # 满意度
        sat = f.get("user_satisfaction_counts", {})
        for k, v in sat.items():
            total_satisfaction[k] += v

    if sessions_with_facet == 0:
        return None

    total = sessions_with_facet
    fully = outcomes.get("fully_achieved", 0)
    partially = outcomes.get("partially_achieved", 0)
    not_achieved = outcomes.get("not_achieved", 0)
    abandoned = outcomes.get("abandoned", 0)
    mostly = outcomes.get("mostly_achieved", 0)
    unclear = outcomes.get("unclear_from_transcript", 0)

    achievement_rate = (fully + mostly) / total if total else 0

    satisfied = total_satisfaction.get("satisfied", 0)
    likely_sat = total_satisfaction.get("likely_satisfied", 0)
    satisfaction_total = satisfied + likely_sat
    satisfaction_rate = satisfied / satisfaction_total if satisfaction_total else None

    return {
        "total": total,
        "fully_achieved": fully,
        "partially_achieved": partially,
        "not_achieved": not_achieved,
        "abandoned": abandoned,
        "mostly_achieved": mostly,
        "unclear": unclear,
        "outcomes": dict(outcomes),
        "achievement_rate": achievement_rate,
        "unachieved": unachieved,
        "satisfied": satisfied,
        "likely_satisfied": likely_sat,
        "satisfaction_rate": satisfaction_rate,
    }


def health_status(total):
    ratio = bash_read_ratio(total)
    if ratio is None:
        return "✅ 没大量用 Bash，OK"
    if ratio == float("inf"):
        return "🚨 用了 Bash 但没用 Read，严重失衡"
    if ratio > HEALTHY_BASH_READ_RATIO * 2:
        return f"🚨 Bash/Read = {ratio:.1f}，远超基线 {HEALTHY_BASH_READ_RATIO}"
    if ratio > HEALTHY_BASH_READ_RATIO:
        return f"⚠️ Bash/Read = {ratio:.1f}，高于基线 {HEALTHY_BASH_READ_RATIO}"
    return f"✅ Bash/Read = {ratio:.1f}，健康"


def gen_report_markdown(target_date, items, prev_items=None):
    items = [it for it in items if not is_noise(it)]
    lines = []
    lines.append(f"# Claude Code 日报 — {target_date}")
    lines.append("")

    if not items:
        lines.append("今天没有 Claude Code 实际工作（只有测试输入或空会话）。")
        lines.append("")
        lines.append(USER_MARKER)
        lines.append("")
        lines.append("## 你的反思")
        lines.append("")
        lines.append("- 观察：")
        lines.append("- 约束：")
        return "\n".join(lines) + "\n"

    stats = daily_stats(items, target_date)
    total = stats["total"]
    dur_min = stats["dur_min"]
    user_msgs = stats["user_msgs"]
    bash = total.get("Bash", 0)
    read = total.get("Read", 0)
    edit = total.get("Edit", 0)
    write = total.get("Write", 0)

    # === 深度分析 ===
    bash_analysis = analyze_bash_quality(stats["bash_commands"])
    msg_patterns = analyze_message_patterns(stats["message_turns"], target_date)
    friction = extract_friction_advice(items)
    content_analysis = analyze_message_content(items, target_date)
    derived = compute_derived_metrics(stats)

    # === 概览 ===
    lines.append("## 概览")
    lines.append("")
    proj_counts = Counter(it["project"] for it in items)
    proj_summary = "、".join(f"{p}({c})" for p, c in proj_counts.most_common())
    # 时间范围
    earliest = stats.get("earliest_ts")
    latest = stats.get("latest_ts")
    if earliest and latest:
        time_range = f"{earliest.strftime('%H:%M')}–{latest.strftime('%H:%M')}"
        dur_text = f"活跃 {dur_min} 分钟（{time_range}）"
    else:
        dur_text = f"活跃 {dur_min} 分钟"
    lines.append(f"**{len(items)} 个有效会话 · {dur_text} · 用户消息 {user_msgs} 条 · {sum(total.values())} 次工具调用**")
    lines.append("")
    lines.append(f"项目分布：{proj_summary}")
    lines.append("")

    # === 主要任务 ===
    lines.append("主要任务（按时间倒序）：")
    for it in items[:10]:
        t = it["parsed"]["first_ts"].strftime("%H:%M") if it["parsed"]["first_ts"] else "??:??"
        goal = short(it["parsed"]["first_user"], 60)
        lines.append(f"- `{t}` {it['project']} — {goal}")
    if len(items) > 10:
        lines.append(f"- ...另有 {len(items) - 10} 个会话")
    lines.append("")

    # === 达成与结果（双轨制） ===
    outcome_stats = extract_outcome_stats(items)
    lines.append("## 达成与结果")
    lines.append("")

    # 分离有 facet 和无 facet 的 session
    facet_items = [it for it in items if it["facet"]]
    no_facet_items = [it for it in items if not it["facet"]]

    # 1. facet 数据（如果有）
    if outcome_stats is not None and facet_items:
        o = outcome_stats
        parts = []
        if o["fully_achieved"]:
            parts.append(f"完全达成 {o['fully_achieved']}")
        if o["mostly_achieved"]:
            parts.append(f"大部分达成 {o['mostly_achieved']}")
        if o["partially_achieved"]:
            parts.append(f"部分达成 {o['partially_achieved']}")
        if o["not_achieved"]:
            parts.append(f"未达成 {o['not_achieved']}")
        if o["abandoned"]:
            parts.append(f"放弃 {o['abandoned']}")
        if o["unclear"]:
            parts.append(f"不清楚 {o['unclear']}")
        lines.append(f"**基于 /insight 评估（{o['total']} 个 session）**：")
        lines.append("结果分布：" + (" · ".join(parts) if parts else "无数据"))
        lines.append(f"达成率：{o['achievement_rate']*100:.0f}%")
        if o["satisfaction_rate"] is not None:
            lines.append(f"满意度：{o['satisfied']}/{o['satisfied']+o['likely_satisfied']} 明确满意")
        # 标注跨天警告
        cross_day_facets = []
        for it in facet_items:
            p = it["parsed"]
            if p["first_ts"] and p["last_ts"] and p["first_ts"].date() != p["last_ts"].date():
                cross_day_facets.append(it["session_id"][:8])
        if cross_day_facets:
            lines.append(f"⚠️ 以下 session 跨天，facet 覆盖整个 session 不限于今天：`{'` `'.join(cross_day_facets[:5])}`")
        lines.append("")
        if o["unachieved"]:
            lines.append("未达成会话：")
            for sid, summary, fric in o["unachieved"]:
                s = short(summary, 60)
                extra = f" — {short(fric, 60)}" if fric else ""
                lines.append(f"- `{sid}` {s}{extra}")
            lines.append("")

    # 2. 消息推断数据（无 facet 的 session）
    if no_facet_items:
        ca = content_analysis
        lines.append(f"**基于消息推断（{len(no_facet_items)} 个 session 无 /insight）**：")
        inferred_parts = []
        if ca["has_achievement"]:
            inferred_parts.append(f"推断达成 {ca['achievement_count']}")
        if ca["has_friction"]:
            inferred_parts.append(f"推断有摩擦 {ca['friction_count']}")
        if ca["has_abandon"]:
            inferred_parts.append(f"推断放弃 {ca['abandon_count']}")
        if not inferred_parts:
            inferred_parts.append("信号弱，无明显摩擦或达成标记")
        lines.append(" · ".join(inferred_parts))
        if ca["friction_examples"]:
            lines.append("摩擦信号：")
            for ex in ca["friction_examples"]:
                t = ex["ts"].strftime("%H:%M") if ex["ts"] else "??:??"
                lines.append(f"- `{t}` 「{short(ex['text'], 50)}」→ 触发词「{ex['kw']}」")
        if ca["achievement_examples"]:
            lines.append("达成信号：")
            for ex in ca["achievement_examples"]:
                t = ex["ts"].strftime("%H:%M") if ex["ts"] else "??:??"
                lines.append(f"- `{t}` 「{short(ex['text'], 50)}」→ 触发词「{ex['kw']}」")
        lines.append("")

    # 两者都没有
    if not facet_items and not no_facet_items:
        lines.append("今天没有有效会话数据。")
        lines.append("")

    # === 数据快照 ===
    lines.append("## 数据快照")
    lines.append("")
    lines.append("| 指标 | 今天 | 状态 |")
    lines.append("|---|---|---|")
    ratio = bash_read_ratio(total)
    ratio_str = f"{ratio:.1f}" if isinstance(ratio, float) and ratio != float("inf") else ("∞" if ratio == float("inf") else "-")
    ratio_warn = "✅" if ratio is None or (isinstance(ratio, float) and ratio <= HEALTHY_BASH_READ_RATIO) else "⚠️"
    lines.append(f"| Bash/Read 比 | {bash}:{read} ({ratio_str}) | {ratio_warn} |")
    avg_msgs = user_msgs / max(len(items), 1)
    msgs_warn = "✅" if avg_msgs <= HEALTHY_MSGS_PER_SESSION else "⚠️"
    lines.append(f"| 用户消息数 | {user_msgs} | {msgs_warn} |")
    lines.append(f"| 活跃时长 | {dur_min} 分钟 | - |")
    lines.append(f"| 工具调用 | Bash {bash} · Read {read} · Edit {edit} · Write {write} | - |")
    if outcome_stats:
        ar = outcome_stats["achievement_rate"]
        ar_warn = "✅" if ar >= HEALTHY_ACHIEVEMENT_RATE else "⚠️"
        lines.append(f"| 达成率 | {ar*100:.0f}% | {ar_warn} |")

    # Bash 质量细分
    if bash_analysis.get("total", 0) > 0:
        cr = bash_analysis["could_be_read_count"]
        cr_pct = bash_analysis["could_be_read_pct"]
        lines.append(f"| Bash 质量 | {cr}/{bash} 本可用 Read ({cr_pct:.0f}%) | {'⚠️' if cr_pct > 30 else '✅'} |")
    if msg_patterns["max_consecutive_tools"] > 5:
        lines.append(f"| 工具连发 | 最多连续 {msg_patterns['max_consecutive_tools']} 轮工具调用 | ⚠️ |")
    # 新增衍生指标
    if derived["compact_count"] > 0:
        lines.append(f"| 上下文压缩 | {derived['compact_count']} 次 /compact | {'⚠️' if derived['compact_count'] >= 3 else '✅'} |")
    if derived["idle_turns"] > 0:
        lines.append(f"| 纯对话轮数 | {derived['idle_turns']} 轮（assistant 未调工具）| - |")
    if derived["unique_files_modified"] > 0:
        lines.append(f"| 文件产出 | 修改 {derived['unique_files_modified']} 个文件（Edit {derived['edited_files_count']} · Write {derived['written_files_count']}）| - |")
    if derived["repeat_edit_count"] > 0:
        lines.append(f"| 反复编辑 | {derived['repeat_edit_count']} 个文件被 Edit 2+ 次 | ⚠️ |")
    lines.append("")

    # === 对比昨天 ===
    if prev_items is not None:
        prev_clean = [it for it in prev_items if not is_noise(it)]
        if prev_clean:
            prev_stats = daily_stats(prev_clean, target_date - timedelta(days=1))
            prev_total = prev_stats["total"]
            prev_dur = prev_stats["dur_min"]
            prev_msgs = prev_stats["user_msgs"]
            prev_ratio = bash_read_ratio(prev_total)
            lines.append("**跟昨天对比：**")
            lines.append("")
            if isinstance(ratio, float) and isinstance(prev_ratio, float) and ratio != float("inf") and prev_ratio != float("inf"):
                delta = ratio - prev_ratio
                arrow = "↘ 改善" if delta < -0.1 else ("↗ 恶化" if delta > 0.1 else "→ 持平")
                lines.append(f"- Bash/Read: {ratio:.1f} ← {prev_ratio:.1f}  ({arrow} {delta:+.1f})")
            delta_msgs = user_msgs - prev_msgs
            lines.append(f"- 用户消息: {user_msgs} ← {prev_msgs}  ({delta_msgs:+d})")
            delta_dur = dur_min - prev_dur
            lines.append(f"- 活跃时长: {dur_min} ← {prev_dur} 分钟  ({delta_dur:+d})")
            lines.append("")

    # === 今天的主要摩擦（核心改进） ===
    lines.append("## 今天的主要摩擦")
    lines.append("")

    problems = []

    # 1. Bash 滥用（精准定位）
    if bash_analysis.get("could_be_read_count", 0) >= 3:
        worst = bash_analysis.get("worst_cluster")
        if worst:
            t = worst[0]["ts"].strftime("%H:%M") if worst[0]["ts"] else "??:??"
            sample_cmds = [short(c["cmd"], 40) for c in worst[:3]]
            problems.append(f"**Bash 本可用 Read**：{bash_analysis['could_be_read_count']} 条 Bash 命令本可用 Read/Grep 替代。最密集区段 `{t}` 连续 {len(worst)} 条：{'；'.join(sample_cmds)}")
        else:
            problems.append(f"**Bash 本可用 Read**：{bash_analysis['could_be_read_count']} 条 Bash 命令本可用 Read/Grep 替代")

    # 2. 消息密度
    avg_msgs = user_msgs / max(len(items), 1)
    if avg_msgs > HEALTHY_MSGS_PER_SESSION:
        avg = user_msgs / max(len(items), 1)
        problems.append(f"**消息密度高**：{user_msgs} 条消息 / {len(items)} 会话 ≈ 每会话 {avg:.0f} 条。说明你在反复修正而不是一次说清。")

    # 3. 频繁打断
    interrupts = sum(it["parsed"]["interruptions"] for it in items)
    if interrupts > 3:
        problems.append(f"**频繁打断**：今天打断 Claude {interrupts} 次。说明你扔出去的 prompt 跟你想要的不匹配，从源头改 prompt 更省力。")

    # 4. 只探索没落地
    edit_write = edit + write
    if edit_write < 5 and dur_min > 60:
        problems.append(f"**只在探索没在改代码**：{dur_min} 分钟但 Edit/Write 只有 {edit_write} 次。今天主要在调试/搜索/聊天，不在落地实现。")

    # 5. 工具连发
    if msg_patterns["max_consecutive_tools"] > 8:
        problems.append(f"**工具连发**：最多连续 {msg_patterns['max_consecutive_tools']} 轮工具调用，Claude 可能在兜圈子或没理解目标。")

    # 6. facet 摩擦
    if friction:
        fric_summary = ", ".join(f"{k}×{v}" for k, v in list(friction["total_frics"].items())[:3])
        problems.append(f"**会话摩擦（/insight）**：{fric_summary}")
        if friction["details"]:
            sid, detail = friction["details"][0]
            problems.append(f"  - 典型例子 (`{sid}`)：{short(detail, 100)}")

    # 7. 消息推断摩擦（无 facet 时）
    if content_analysis["has_friction"] and not friction:
        examples = content_analysis["friction_examples"]
        if examples:
            t = examples[0]["ts"].strftime("%H:%M") if examples[0]["ts"] else "??:??"
            problems.append(f"**消息推断摩擦**：用户 {content_analysis['friction_count']} 次表达否定/纠偏。例如 `{t}` 「{short(examples[0]['text'], 40)}」")

    # 8. 短消息密集（反复修正信号）
    if content_analysis["short_msg_count"] >= 5 and not friction:
        problems.append(f"**反复短消息修正**：{content_analysis['short_msg_count']} 条短消息（<20字），说明在逐句投喂而非一次说清")

    # 9. 反复编辑同一文件
    if derived["repeat_edit_count"] > 0:
        repeat_list = ", ".join(f"`{short(p, 30)}`×{c}" for p, c in list(derived["repeat_edits"].items())[:3])
        problems.append(f"**反复编辑**：{repeat_list}——可能说明方案没想清楚就动手")

    if not problems:
        problems.append("数据上没自动发现明显问题。今天可能用得不错，或者数据信号弱。")

    for p in problems:
        lines.append(f"- {p}")
    lines.append("")

    # === 明天的改进候选（预填充，核心改进） ===
    lines.append("## 明天的改进候选")
    lines.append("")

    # 基于摩擦数据或问题检测生成精准建议（双轨）
    suggestions = []

    # 优先用 facet 建议
    if friction:
        suggestions.append(f"**{friction['observation']}** —— {friction['constraint']}")
    # 次之用消息推断建议
    elif content_analysis["top_fric_type"]:
        suggestions.append(f"**{content_analysis['top_fric_obs']}** —— {content_analysis['top_fric_constraint']}")
    elif bash_analysis.get("could_be_read_count", 0) >= 10 or bash_analysis.get("could_be_read_pct", 0) > 15:
        suggestions.append("**Bash 滥用** —— 下次想敲 Bash 前停 3 秒：这个命令是不是在'读文件'？是的话用 Read/Grep。")
    elif avg_msgs > HEALTHY_MSGS_PER_SESSION:
        suggestions.append("**消息太多** —— 开新会话前写 3 行需求草稿：目标、约束、验收标准。")
    elif interrupts > 3:
        suggestions.append("**频繁打断** —— 想打断时先问自己：是 Claude 跑偏了，还是我没说清？")
    elif edit_write < 5 and dur_min > 60:
        suggestions.append("**落地不足** —— 开会话前先决定：今天要输出什么？不要混在探索里。")
    elif derived["repeat_edit_count"] > 0:
        suggestions.append("**反复编辑** —— 改前先想清楚方案，不要边写边试。")
    elif content_analysis["short_msg_count"] >= 5:
        suggestions.append("**短消息太多** —— 一次说清需求，不要逐句投喂。")
    else:
        suggestions.append("今天没明显坏习惯。明天保持节奏即可。")

    for s in suggestions:
        lines.append(f"- {s}")
    lines.append("")

    # === /insight 摩擦详情（facet 原始数据） ===
    if friction and friction["details"]:
        lines.append("## /insight 摩擦详情")
        lines.append("")
        for sid, detail in friction["details"][:5]:
            lines.append(f"- `{sid}` {short(detail, 120)}")
        lines.append("")

    lines.append(REGEN_MARKER)
    lines.append("")
    lines.append(USER_MARKER)
    lines.append("")
    lines.append("## 你的反思")
    lines.append("")

    # 预填充反思区
    if friction:
        lines.append(f"- 观察：{friction['observation']}（{friction['top_type']} ×{friction['top_count']}）")
        lines.append(f"- 约束：{friction['constraint']}")
    elif content_analysis["top_fric_type"]:
        lines.append(f"- 观察：{content_analysis['top_fric_obs']}（消息推断）")
        lines.append(f"- 约束：{content_analysis['top_fric_constraint']}")
    elif bash_analysis.get("could_be_read_count", 0) >= 3:
        lines.append(f"- 观察：今天 {bash_analysis['could_be_read_count']} 条 Bash 本可用 Read 替代")
        lines.append("- 约束：读文件用 Read，只有系统命令才用 Bash")
    elif avg_msgs > HEALTHY_MSGS_PER_SESSION:
        lines.append("- 观察：消息密度高，反复修正")
        lines.append("- 约束：一次说清，不要逐句投喂")
    elif derived["repeat_edit_count"] > 0:
        lines.append(f"- 观察：{derived['repeat_edit_count']} 个文件反复编辑")
        lines.append("- 约束：改前先写方案，想清楚再动手")
    else:
        lines.append("- 观察：")
        lines.append("- 约束：")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_report(target_date, content, regen=False):
    """写报告文件，保留用户反思区域。"""
    path = REPORTS_DIR / f"{target_date}.md"
    if path.exists() and not regen:
        existing = path.read_text(encoding="utf-8")
        if USER_MARKER in existing:
            existing_user_part = existing.split(USER_MARKER, 1)[1]
            new_top = content.split(USER_MARKER, 1)[0]
            content = new_top + USER_MARKER + existing_user_part
    path.write_text(content, encoding="utf-8")
    return path


def print_summary(target_date, items):
    items = [it for it in items if not is_noise(it)]
    if not items:
        print(f"📅 {target_date}：没有有效会话")
        return
    stats = daily_stats(items, target_date)
    total = stats["total"]
    dur_min = stats["dur_min"]
    user_msgs = stats["user_msgs"]
    ratio = bash_read_ratio(total)
    ratio_str = f"{ratio:.1f}" if isinstance(ratio, float) and ratio != float("inf") else "-"
    status = health_status(total)
    bash_analysis = analyze_bash_quality(stats["bash_commands"])
    fric = extract_friction_advice(items)
    extra = ""
    if bash_analysis.get("could_be_read_count", 0) > 0:
        extra += f" | Read替Bash:{bash_analysis['could_be_read_count']}"
    if fric:
        extra += f" | 摩擦:{fric['top_type']}×{fric['top_count']}"
    print(f"📅 {target_date} | {len(items)} 会话 | {dur_min} 分钟 | {user_msgs} 消息 | Bash/Read {ratio_str}{extra} | {status}")


def main():
    args = parse_args()
    if args.help:
        print(__doc__)
        return

    if args.week:
        today = date.today()
        start = today - timedelta(days=today.weekday())
        print(f"\n本周（{start} → {today}）按天统计：\n")
        for i in range((today - start).days + 1):
            d = start + timedelta(days=i)
            items = load_sessions(d, d)
            print_summary(d, items)
        print()
        return

    kind, val = parse_target(args.target)

    if kind == "range":
        today = date.today()
        start = today - timedelta(days=val - 1)
        print(f"\n最近 {val} 天 ({start} → {today})：\n")
        for i in range(val):
            d = start + timedelta(days=i)
            items = load_sessions(d, d)
            print_summary(d, items)
        print()
        items = load_sessions(today, today)
        prev_items = load_sessions(today - timedelta(days=1), today - timedelta(days=1))
        report = gen_report_markdown(today, items, prev_items)
        target_date = today
    else:
        target_date = val
        items = load_sessions(target_date, target_date)
        prev_items = load_sessions(target_date - timedelta(days=1), target_date - timedelta(days=1))
        report = gen_report_markdown(target_date, items, prev_items)
        print()
        print_summary(target_date, items)
        print()

    if args.print_only:
        print(report)
        return

    path = write_report(target_date, report, regen=args.regen)
    print(f"报告写入：{path}")

    if not args.quiet:
        try:
            subprocess.run(["open", str(path)], check=False)
        except Exception as e:
            print(f"(自动打开失败: {e})")


if __name__ == "__main__":
    main()
