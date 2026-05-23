import re
from collections import Counter

from insight_zh.domain.session import coerce_int


JSONL_TOPIC_TO_GOAL_CATEGORY = {
    "skill": "skill_management",
    "config": "configuration_setup",
    "debug": "debugging",
    "explore": "question_answering",
    "create": "feature_implementation",
    "doc": "write_documentation",
    "git": "git_management",
    "test": "question_answering",
}

INFERRED_FRICTION_PATTERNS = {
    "misunderstood_request": ["没理解", "误解", "搞错", "跑偏", "偏了", "方向错了", "不是这样"],
    "user_rejected_action": ["不要", "停", "换个", "重来", "先别", "回退"],
    "buggy_code": ["报错", "错误", "bug", "不行", "失败", "崩溃", "异常"],
    "wrong_approach": ["不对", "不对劲", "不合适", "不应该", "方向错"],
    "environment_issue": ["权限", "代理", "网络", "环境", "安装", "配置", "mcp", "模型不可用"],
}

MESSAGE_FRICTION_SIGNALS = [
    "不对", "错了", "重来", "不是", "不要", "停", "换个", "跑偏", "偏了",
    "没理解", "误解", "搞错", "搞混", "混乱", "不对头",
    "不要这样", "不是这样", "方向错了", "走偏", "不对劲儿",
]

ACHIEVEMENT_SIGNALS = [
    "好了", "可以了", "搞定", "完成", "谢谢", "完美", "不错", "ok",
    "解决了", "搞定了", "没问题", "成了", "满足了", "符合", "满意",
]

ABANDON_SIGNALS = [
    "算了", "先这样", "明天再说", "先放着", "暂时", "以后再", "回头",
    "搁置", "放下", "不搞了", "先不搞", "跳过",
]

QUESTION_SIGNALS = ["怎么", "如何", "什么", "为什么", "区别", "对比", "?", "？"]

TOPIC_KEYWORDS = {
    "skill": ["skill", "技能", "/skill", "skill-manager", "stay-awake"],
    "config": ["配置", "设置", "mcp", "claude.md", "settings", "权限"],
    "debug": ["bug", "调试", "报错", "错误", "不行", "不对", "错了", "失败"],
    "explore": ["怎么", "如何", "什么是", "区别", "对比", "为什么"],
    "create": ["写一个", "做一个", "开发", "实现", "添加", "创建"],
    "doc": ["README", "文档", "注释", "说明", "整理"],
    "git": ["git", "commit", "push", "仓库", "开源", "license", "GPL", "MIT", "Apache"],
    "test": ["测试", "验证", "看看", "试一下", "行不行", "你好", "你叫", "你是谁"],
}

DEBUG_KEYWORDS = ["不行", "不对", "错了", "失败", "报错", "错误", "bug", "debug", "调试", "异常", "崩溃", "卡住", "没反应"]
CREATE_KEYWORDS = ["做一个", "写一个", "开发", "实现", "添加", "创建", "设计", "封装", "开源", "发布", "skill", "项目"]
LEARN_KEYWORDS = ["区别", "什么是", "为什么", "怎么", "如何", "对比", "比较", "原理", "机制", "概念", "介绍"]
ORGANIZE_KEYWORDS = ["整理", "清理", "删除", "移除", "归档", "分类", "统计", "检查", "audit", "review"]
GREETING_TOKENS = ["你好", "你叫", "你是谁", "在吗", "测试"]


def safe_keyword_match(text, keyword):
    pattern = r'(?<![一-鿿\w])' + re.escape(keyword) + r'(?![一-鿿\w])'
    return bool(re.search(pattern, text, re.IGNORECASE))


def get_git_push_count(meta):
    return coerce_int(meta.get("git_pushes", meta.get("git_commits", 0)))


def infer_topic_hits(all_user_texts):
    all_text_lower = " ".join(all_user_texts).lower()
    topic_hits = Counter()
    for topic, keywords in TOPIC_KEYWORDS.items():
        for keyword in keywords:
            if keyword.lower() in all_text_lower:
                topic_hits[topic] += 1
                break
    return topic_hits


def infer_goal_categories(first_prompt, topic_hits, edit_write):
    categories = Counter()
    for topic, count in topic_hits.items():
        mapped = JSONL_TOPIC_TO_GOAL_CATEGORY.get(topic)
        if mapped and count > 0:
            categories[mapped] += count

    first_prompt_lower = first_prompt.lower()
    if not categories and any(sig in first_prompt_lower for sig in QUESTION_SIGNALS):
        categories["question_answering"] += 1
    if edit_write > 0 and not any(key in categories for key in ["feature_implementation", "write_documentation"]):
        categories["feature_implementation"] += 1
    if not categories:
        categories["information_query"] += 1

    return dict(categories)


def infer_friction_counts(all_user_texts):
    counts = Counter()
    samples = []

    for text in all_user_texts:
        normalized = text.strip()
        if not normalized:
            continue
        text_lower = normalized.lower()
        matched = None
        for friction_type, keywords in INFERRED_FRICTION_PATTERNS.items():
            if any(safe_keyword_match(text_lower, keyword.lower()) for keyword in keywords):
                matched = friction_type
                break
        if matched:
            counts[matched] += 1
            if len(samples) < 3:
                samples.append(normalized[:200])

    return dict(counts), (samples[0] if samples else "")


