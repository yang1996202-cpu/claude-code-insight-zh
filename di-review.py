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
HEALTHY_DAILY_MSGS = 80

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
    # 消息级数据
    bash_commands = []  # [(timestamp, command, could_be_read)]
    read_files = []     # [(timestamp, file_path)]
    message_turns = []  # [{'ts': datetime, 'type': 'user'|'assistant', 'has_tool': bool}]

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
            if first_user is None:
                content = d.get("message", {}).get("content")
                if isinstance(content, str):
                    first_user = content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            first_user = blk.get("text", "")
                            break
            # 检查是否包含 tool_result（即对 assistant 工具的回应）
            has_tool_result = False
            content = d.get("message", {}).get("content")
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
        "bash_commands": bash_commands,
        "read_files": read_files,
        "message_turns": message_turns,
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

    for it in items:
        p = it["parsed"]
        for k, v in p["tool_counts"].items():
            total[k] += v
        user_msgs += p["user_msgs"]

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

        all_bash.extend(p["bash_commands"])
        all_reads.extend(p["read_files"])
        all_turns.extend(p["message_turns"])

    # 计算当天最早和最晚消息时间
    all_day_ts = sorted([t["ts"] for t in all_turns if t["ts"] and (target_date is None or t["ts"].date() == target_date)])
    earliest_ts = all_day_ts[0] if all_day_ts else None
    latest_ts = all_day_ts[-1] if all_day_ts else None

    return {
        "total": total,
        "dur_min": int(dur_min),
        "user_msgs": user_msgs,
        "bash_commands": all_bash,
        "read_files": all_reads,
        "message_turns": all_turns,
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

    # === 数据快照 ===
    lines.append("## 数据快照")
    lines.append("")
    lines.append("| 指标 | 今天 | 状态 |")
    lines.append("|---|---|---|")
    ratio = bash_read_ratio(total)
    ratio_str = f"{ratio:.1f}" if isinstance(ratio, float) and ratio != float("inf") else ("∞" if ratio == float("inf") else "-")
    ratio_warn = "✅" if ratio is None or (isinstance(ratio, float) and ratio <= HEALTHY_BASH_READ_RATIO) else "⚠️"
    lines.append(f"| Bash/Read 比 | {bash}:{read} ({ratio_str}) | {ratio_warn} |")
    msgs_warn = "✅" if user_msgs <= HEALTHY_DAILY_MSGS else "⚠️"
    lines.append(f"| 用户消息数 | {user_msgs} | {msgs_warn} |")
    lines.append(f"| 活跃时长 | {dur_min} 分钟 | - |")
    lines.append(f"| 工具调用 | Bash {bash} · Read {read} · Edit {edit} · Write {write} | - |")

    # Bash 质量细分
    if bash_analysis.get("total", 0) > 0:
        cr = bash_analysis["could_be_read_count"]
        cr_pct = bash_analysis["could_be_read_pct"]
        lines.append(f"| Bash 质量 | {cr}/{bash} 本可用 Read ({cr_pct:.0f}%) | {'⚠️' if cr_pct > 30 else '✅'} |")
    if msg_patterns["max_consecutive_tools"] > 5:
        lines.append(f"| 工具连发 | 最多连续 {msg_patterns['max_consecutive_tools']} 轮工具调用 | ⚠️ |")
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
    if user_msgs > HEALTHY_DAILY_MSGS:
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
        problems.append(f"**会话摩擦**：{fric_summary}")
        if friction["details"]:
            sid, detail = friction["details"][0]
            problems.append(f"  - 典型例子 (`{sid}`)：{short(detail, 100)}")
    elif not problems:
        problems.append("数据上没自动发现明显问题。今天可能用得不错，或者数据信号弱。")

    for p in problems:
        lines.append(f"- {p}")
    lines.append("")

    # === 明天的改进候选（预填充，核心改进） ===
    lines.append("## 明天的改进候选")
    lines.append("")

    # 基于摩擦数据或问题检测生成精准建议
    suggestions = []

    if friction:
        suggestions.append(f"**{friction['observation']}** —— {friction['constraint']}")
    elif bash_analysis.get("could_be_read_count", 0) >= 3:
        suggestions.append("**Bash 滥用** —— 下次想敲 Bash 前停 3 秒：这个命令是不是在'读文件'？是的话用 Read/Grep。")
    elif user_msgs > HEALTHY_DAILY_MSGS:
        suggestions.append("**消息太多** —— 开新会话前写 3 行需求草稿：目标、约束、验收标准。")
    elif interrupts > 3:
        suggestions.append("**频繁打断** —— 想打断时先问自己：是 Claude 跑偏了，还是我没说清？")
    elif edit_write < 5 and dur_min > 60:
        suggestions.append("**落地不足** —— 开会话前先决定：今天要输出什么？不要混在探索里。")
    else:
        suggestions.append("今天没明显坏习惯。明天保持节奏即可。")

    for s in suggestions:
        lines.append(f"- {s}")
    lines.append("")

    # === /insight 摩擦详情 ===
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
    elif bash_analysis.get("could_be_read_count", 0) >= 3:
        lines.append(f"- 观察：今天 {bash_analysis['could_be_read_count']} 条 Bash 本可用 Read 替代")
        lines.append("- 约束：读文件用 Read，只有系统命令才用 Bash")
    elif user_msgs > HEALTHY_DAILY_MSGS:
        lines.append("- 观察：消息密度高，反复修正")
        lines.append("- 约束：一次说清，不要逐句投喂")
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
