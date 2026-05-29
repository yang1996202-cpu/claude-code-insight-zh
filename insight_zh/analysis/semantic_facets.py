from collections import Counter
from pathlib import Path
import re


QUESTION_RE = re.compile(r"[?？]|怎么|如何|为什么|什么|区别|对比|能不能|可以吗")
COMPLETION_RE = re.compile(r"好了|可以了|搞定|完成|提交|推送|push|commit|没问题|就这样|对了|满意", re.I)
ABANDON_RE = re.compile(r"算了|先这样|先放着|不搞了|跳过|回头再|明天再说")
NEGATIVE_RE = re.compile(r"不对|错了|不是|别|不要|没理解|误解|跑偏|失败|报错|bug|奇怪|抽象|不清楚")

GOAL_KEYWORDS = {
    "代码与实现": ["实现", "开发", "添加", "修", "改", "脚本", "测试", "报告", "html", "缓存", "分析器"],
    "调试与排障": ["不对", "错", "失败", "报错", "为什么", "咋回事", "debug", "bug"],
    "Git 操作": ["git", "commit", "push", "github", "提交", "推送"],
    "数据分析": ["统计", "数据", "准确", "源头", "维度", "分析", "insight", "facets"],
    "架构讨论": ["逻辑", "缓存层", "分析层", "优先级", "设计", "架构", "怎么判断"],
    "清理维护": ["删除", "清理", "历史记录", "文件夹", "过期", "stale"],
}


def _clean_text(text, limit=160):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"<[^>]+>", "", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _basename(path):
    try:
        return Path(path).name or str(path)
    except Exception:
        return str(path)


def _topic_labels(texts):
    blob = " ".join(texts).lower()
    labels = []
    for label, keywords in GOAL_KEYWORDS.items():
        if any(keyword.lower() in blob for keyword in keywords):
            labels.append(label)
    return labels