def infer_session_labels(all_user_texts, tool_counts, edit_write, git_pushes, friction_counts):
    texts = [text.strip() for text in all_user_texts if text.strip()]
    first_text = texts[0].lower() if texts else ""
    has_achievement = any(safe_keyword_match(text.lower(), keyword) for text in texts for keyword in ACHIEVEMENT_SIGNALS)
    has_abandon = any(safe_keyword_match(text.lower(), keyword) for text in texts for keyword in ABANDON_SIGNALS)
    friction_total = sum(friction_counts.values())

    outcome = None
    if git_pushes > 0:
        outcome = "fully_achieved"
    elif has_achievement and edit_write >= 2:
        outcome = "mostly_achieved" if friction_total else "fully_achieved"
    elif has_abandon:
        outcome = "not_achieved"
    elif friction_total >= 3:
        outcome = "partially_achieved"

    helpfulness = None
    if outcome in ("fully_achieved", "mostly_achieved"):
        helpfulness = "helpful"
    elif outcome == "partially_achieved":
        helpfulness = "moderately_helpful"
    elif friction_total >= 3:
        helpfulness = "not_helpful"

    primary_success = None
    if outcome in ("fully_achieved", "mostly_achieved"):
        if edit_write >= 2:
            primary_success = "correct_code_edits"
        elif friction_counts.get("buggy_code"):
            primary_success = "good_debugging"
        elif any(signal in first_text for signal in QUESTION_SIGNALS):
            primary_success = "good_explanations"
        elif tool_counts.get("Read", 0) >= 3:
            primary_success = "fast_accurate_search"
        else:
            primary_success = "proactive_help"

    satisfaction_counts = {}
    if outcome == "fully_achieved":
        satisfaction_counts = {"satisfied": 1}
    elif outcome == "mostly_achieved":
        satisfaction_counts = {"likely_satisfied": 1}
    elif outcome == "partially_achieved":
        satisfaction_counts = {"neutral": 1}
    elif outcome == "not_achieved":
        satisfaction_counts = {"frustrated": 1}

    return {
        "outcome": outcome,
        "claude_helpfulness": helpfulness,
        "primary_success": primary_success,
        "user_satisfaction_counts": satisfaction_counts,
    }


