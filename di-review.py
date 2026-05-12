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
    tool_counts = Counter()
    first_user = None
    first_ts = None
    last_ts = None
    user_msgs = 0
    interruptions = 0
    short_msg = False
    for line in path.open(encoding="utf-8", errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("timestamp")
        if ts:
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                if first_ts is None:
                    first_ts = t
                last_ts = t
            except Exception:
                pass
        if d.get("type") == "user":
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
        elif d.get("type") == "assistant":
            blocks = d.get("message", {}).get("content", [])
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_counts[block.get("name", "?")] += 1
        if '"interrupted":true' in line:
            interruptions += 1
    return {
        "tool_counts": tool_counts,
        "first_user": first_user,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "user_msgs": user_msgs,
        "interruptions": interruptions,
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


def daily_stats(items):
    total = Counter()
    user_msgs = 0
    dur_min = 0
    for it in items:
        for k, v in it["parsed"]["tool_counts"].items():
            total[k] += v
        user_msgs += it["parsed"]["user_msgs"]
        if it["parsed"]["first_ts"] and it["parsed"]["last_ts"]:
            dur_min += (it["parsed"]["last_ts"] - it["parsed"]["first_ts"]).total_seconds() / 60
    return total, int(dur_min), user_msgs


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

    total, dur_min, user_msgs = daily_stats(items)
    bash = total.get("Bash", 0)
    read = total.get("Read", 0)
    edit = total.get("Edit", 0)
    write = total.get("Write", 0)

    lines.append("## 今天做了什么")
    lines.append("")
    proj_counts = Counter(it["project"] for it in items)
    proj_summary = "、".join(f"{p}({c})" for p, c in proj_counts.most_common())
    lines.append(f"**{len(items)} 个有效会话 · {dur_min} 分钟 · 用户消息 {user_msgs} 条**")
    lines.append("")
    lines.append(f"项目分布：{proj_summary}")
    lines.append("")
    lines.append("主要任务（按时间倒序）：")
    for it in items[:10]:
        t = it["parsed"]["first_ts"].strftime("%H:%M") if it["parsed"]["first_ts"] else "??:??"
        goal = short(it["parsed"]["first_user"], 60)
        lines.append(f"- `{t}` {it['project']} — {goal}")
    if len(items) > 10:
        lines.append(f"- ...另有 {len(items) - 10} 个会话")
    lines.append("")

    lines.append("## 数据健康度")
    lines.append("")
    lines.append("| 指标 | 今天 | 健康基线 | 状态 |")
    lines.append("|---|---|---|---|")
    ratio = bash_read_ratio(total)
    ratio_str = f"{ratio:.1f}" if isinstance(ratio, float) and ratio != float("inf") else ("∞" if ratio == float("inf") else "-")
    ratio_warn = "✅" if ratio is None or (isinstance(ratio, float) and ratio <= HEALTHY_BASH_READ_RATIO) else "⚠️"
    lines.append(f"| Bash/Read 比 | {ratio_str} | < {HEALTHY_BASH_READ_RATIO} | {ratio_warn} |")
    msgs_warn = "✅" if user_msgs <= HEALTHY_DAILY_MSGS else "⚠️"
    lines.append(f"| 用户消息数 | {user_msgs} | < {HEALTHY_DAILY_MSGS} | {msgs_warn} |")
    lines.append(f"| 总工具调用 | {sum(total.values())} | - | - |")
    lines.append(f"| Bash · Read · Edit · Write | {bash} · {read} · {edit} · {write} | - | - |")
    lines.append("")

    if prev_items is not None:
        prev_clean = [it for it in prev_items if not is_noise(it)]
        if prev_clean:
            prev_total, prev_dur, prev_msgs = daily_stats(prev_clean)
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
            lines.append(f"- 时长: {dur_min} ← {prev_dur} 分钟  ({delta_dur:+d})")
            lines.append("")

    lines.append("## 检测到的问题")
    lines.append("")
    problems = []
    if isinstance(ratio, float) and ratio > HEALTHY_BASH_READ_RATIO:
        worst = sorted(items, key=lambda it: it["parsed"]["tool_counts"].get("Bash", 0), reverse=True)[:3]
        worst_str = "、".join(f"`{it['session_id'][:8]}`(Bash×{it['parsed']['tool_counts'].get('Bash',0)})" for it in worst)
        problems.append(f"**Bash 滥用**：今天 Bash {bash} 次 vs Read {read} 次。Bash 用得最多的会话：{worst_str}。这些里有多少是本可以 Read/Grep 的？")
    if user_msgs > HEALTHY_DAILY_MSGS:
        avg = user_msgs / max(len(items), 1)
        problems.append(f"**消息密度高**：{user_msgs} 条消息 / {len(items)} 会话 ≈ 每会话 {avg:.0f} 条。说明你在反复修正而不是一次说清。")
    interrupts = sum(it["parsed"]["interruptions"] for it in items)
    if interrupts > 3:
        problems.append(f"**频繁打断**：今天打断 Claude {interrupts} 次。说明你扔出去的 prompt 跟你想要的不匹配，从源头改 prompt 更省力。")
    edit_write = edit + write
    if edit_write < 5 and dur_min > 120:
        problems.append(f"**只在探索没在改代码**：{dur_min} 分钟但 Edit/Write 只有 {edit_write} 次。今天主要在调试/搜索/聊天，不在落地实现。")
    if not problems:
        problems.append("数据上没自动发现明显问题。今天可能用得不错，或者数据信号弱。")
    for p in problems:
        lines.append(f"- {p}")
    lines.append("")

    lines.append("## 给你的改进建议")
    lines.append("")
    suggestions = []
    if isinstance(ratio, float) and ratio > HEALTHY_BASH_READ_RATIO:
        suggestions.append("明天开会话第一句加上：**'诊断前先一句假设，能 Read/Grep 就别 Bash。'**")
    if user_msgs > HEALTHY_DAILY_MSGS:
        suggestions.append("明天试一次：开新会话前先在 daily-improvement.md 写 3 行需求草稿再发，不要随手就发。")
    if interrupts > 3:
        suggestions.append("明天每次想打断 Claude 时，先问自己：'是 Claude 跑偏了，还是我没说清？' 是后者就记下来。")
    if edit_write < 5 and dur_min > 120 and len(items) > 0:
        suggestions.append("明天开会话前先决定：**今天是要落地东西，还是要调试基础设施。** 不要混在一起。")
    if not suggestions:
        suggestions.append("今天没明显坏习惯。明天保持今天的节奏即可。")
    for s in suggestions:
        lines.append(f"- {s}")
    lines.append("")

    facet_frics = []
    for it in items:
        f = it["facet"]
        if f and f.get("session_id") == it["session_id"]:
            fr = f.get("friction_detail")
            if fr:
                facet_frics.append((it["session_id"][:8], fr))
    if facet_frics:
        lines.append("## /insight 给出的摩擦细节（如果有）")
        lines.append("")
        for sid, fr in facet_frics:
            lines.append(f"- `{sid}` {fr}")
        lines.append("")

    lines.append(REGEN_MARKER)
    lines.append("")
    lines.append(USER_MARKER)
    lines.append("")
    lines.append("## 你的反思")
    lines.append("")
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
    total, dur_min, user_msgs = daily_stats(items)
    ratio = bash_read_ratio(total)
    ratio_str = f"{ratio:.1f}" if isinstance(ratio, float) and ratio != float("inf") else "-"
    status = health_status(total)
    print(f"📅 {target_date} | {len(items)} 会话 | {dur_min} 分钟 | {user_msgs} 消息 | Bash/Read {ratio_str} | {status}")


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