def _important_user_texts(texts, limit=3):
    scored = []
    for idx, text in enumerate(texts):
        t = _clean_text(text, 240)
        if not t:
            continue
        score = 0
        if QUESTION_RE.search(t):
            score += 2
        if NEGATIVE_RE.search(t):
            score += 3
        if COMPLETION_RE.search(t):
            score += 2
        score += min(len(t) // 80, 2)
        scored.append((score, -idx, t))
    scored.sort(reverse=True)
    picked = [item[2] for item in scored[:limit]]
    if picked:
        return picked
    return [_clean_text(texts[0], 240)] if texts else []


def _outcome_from_signals(user_texts, edit_write, read_count, bash_count, git_commits, friction_total):
    blob = " ".join(user_texts)
    completed = bool(COMPLETION_RE.search(blob))
    abandoned = bool(ABANDON_RE.search(blob))
    corrected = bool(NEGATIVE_RE.search(blob))

    if git_commits > 0:
        return "fully_achieved"
    if abandoned and not completed:
        return "not_achieved"
    if corrected and edit_write < 3 and not completed:
        return "partially_achieved"
    if friction_total >= 4 and not completed:
        return "partially_achieved"
    if edit_write >= 5 and friction_total <= 1:
        return "mostly_achieved"
    if edit_write >= 2 and completed:
        return "mostly_achieved"
    if edit_write >= 2:
        return "partially_achieved" if friction_total else "mostly_achieved"
    if read_count + bash_count >= 8 and friction_total >= 2:
        return "partially_achieved"
    if completed:
        return "mostly_achieved"
    if user_texts and all(QUESTION_RE.search(t) for t in user_texts[: min(len(user_texts), 2)]) and friction_total == 0:
        return "mostly_achieved"
    return "unclear_from_transcript"


def _success_from_signals(outcome, tool_counts, edit_write, git_commits, topic_labels):
    if outcome in ("not_achieved", "unclear_from_transcript"):
        return None
    if git_commits > 0:
        return "correct_code_edits"
    if edit_write >= 3:
        return "multi_file_changes" if edit_write >= 6 else "correct_code_edits"
    if "调试与排障" in topic_labels:
        return "good_debugging"
    if tool_counts.get("Read", 0) >= 3:
        return "fast_accurate_search"
    return "good_explanations"


def _brief_summary(goal, labels, outcome, tool_counts, edit_targets, write_targets, friction_detail):
    label_text = "、".join(labels[:3]) if labels else "任务处理"
    action_bits = []
    edit_write = tool_counts.get("Edit", 0) + tool_counts.get("Write", 0)
    if edit_write:
        target_count = len(set(edit_targets) | set(write_targets))
        if target_count:
            action_bits.append(f"修改/写入 {target_count} 个文件")
        else:
            action_bits.append(f"执行 {edit_write} 次代码编辑")
    if tool_counts.get("Read", 0):
        action_bits.append(f"读取 {tool_counts.get('Read', 0)} 次文件")
    if tool_counts.get("Bash", 0):
        action_bits.append(f"运行 {tool_counts.get('Bash', 0)} 次命令")
    if not action_bits:
        action_bits.append("以讨论和判断为主")

    outcome_text = {
        "fully_achieved": "并形成了可落地结果",
        "mostly_achieved": "整体推进到了可用状态",
        "partially_achieved": "但过程仍有卡点或未完全收束",
        "not_achieved": "最终没有收束成结果",
        "unclear_from_transcript": "结果无法仅凭记录确认",
    }.get(outcome, "结果不明")

    summary = f"围绕「{_clean_text(goal, 90)}」展开，主题偏向{label_text}；过程主要是{ '、'.join(action_bits[:3]) }，{outcome_text}。"
    if friction_detail and outcome in ("partially_achieved", "not_achieved"):
        summary += f" 主要摩擦：{_clean_text(friction_detail, 90)}"
    return summary


def enhance_facet_semantics(session, facet):
    """Fill official-/insights-like semantic fields when official facets are absent.

    The zh cache stores analyzer output, not raw material. This layer is the
    deterministic semantic analyzer for sessions that do not have an official
    `/insights` facet yet. Official facet values still win.
    """
    facet = dict(facet or {})
    if facet.get("_source") == "jsonl+facet":
        facet.setdefault("semantic_confidence", "official")
        return facet

    tool_counts = Counter(session.tool_counts or {})
    user_texts = [str(t).strip() for t in (session.all_user_texts or []) if str(t).strip()]
    raw_jsonl = session.raw_jsonl or {}
    edit_targets = [
        item.get("path")
        for item in raw_jsonl.get("edited_files", [])
        if isinstance(item, dict) and item.get("path")
    ]
    write_targets = [
        item.get("path")
        for item in raw_jsonl.get("written_files", [])
        if isinstance(item, dict) and item.get("path")
    ]
    read_targets = [
        item.get("path")
        for item in raw_jsonl.get("read_files", [])
        if isinstance(item, dict) and item.get("path")
    ]

    labels = _topic_labels(user_texts)
    important_texts = _important_user_texts(user_texts)
    first_prompt = _clean_text(session.first_prompt or (user_texts[0] if user_texts else ""), 160)
    goal = facet.get("underlying_goal") if facet.get("underlying_goal") and facet.get("underlying_goal") != "未记录" else first_prompt
    if labels and goal:
        goal = f"{goal}"

    friction_counts = dict(facet.get("friction_counts") or {})
    friction_total = sum(int(v or 0) for v in friction_counts.values())
    friction_detail = str(facet.get("friction_detail") or "").strip()
    if not friction_detail and important_texts:
        negative_samples = [t for t in important_texts if NEGATIVE_RE.search(t)]
        if negative_samples:
            friction_detail = "用户明确提出卡点或纠偏：" + _clean_text(negative_samples[0], 140)
    if not friction_detail:
        read_bash = tool_counts.get("Read", 0) + tool_counts.get("Bash", 0)
        edit_write = tool_counts.get("Edit", 0) + tool_counts.get("Write", 0)
        if read_bash >= 12 and edit_write == 0:
            sample_targets = ", ".join(_basename(p) for p in (read_targets[:3] or edit_targets[:3]))
            suffix = f"（主要查看：{sample_targets}）" if sample_targets else ""
            friction_detail = f"大量读取/命令探查后没有进入编辑或交付，可能停留在定位阶段{suffix}。"

    edit_write = tool_counts.get("Edit", 0) + tool_counts.get("Write", 0)
    outcome = facet.get("outcome") or _outcome_from_signals(
        user_texts=user_texts,
        edit_write=edit_write,
        read_count=tool_counts.get("Read", 0),
        bash_count=tool_counts.get("Bash", 0),
        git_commits=session.git_pushes,
        friction_total=friction_total,
    )
    primary_success = facet.get("primary_success") or _success_from_signals(
        outcome=outcome,
        tool_counts=tool_counts,
        edit_write=edit_write,
        git_commits=session.git_pushes,
        topic_labels=labels,
    )
    helpfulness = facet.get("claude_helpfulness")
    if not helpfulness:
        helpfulness = {
            "fully_achieved": "helpful",
            "mostly_achieved": "helpful",
            "partially_achieved": "moderately_helpful",
            "not_achieved": "not_helpful",
            "unclear_from_transcript": None,
        }.get(outcome)

    satisfaction = facet.get("user_satisfaction_counts") or {}
    if not satisfaction:
        satisfaction = {
            "fully_achieved": {"satisfied": 1},
            "mostly_achieved": {"likely_satisfied": 1},
            "partially_achieved": {"neutral": 1},
            "not_achieved": {"frustrated": 1},
        }.get(outcome, {})

    summary = facet.get("brief_summary")
    if not summary or summary == facet.get("underlying_goal"):
        summary = _brief_summary(
            goal=goal or "未记录",
            labels=labels,
            outcome=outcome,
            tool_counts=tool_counts,
            edit_targets=edit_targets,
            write_targets=write_targets,
            friction_detail=friction_detail,
        )

    if not facet.get("goal_categories") and labels:
        category_map = {
            "代码与实现": "feature_implementation",
            "调试与排障": "debugging",
            "Git 操作": "git_management",
            "数据分析": "data_analysis",
            "架构讨论": "system_architecture",
            "清理维护": "cleanup_maintenance",
        }
        facet["goal_categories"] = {category_map[label]: 1 for label in labels if label in category_map}

    facet.update({
        "underlying_goal": goal or "未记录",
        "brief_summary": summary,
        "outcome": outcome,
        "claude_helpfulness": helpfulness,
        "primary_success": primary_success,
        "friction_detail": friction_detail,
        "user_satisfaction_counts": satisfaction,
        "semantic_topics": labels,
        "semantic_evidence": important_texts,
        "semantic_confidence": "heuristic",
        "_semantic_source": "insight-zh-semantic-v1",
    })
    return facet