def build_legacy_report_item(session):
    tool_counts = Counter(session.tool_counts or {})
    first_prompt = (session.first_prompt or "").strip()
    all_user_texts = list(session.all_user_texts or [])
    raw_jsonl = session.raw_jsonl or {}
    session_facet = dict(session.facet or {})

    has_compact = bool(raw_jsonl.get("compact_count"))
    has_url = bool(re.search(r'https?://', first_prompt))
    edit_targets = {
        item.get("path")
        for item in raw_jsonl.get("edited_files", [])
        if isinstance(item, dict) and item.get("path")
    }
    write_targets = {
        item.get("path")
        for item in raw_jsonl.get("written_files", [])
        if isinstance(item, dict) and item.get("path")
    }

    all_text_lower = " ".join(all_user_texts).lower()
    topic_hits = infer_topic_hits(all_user_texts)
    debug_signals = sum(all_text_lower.count(keyword) for keyword in DEBUG_KEYWORDS)
    create_signals = sum(all_text_lower.count(keyword) for keyword in CREATE_KEYWORDS)
    learn_signals = sum(all_text_lower.count(keyword) for keyword in LEARN_KEYWORDS)
    organize_signals = sum(all_text_lower.count(keyword) for keyword in ORGANIZE_KEYWORDS)

    user_msgs = session.user_message_count
    assist_msgs = session.assistant_message_count
    bash_count = tool_counts.get("Bash", 0)
    read_count = tool_counts.get("Read", 0)
    edit_count = tool_counts.get("Edit", 0)
    write_count = tool_counts.get("Write", 0)
    total_tools = sum(tool_counts.values())
    edit_write = edit_count + write_count
    is_greeting = any(token in first_prompt for token in GREETING_TOKENS)

    if user_msgs <= 3 and total_tools <= 1 and is_greeting:
        painting_stage = "test_stroke"
    elif has_url and read_count > edit_write * 3 and read_count > 5:
        painting_stage = "copying"
    elif bash_count > 5 and edit_write < 2 and organize_signals >= 1:
        painting_stage = "organizing"
    elif ("README" in all_text_lower or "文档" in all_text_lower or "README" in first_prompt) and ("github" in all_text_lower or "git" in all_text_lower or "开源" in all_text_lower or "license" in all_text_lower or "发布" in all_text_lower):
        painting_stage = "framing"
    elif edit_write >= 3 and (len(edit_targets) + len(write_targets) >= 2 or edit_count >= 5):
        painting_stage = "coloring"
    elif user_msgs > 50 and len(topic_hits) >= 3 and has_compact:
        painting_stage = "sketching"
    elif user_msgs > 20 and len([name for name, count in tool_counts.items() if count > 0]) >= 4 and edit_write < 2:
        painting_stage = "exploring"
    elif edit_write >= 1:
        painting_stage = "coloring"
    elif read_count >= 3:
        painting_stage = "copying"
    elif bash_count >= 3:
        painting_stage = "exploring"
    elif is_greeting:
        painting_stage = "test_stroke"
    else:
        painting_stage = "exploring"

    energy_flow = "neutral"
    if debug_signals >= 3 and user_msgs > 30:
        energy_flow = "consuming"
    elif (create_signals >= 2 or "做一个" in all_text_lower or "写一个" in all_text_lower) and edit_write >= 2:
        energy_flow = "creating"
    elif organize_signals >= 2 and bash_count > 3 and edit_write < 3:
        energy_flow = "organizing"
    elif (learn_signals >= 2 or has_url) and read_count > 3 and edit_write < 3:
        energy_flow = "learning"
    elif edit_write >= 5:
        energy_flow = "creating"
    elif read_count > 5 and edit_write == 0:
        energy_flow = "learning"

    session_type = "single_task"
    if user_msgs > 30:
        session_type = "multi_task"
    elif user_msgs < 5 and assist_msgs < 10:
        session_type = "quick_question"
    elif bash_count > 20 or read_count > 15:
        session_type = "exploration"
    elif edit_write > 0:
        session_type = "iterative_refinement"

    goal = first_prompt[:120] if first_prompt else "未记录"
    git_pushes = session.git_pushes
    inferred_goal_categories = infer_goal_categories(first_prompt, topic_hits, edit_write)
    inferred_friction_counts, inferred_friction_detail = infer_friction_counts(all_user_texts)
    inferred_labels = infer_session_labels(all_user_texts, tool_counts, edit_write, git_pushes, inferred_friction_counts)

    facet = {
        "session_id": session.session_id,
        "session_type": session_facet.get("session_type") or session_type,
        "underlying_goal": session_facet.get("underlying_goal") or goal,
        "brief_summary": session_facet.get("brief_summary") or goal,
        "outcome": session_facet.get("outcome") or inferred_labels.get("outcome"),
        "claude_helpfulness": session_facet.get("claude_helpfulness") or inferred_labels.get("claude_helpfulness"),
        "primary_success": session_facet.get("primary_success") or inferred_labels.get("primary_success"),
        "friction_counts": session_facet.get("friction_counts") or inferred_friction_counts,
        "friction_detail": session_facet.get("friction_detail") or inferred_friction_detail,
        "goal_categories": session_facet.get("goal_categories") or inferred_goal_categories,
        "user_satisfaction_counts": session_facet.get("user_satisfaction_counts") or inferred_labels.get("user_satisfaction_counts", {}),
        "_source": "jsonl+facet" if session_facet else "jsonl",
        "painting_stage": painting_stage,
        "energy_flow": energy_flow,
        "topic_count": len(topic_hits),
        "has_compact": has_compact,
        "has_url": has_url,
        "edit_targets_count": len(edit_targets),
        "write_targets_count": len(write_targets),
    }

    meta = {
        "session_id": session.session_id,
        "project_path": session.project_path,
        "start_time": session.start_time.isoformat() if session.start_time else "",
        "duration_minutes": session.duration_minutes,
        "user_message_count": user_msgs,
        "assistant_message_count": assist_msgs,
        "tool_counts": dict(tool_counts),
        "languages": {},
        "git_commits": 0,
        "git_pushes": git_pushes,
        "input_tokens": session.input_tokens,
        "output_tokens": session.output_tokens,
        "first_prompt": first_prompt[:200],
        "user_interruptions": 0,
        "user_response_times": [],
        "tool_errors": 0,
        "tool_error_categories": {},
        "uses_task_agent": bool(tool_counts.get("Agent") or tool_counts.get("TaskCreate")),
        "uses_mcp": False,
        "version": session.version,
        "git_branch": session.git_branch,
        "all_user_texts": all_user_texts,
        "topic_hits": dict(topic_hits),
    }
    if session.meta:
        meta.update(session.meta)
    meta["session_id"] = session.session_id
    meta["project_path"] = meta.get("project_path") or session.project_path
    meta["start_time"] = meta.get("start_time") or (session.start_time.isoformat() if session.start_time else "")
    meta["duration_minutes"] = coerce_int(meta.get("duration_minutes", session.duration_minutes)) or session.duration_minutes
    meta["user_message_count"] = coerce_int(meta.get("user_message_count", user_msgs)) or user_msgs
    meta["assistant_message_count"] = coerce_int(meta.get("assistant_message_count", assist_msgs)) or assist_msgs
    meta["tool_counts"] = meta.get("tool_counts") or dict(tool_counts)
    meta["git_pushes"] = git_pushes
    meta["first_prompt"] = meta.get("first_prompt") or first_prompt[:200]
    meta["all_user_texts"] = all_user_texts
    meta["topic_hits"] = dict(topic_hits)
    meta["version"] = meta.get("version") or session.version
    meta["git_branch"] = meta.get("git_branch") or session.git_branch

    return {"facet": facet, "meta": meta, "date": session.report_date}