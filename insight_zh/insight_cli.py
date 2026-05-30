#!/usr/bin/env python3
"""
Claude Code 中文洞察报告 — 从 facets + session-meta 生成，绕过 /insight 的英文模板
支持：本地规则分析 + 可选外部 LLM 增强

用法：
  insight-zh                   全部历史数据（默认）
  insight-zh 7                 最近 7 天
  insight-zh 2026-04-01 2026-05-01   指定日期范围
  insight-zh --save            输出到文件
  insight-zh --print-only      只输出 stdout
  insight-zh --llm-advice      使用外部 LLM 生成建议（需要 INSIGHT_API_KEY）
  insight-zh --translate       使用外部 LLM 翻译官方 facets 英文文本（需要 INSIGHT_API_KEY）
"""
import argparse
import hashlib
import html as html_lib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path

from insight_zh.analysis.session_inference import build_legacy_report_item, get_git_push_count as get_git_commit_count
from insight_zh.sources.facets_source import load_facet
from insight_zh.sources.jsonl_source import iter_project_jsonl_paths, parse_jsonl_session
from insight_zh.sources.session_loader import merge_session_sources
from insight_zh.sources.session_meta_source import load_session_meta
from insight_zh.sources.zh_cache import load_cached_report_item, reports_dir, write_report_item_cache

CLAUDE_DIR = Path.home() / ".claude"
FACETS_DIR = CLAUDE_DIR / "usage-data/facets"
META_DIR = CLAUDE_DIR / "usage-data/session-meta"
REPORTS_DIR = reports_dir(CLAUDE_DIR)
REPORTS_DIR.mkdir(exist_ok=True)

# ── 可选外部 LLM 配置 ──
# 默认报告不需要 API key，也不会主动调用外部模型。
# 只有显式传入 --llm-advice 或 --translate 时才会读取这些环境变量。
# 支持任意兼容 Anthropic SDK 的 API（Kimi、DeepSeek、OpenAI 等）。
# 可选：export INSIGHT_API_BASE="https://api.kimi.com/coding/"
# 可选：export INSIGHT_API_MODEL="kimi-for-coding"
API_BASE_URL = os.environ.get("INSIGHT_API_BASE", "https://api.kimi.com/coding/")
API_KEY = os.environ.get("INSIGHT_API_KEY", "")
API_MODEL = os.environ.get("INSIGHT_API_MODEL", "kimi-for-coding")

# ── 中文映射表 ──
OUTCOME_MAP = {
    "fully_achieved": "完全达成",
    "mostly_achieved": "大部分达成",
    "partially_achieved": "部分达成",
    "not_achieved": "未达成",
    "unclear_from_transcript": "无法从记录判断",
}
SESSION_TYPE_MAP = {
    "multi_task": "多任务",
    "quick_question": "快速问答",
    "iterative_refinement": "迭代优化",
    "single_task": "单任务",
    "exploration": "探索",
    "debugging": "调试",
}
HELPFULNESS_MAP = {
    "essential": "关键帮助",
    "very_helpful": "非常有帮助",
    "helpful": "有帮助",
    "moderately_helpful": "有些帮助",
    "somewhat_helpful": "有些帮助",
    "slightly_helpful": "帮助甚微",
    "not_helpful": "没帮助",
    "unhelpful": "没帮助",
}
SUCCESS_MAP = {
    "good_explanations": "解释清晰",
    "proactive_help": "主动帮助",
    "good_debugging": "调试能力强",
    "fast_accurate_search": "搜索快速准确",
    "multi_file_changes": "多文件修改",
    "correct_code_edits": "代码编辑正确",
    "user_persistence": "用户坚持",
    "none": "无明显成功因素",
}
FRICTION_MAP = {
    "misunderstood_request": "误读请求",
    "wrong_approach": "方向错误",
    "buggy_code": "代码有 bug",
    "excessive_changes": "过度修改",
    "user_rejected_action": "用户拒绝操作",
    "ui_state_cache_issue": "UI 状态缓存问题",
    "api_errors": "API 错误",
    "unable_to_resolve": "无法解决",
    "external_blocker": "外部阻塞",
    "environment_issue": "环境问题",
    "context_overflow": "上下文溢出",
    "api_key_issues": "API 密钥问题",
    "recurring_bug": "重复出现的 bug",
    "slow_progress": "进展缓慢",
    "external_limitation": "外部限制",
    "tool_failure": "工具失败",
    "model_unavailable_error": "模型不可用错误",
    "no_response_from_assistant": "无响应",
    "stuck_loop": "卡死循环",
    "unverified_claims": "未验证的断言",
    "model_unavailable": "模型不可用",
    "api_connection_failure": "API 连接失败",
    "api_policy_error": "API 策略错误",
    "incomplete_solution": "方案不完整",
    "questionable_evaluation": "评估存疑",
}


def classify_goal(cat):
    """把细粒度 goal category 合并成大类。"""
    c = cat.lower()
    if "warmup" in c:
        return "测试与热身"
    if "skill" in c:
        return "Skill 系统管理"
    if any(x in c for x in ["memory", "save_to_memory", "save_to_knowledge", "save_context", "checkpoint", "recall_past", "knowledge_organization", "knowledge_persistence"]):
        return "记忆与持久化"
    if any(x in c for x in ["debug", "troubleshoot", "fix_", "bug_fix", "bug_fixing", "diagnose", "debugging", "debug_errors", "debug_server", "debug_mcp", "debug_proxy", "debug_map", "debug_environment", "debug_deployed"]):
        return "调试与排障"
    if any(x in c for x in ["search_", "search_discovery", "search_functionality"]):
        return "搜索与发现"
    if any(x in c for x in ["explain", "concept", "information", "question_answer", "clarification", "how_to", "ask_question", "information_query", "information_request", "question_answering", "conceptual_explanation"]):
        return "概念解释与信息查询"
    if any(x in c for x in ["content", "article", "write_", "creative", "illustration", "generate_ppt", "improve_ppt", "article_creation", "article_polishing", "article_analysis", "write_article", "write_documentation", "data_mining_for_content", "creative_design"]):
        return "内容创作"
    if any(x in c for x in ["code_", "code_integration", "code_review", "code_investigation", "implementation", "build_", "create_frontend", "integrate", "feature_implementation", "feature_design", "roadmap_implementation", "create_or_edit_files"]):
        return "代码与实现"
    if any(x in c for x in ["configuration", "config_", "setup", "install", "shell_configuration", "configure_permissions", "configure_model", "configuration_setup", "configuration_task", "config_file_edits", "cleanup_config", "fix_config_path", "update_tool", "rule_configuration"]):
        return "配置与安装"
    if any(x in c for x in ["project", "repo_", "project_status", "project_evaluation", "project_assessment", "project_update", "compare_projects", "strategic_review"]):
        return "项目管理"
    if any(x in c for x in ["git_", "git_commit", "git_management", "repo_sync_check", "secure_commit", "ship_to_github", "github_upload"]):
        return "Git 操作"
    if any(x in c for x in ["market", "career", "business", "job", "interview", "research", "career_direction", "career_analysis", "resume_review", "job_analysis", "interview_preparation", "market_research", "market_comparison", "business_strategy"]):
        return "研究与策略"
    if any(x in c for x in ["compare", "analysis", "evaluate", "assessment", "scan", "data_analysis", "comparison_analysis", "analyze"]):
        return "分析与评估"
    if any(x in c for x in ["delete_", "cleanup_", "remove_", "cleanup_old", "cleanup_redundant", "cleanup_skills", "system_process_cleanup", "directory_cleanup", "security_cleanup"]):
        return "清理与维护"
    if any(x in c for x in ["demo", "create_frontend_demo"]):
        return "Demo 与演示"
    if any(x in c for x in ["design_discussion", "design_", "ux_redesign", "compare_themes", "reference_external_style", "run_styling_workflow", "run_full_illustration_pipeline"]):
        return "设计与样式"
    if any(x in c for x in ["folder_setup", "create_finder_shortcut", "create_startup_script", "environment_setting"]):
        return "环境设置"
    if any(x in c for x in ["import_", "export_", "publish_", "publish_via_picgo", "locate_exported_file"]):
        return "导入导出发布"
    if any(x in c for x in ["personal_profile", "profile_management", "profile_analysis", "reader_persona_analysis"]):
        return "个人档案"
    if any(x in c for x in ["learning_guidance", "learning_best_practices", "learning_tool_usage"]):
        return "学习与指导"
    if any(x in c for x in ["security_", "api_key_issues", "local_search_for_credentials"]):
        return "安全与凭证"
    if any(x in c for x in ["workflow_", "workflow_documentation", "workflow_optimization", "process_improvement", "process_retrospective"]):
        return "流程与优化"
    if any(x in c for x in ["system_", "system_check", "system_management", "system_investigation", "system_iteration", "system_optimization", "system_architecture", "iterate_system_principles"]):
        return "系统管理"
    if any(x in c for x in ["tool_", "tool_usage", "tool_setup", "tool_installation", "tool_exploration", "tool_compatibility", "tool_meta_question", "run_tool_functions", "automation_tooling"]):
        return "工具探索"
    if any(x in c for x in ["prompt_analysis", "idea_generation", "meta_reflection", "discuss_ai_collaboration", "philosophy_discussion"]):
        return "思考与反思"
    if any(x in c for x in ["weekly_review", "view_report", "output_continuation", "output_testing", "formatting", "text_editing", "command_review", "list_pages", "locate_file_path", "locate_installed_skill", "find_url", "find_network_speed_tool", "data_listing_request", "information_retrieval", "information_gathering", "information_research", "content_extraction", "content_reading", "content_access"]):
        return "杂项操作"
    return "其他"


def parse_args():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("arg1", nargs="?", default=None)
    p.add_argument("arg2", nargs="?", default=None)
    p.add_argument("--all", action="store_true")
    p.add_argument("--save", action="store_true")
    p.add_argument("--print-only", action="store_true")
    p.add_argument("--translate", action="store_true", help="使用外部 LLM 翻译官方 facets 英文文本（需要 INSIGHT_API_KEY）")
    p.add_argument("--no-translate", action="store_true", help="兼容旧参数：保持不翻译")
    p.add_argument("--llm-advice", action="store_true", help="使用外部 LLM 生成深度建议（需要 INSIGHT_API_KEY）")
    p.add_argument("--html", action="store_true", help="生成 HTML 可视化报告")
    p.add_argument("--regen-advice", action="store_true", help="强制重新生成深度建议（不用今天的缓存）")
    p.add_argument("-h", "--help", action="store_true")
    return p.parse_args()


def resolve_range(args):
    if args.arg1 is None:
        return None, date.today()
    if args.all:
        return None, date.today()
    if re.fullmatch(r"\d+", args.arg1):
        n = int(args.arg1)
        return date.today() - timedelta(days=n - 1), date.today()
    m1 = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", args.arg1)
    m2 = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", args.arg2 or "")
    if m1 and m2:
        return date(int(m1[1]), int(m1[2]), int(m1[3])), date(int(m2[1]), int(m2[2]), int(m2[3]))
    if m1 and not m2:
        return date(int(m1[1]), int(m1[2]), int(m1[3])), date.today()
    print(f"无法识别的参数：{args.arg1} {args.arg2}", file=sys.stderr)
    sys.exit(2)


def report_basename(start_d, end_d):
    if start_d and start_d != end_d:
        return f"{start_d}_to_{end_d}"
    return str(end_d or date.today())


def load_data(start_d, end_d):
    items = []
    if not FACETS_DIR.exists():
        return items
    for fp in FACETS_DIR.glob("*.json"):
        try:
            facet = json.loads(fp.read_text(encoding="utf-8"))
            sid = facet.get("session_id") or fp.stem
            meta = {}
            mp = META_DIR / f"{sid}.json"
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                except Exception:
                    pass
            start = meta.get("start_time")
            if start:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone().date()
                if start_d and dt < start_d:
                    continue
                if dt > end_d:
                    continue
            elif start_d:
                continue
            items.append({"facet": facet, "meta": meta, "date": dt if start else None})
        except Exception:
            continue
    items.sort(key=lambda x: x["date"] or date.min, reverse=True)
    return items


def _load_json_if_exists(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── JSONL 数据源（Claude Code 2.1.140+ 新格式）──

def _build_item_from_normalized_session(session):
    """把共享 loader 的 NormalizedSession 适配回旧报告层需要的结构。"""
    return build_legacy_report_item(session)


def load_data_from_jsonl(start_d, end_d):
    """读取 JSONL 原始会话，并维护 insight-zh 自己的分层缓存。

    缓存目录：
      ~/.claude/usage-data-zh/session-meta/*.json
      ~/.claude/usage-data-zh/facets/*.json
      ~/.claude/usage-data-zh/index.json

    原始 JSONL 的 mtime/size 未变化且 analyzer_version 一致时，直接复用缓存；
    否则重新解析 JSONL，合并官方 /insights 的 usage-data，再写回中文缓存。
    """
    items = []
    for jsonl_path in iter_project_jsonl_paths(CLAUDE_DIR):
        cached_item = load_cached_report_item(jsonl_path, CLAUDE_DIR, start_d, end_d)
        if cached_item:
            item_date = cached_item.get("date")
            if start_d and item_date and item_date < start_d:
                continue
            if item_date and item_date > end_d:
                continue
            items.append(cached_item)
            continue

        parsed = parse_jsonl_session(jsonl_path, start_date=start_d, end_date=end_d)
        first_ts = parsed.get("first_ts")
        last_ts = parsed.get("last_ts") or first_ts
        if first_ts is None:
            continue

        session_start_date = first_ts.date()
        session_end_date = last_ts.date() if last_ts else session_start_date
        if start_d and session_end_date < start_d:
            continue
        if session_start_date > end_d:
            continue
        if not parsed.get("user_msgs"):
            continue

        session_id = jsonl_path.stem
        session = merge_session_sources(
            session_id=session_id,
            jsonl_path=jsonl_path,
            parsed=parsed,
            facet=load_facet(session_id, CLAUDE_DIR),
            meta=load_session_meta(session_id, CLAUDE_DIR),
        )
        if session is None:
            continue
        item = _build_item_from_normalized_session(session)
        write_report_item_cache(item, jsonl_path, CLAUDE_DIR, start_d, end_d)
        items.append(item)

    items.sort(key=lambda x: x["date"] or date.min, reverse=True)
    return items


def fmt_bar(v, max_v, width=30):
    if max_v <= 0:
        return ""
    filled = int(v / max_v * width)
    return "█" * filled + "░" * (width - filled)


# ── LLM 翻译层 ──

TRANSLATION_CACHE_FILE = REPORTS_DIR / ".translation-cache.json"


def load_translation_cache():
    """加载翻译缓存。"""
    if not TRANSLATION_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(TRANSLATION_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_translation_cache(cache):
    """保存翻译缓存。"""
    try:
        TRANSLATION_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"(缓存保存失败: {e})", file=sys.stderr)


def translate_batch(texts, max_batch=30):
    """用 Kimi API 批量翻译英文文本。返回 {原文: 译文} 字典。支持缓存。"""
    if not texts:
        return {}
    if not API_KEY:
        print("(未设置 INSIGHT_API_KEY，跳过外部 LLM 翻译)", file=sys.stderr)
        return {t: t for t in texts}

    # 加载缓存
    cache = load_translation_cache()
    cached_count = sum(1 for t in texts if t in cache)
    if cached_count > 0:
        print(f"  缓存命中：{cached_count}/{len(texts)} 条已翻译过", file=sys.stderr)

    # 只翻译未缓存的
    to_translate = [t for t in texts if t not in cache]
    if not to_translate:
        return {t: cache[t] for t in texts}

    print(f"  需新翻译：{len(to_translate)} 条", file=sys.stderr)

    try:
        from anthropic import Anthropic
    except ImportError:
        print("(anthropic SDK 未安装，跳过翻译)", file=sys.stderr)
        return {t: t for t in texts}

    client = Anthropic(api_key=API_KEY, base_url=API_BASE_URL)
    results = {t: cache[t] for t in texts if t in cache}

    total_batches = (len(to_translate) + max_batch - 1) // max_batch
    for batch_idx, i in enumerate(range(0, len(to_translate), max_batch), 1):
        batch = to_translate[i:i + max_batch]
        numbered = "\n".join(f"{idx + 1}. {txt}" for idx, txt in enumerate(batch))

        prompt = f"""请将以下英文文本逐条翻译成中文。

要求：
1. 保留所有专有名词不翻译：Claude Code、MCP、API、Bash、Nowledge Mem、IMA、JSON、HTML、CSS、GitHub、Cloudflare、Kimi、WeChat、VS Code、Obsidian 等
2. 保留所有 URL、文件路径、代码片段、命令行、环境变量名不翻译
3. 保留所有品牌名、产品名、技术术语不翻译
4. 自然语言部分翻译成流畅的中文，不要直译
5. 不要添加原文没有的注释或解释
6. 如果原文已经是中文或混合语言，保持原样

文本列表：
{numbered}

请严格按以下格式返回（每行对应输入的一条）：
1. [中文译文]
2. [中文译文]
..."""

        print(f"  批次 {batch_idx}/{total_batches}（{len(batch)} 条）...", file=sys.stderr, flush=True)
        try:
            resp = client.messages.create(
                model=API_MODEL,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = resp.content[0].text
            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^(\d+)\.\s*(.+)$", line)
                if m:
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < len(batch):
                        results[batch[idx]] = m.group(2).strip()
                        cache[batch[idx]] = m.group(2).strip()
            for txt in batch:
                if txt not in results:
                    results[txt] = txt
            save_translation_cache(cache)
        except Exception as e:
            err_msg = str(e)
            # 如果是 high_risk 错误且批次较大，拆成小批重试
            if ("high risk" in err_msg or "high_risk" in err_msg) and len(batch) > 3:
                print(f"  (批次 {batch_idx} 被拒，拆成小批 5 条重试)", file=sys.stderr)
                sub_size = 5
                for sub_start in range(0, len(batch), sub_size):
                    sub_batch = batch[sub_start:sub_start + sub_size]
                    sub_numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(sub_batch))
                    sub_prompt = f"请将以下英文文本逐条翻译成中文。保留专有名词。\n\n{sub_numbered}\n\n请按 '1. 译文' 格式返回。"
                    try:
                        sub_resp = client.messages.create(
                            model=API_MODEL,
                            max_tokens=1500,
                            messages=[{"role": "user", "content": sub_prompt}]
                        )
                        sub_raw = sub_resp.content[0].text
                        for line in sub_raw.split("\n"):
                            line = line.strip()
                            m = re.match(r"^(\d+)\.\s*(.+)$", line)
                            if m:
                                idx = int(m.group(1)) - 1
                                if 0 <= idx < len(sub_batch):
                                    results[sub_batch[idx]] = m.group(2).strip()
                                    cache[sub_batch[idx]] = m.group(2).strip()
                    except Exception as sub_e:
                        # 这小批也失败，跳过
                        for txt in sub_batch:
                            if txt not in results:
                                results[txt] = txt
                save_translation_cache(cache)
            else:
                print(f"(批次 {batch_idx} 失败: {e})，该批次使用原文", file=sys.stderr)
                for txt in batch:
                    if txt not in results:
                        results[txt] = txt

    return results


def collect_texts_to_translate(items):
    """收集所有需要翻译的 unique 文本。"""
    texts = set()
    for it in items:
        f = it["facet"]
        for field in ["underlying_goal", "brief_summary", "friction_detail"]:
            val = f.get(field)
            if val and isinstance(val, str) and len(val) > 3:
                # 跳过纯中文或明显中文主导的
                ascii_ratio = sum(1 for c in val if ord(c) < 128) / len(val)
                if ascii_ratio > 0.6:  # 主要是英文才翻译
                    texts.add(val.strip())
    return list(texts)


# ── 定性分析 ──

# ── 绘画方法论分析 ──

STAGE_LABELS = {
    "test_stroke": "试笔",
    "copying": "临摹",
    "sketching": "素描",
    "exploring": "探索",
    "coloring": "上色",
    "framing": "装裱",
    "organizing": "整理",
    "unknown": "未分类",
}

STAGE_DESCRIPTIONS = {
    "test_stroke": "验证工具是否正常，不产出作品",
    "copying": "学习他人作品，Read 多产出少",
    "sketching": "一张画布尝试多种构图，主题发散",
    "exploring": "自由探索，工具杂但无明确产出",
    "coloring": "专注实现，大量 Edit/Write",
    "framing": "文档、README、GitHub 发布准备",
    "organizing": "整理画室，系统审计与清理",
    "unknown": "无法归类",
}

ENERGY_LABELS = {
    "consuming": "消耗型",
    "creating": "创造型",
    "learning": "学习型",
    "organizing": "整理型",
    "neutral": "中性",
}

ENERGY_DESCRIPTIONS = {
    "consuming": "陷入调试地狱，连环 bug 反复修",
    "creating": "从需求到产出的完整链路",
    "learning": "知识探索、对比、文档阅读",
    "organizing": "系统审计、配置、清理",
    "neutral": "无明显能量特征",
}


def generate_painting_analysis(items, total, total_dur, total_user_msgs, total_commits, hours, goals):
    """基于《黑客与画家》方法论的画室观察笔记。

    Paul Graham 的核心观点：
    1. 黑客和画家都是创作者——作品是"改"出来的，不是一次画对的
    2. 快速原型——先画一个粗糙版本，再不断迭代
    3. 大段连续时间——创作需要不被打断的沉浸
    4. 草图和成品一样珍贵——保存所有中间状态
    5. 完成比完美重要——必须设定"提交点"
    6. 自下而上——在过程中发现，不是严格按计划

    报告像画室导师的观察笔记，不是统计数据报表。
    """
    lines = []
    n = len(items)
    bash = total.get("Bash", 0)
    read = total.get("Read", 0)
    edit = total.get("Edit", 0)
    write = total.get("Write", 0)
    edit_write = edit + write

    # 根据数据范围决定时间词
    dates = [it["date"] for it in items if it["date"]]
    if dates:
        day_span = (max(dates) - min(dates)).days + 1
    else:
        day_span = 7
    if day_span <= 1:
        period = "天"
    elif day_span <= 7:
        period = "周"
    elif day_span <= 14:
        period = "两周"
    elif day_span <= 31:
        period = "月"
    else:
        period = "段时期"

    # ── 收集基础数据 ──
    compact_count = 0
    topic_counts = []
    long_sessions = 0
    short_sessions = 0
    evening_sessions = 0
    midnight_sessions = 0
    morning_sessions = 0

    for it in items:
        f = it["facet"]
        m = it["meta"]
        if f.get("has_compact"):
            compact_count += 1
        topic_counts.append(f.get("topic_count", 0))
        umsgs = m.get("user_message_count", 0)
        if umsgs > 100:
            long_sessions += 1
        elif umsgs < 10:
            short_sessions += 1
        # 时段
        h = 12
        st = m.get("start_time", "")
        if st:
            try:
                h = datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone().hour
            except Exception:
                pass
        if 18 <= h <= 23:
            evening_sessions += 1
        elif 0 <= h <= 5:
            midnight_sessions += 1
        elif 6 <= h <= 11:
            morning_sessions += 1

    avg_topics = sum(topic_counts) / max(len(topic_counts), 1)
    avg_msg_per_session = total_user_msgs / max(n, 1)
    short_ratio = short_sessions / max(n, 1)
    late_sessions = evening_sessions + midnight_sessions
    late_ratio = late_sessions / max(n, 1)
    bash_read_ratio = bash / max(read, 1)
    commit_minutes = total_dur / max(total_commits, 1) if total_commits else None

    if day_span <= 1:
        action_label = "明天可以试的"
        max_observations = 3
    elif day_span <= 7:
        action_label = "下周可以试的"
        max_observations = 4
    elif day_span <= 31:
        action_label = "下个月可以试的"
        max_observations = 4
    else:
        action_label = "下一阶段可以试的"
        max_observations = 5

    lines.append("## 画室观察笔记")
    lines.append("")
    lines.append(f"这{period}你的画室里有 {n} 张画布，总共落了 {total_user_msgs} 笔。")
    lines.append("")

    def note(title, paragraphs, severity):
        return {
            "title": title,
            "paragraphs": paragraphs,
            "severity": severity,
        }

    candidates = []

    if short_ratio >= 0.45 or long_sessions == 0:
        if day_span <= 1:
            title = "今天的问题不是会话少，而是很多画布只开了头"
            action = "选今天最重要的一张短会话继续做，完成标准只设一个：产生一个能运行或能复查的产物。"
        else:
            title = "这段时间速写太多，真正深入的画布太少"
            action = "从短会话里挑一个反复出现的主题，单独开会话收束成一个文件、一个提交或一个决策记录。"
        candidates.append(note(title, [
            f"{short_sessions} 张画布少于 10 条消息，占 {short_ratio*100:.0f}%。",
            f"超过 100 条消息的深画布有 {long_sessions} 张；平均每张画布 {avg_msg_per_session:.1f} 笔。",
            "速写不是问题，问题是速写没有进入第二遍。创作需要从草稿里挑一个继续加深，而不是不断开新纸。",
            f"**{action_label}**：{action}",
        ], short_ratio * 100 + (20 if long_sessions == 0 else 0)))

    if bash_read_ratio > 2 or (bash > 30 and edit_write < bash * 0.5):
        if day_span <= 1:
            title = "今天调色盘动作偏多，落到画布上的比例不够"
        else:
            title = "这段时间的主要惯性是探索过量"
        candidates.append(note(title, [
            f"Bash {bash} 次，Read {read} 次，Edit/Write {edit_write} 次；Bash/Read 比是 {bash_read_ratio:.1f}:1。",
            "这类数据通常说明 Claude 在环境和命令层来回试，而不是快速把判断写回代码或文档。",
            "Paul Graham 说编程和画画一样是设计工作；设计不能只在调色盘上发生，必须回到画布。",
            f"**{action_label}**：每次连续 5 个 Bash 后强制停一下，要求 Claude 说清楚下一笔要改哪个文件、为什么改。",
        ], min(100, bash_read_ratio * 18)))

    if total_commits == 0 or (commit_minutes and commit_minutes > 120):
        if total_commits == 0:
            first_line = f"{edit_write} 次 Edit/Write，但没有任何 commit。"
            severity = 95
        else:
            first_line = f"{total_commits} 个 commit，约 {int(commit_minutes)} 分钟/commit。"
            severity = min(95, commit_minutes / 2)
        candidates.append(note("草图保存频率偏低", [
            first_line,
            "这里统计的是底层 Git 仓库真实 commit，不区分 Claude Code、Codex、其他 AI 还是你手动提交。",
            "如果没有版本点，报告只能看到最终状态，看不到中间判断，也就很难复盘哪一步真正有效。",
            f"**{action_label}**：把 commit 当作工作节拍器；超过 60-90 分钟的会话，结束前至少留一个 wip commit。",
        ], severity))

    if late_ratio > 0.55 or morning_sessions == 0:
        if morning_sessions == 0:
            first_line = f"上午 0 次，晚上和凌晨 {late_sessions} 次。"
        else:
            first_line = f"上午 {morning_sessions} 次，晚上和凌晨 {late_sessions} 次，晚间占 {late_ratio*100:.0f}%。"
        candidates.append(note("工作时段偏向晚间，复杂判断容易堆到低能量时段", [
            first_line,
            "深夜适合连续执行，不适合做方向判断。方向判断错了，后面 Bash、Read、Edit 都会变成补救成本。",
            f"**{action_label}**：把最难的架构判断、范围确认、是否继续做，放到上午或下午第一段清醒时间。",
        ], late_ratio * 80))

    if compact_count > 0 or long_sessions > 0:
        details = []
        if compact_count > 0:
            details.append(f"{compact_count} 次 /compact")
        if long_sessions > 0:
            details.append(f"{long_sessions} 个 100+ 消息会话")
        candidates.append(note("长画布有被覆盖的风险", [
            "、".join(details) + "。",
            "长会话不是天然更深。超过一定长度后，早期约束、失败尝试和临时决策会被后面的上下文挤掉。",
            "这也是为什么日报和周报要看会话摘要：摘要如果只剩最后阶段，说明过程材料已经丢了一部分。",
            f"**{action_label}**：会话超过 50 条消息时，先生成一段决策记录；如果目标已经变了，开新会话。",
        ], compact_count * 8 + long_sessions * 12))

    if not candidates:
        candidates.append(note("这段数据没有明显红灯", [
            f"{n} 个会话，{total_user_msgs} 条消息，{total_dur//60} 小时，{total_commits} 个 commit。",
            "从画室视角看，当前更适合继续积累趋势，而不是对单日波动下重判断。",
            f"**{action_label}**：保留同一口径连续跑几天，优先看 Bash/Read、commit 率、短会话比例是否连续恶化。",
        ], 0))

    candidates.sort(key=lambda item: item["severity"], reverse=True)
    for idx, item in enumerate(candidates[:max_observations], 1):
        lines.append(f"### 观察{idx}：{item['title']}")
        lines.append("")
        for paragraph in item["paragraphs"]:
            lines.append(paragraph)
            lines.append("")

    return lines

def generate_report(items, translations=None):
    if not items:
        return "# Claude Code 中文洞察报告\n\n所选范围内无数据。\n"

    if translations is None:
        translations = {}

    n = len(items)
    first_date = min(it["date"] for it in items if it["date"])
    last_date = max(it["date"] for it in items if it["date"])

    # 聚合统计
    total_user_msgs = 0
    total_assist_msgs = 0
    total_dur = 0
    total_commits = 0
    total_commit_hashes = set()
    total_input_tokens = 0
    total_output_tokens = 0
    tool_counter = Counter()
    lang_counter = Counter()
    session_types = Counter()
    outcomes = Counter()
    satisfactions = Counter()
    helpfulness = Counter()
    successes = Counter()
    frictions = Counter()
    friction_details = []
    goals = Counter()
    brief_summaries = []
    interruptions = 0

    for it in items:
        m = it["meta"]
        f = it["facet"]
        total_user_msgs += m.get("user_message_count", 0)
        total_assist_msgs += m.get("assistant_message_count", 0)
        total_dur += m.get("duration_minutes", 0)
        commit_hashes = m.get("git_commit_hashes") or []
        if commit_hashes:
            total_commit_hashes.update(str(h) for h in commit_hashes)
            total_commits = len(total_commit_hashes)
        else:
            total_commits += get_git_commit_count(m)
        total_input_tokens += m.get("input_tokens", 0)
        total_output_tokens += m.get("output_tokens", 0)
        for k, v in m.get("tool_counts", {}).items():
            tool_counter[k] += v
        for k, v in m.get("languages", {}).items():
            lang_counter[k] += v
        session_types[f.get("session_type", "unknown")] += 1
        outcome = f.get("outcome")
        if outcome and outcome != "unknown":
            outcomes[outcome] += 1
        for k, v in f.get("user_satisfaction_counts", {}).items():
            satisfactions[k] += v
        helpful = f.get("claude_helpfulness")
        if helpful and helpful != "unknown":
            helpfulness[helpful] += 1
        primary_success = f.get("primary_success")
        if primary_success:
            successes[primary_success] += 1
        for k, v in f.get("friction_counts", {}).items():
            frictions[k] += 1
        fr = f.get("friction_detail", "")
        if fr:
            friction_details.append((it["date"], translations.get(fr, fr)))
        for k, v in f.get("goal_categories", {}).items():
            goals[classify_goal(k)] += v
        bs = f.get("brief_summary", "")
        if bs:
            brief_summaries.append((it["date"], translations.get(bs, bs)))
        interruptions += m.get("user_interruptions", 0)

    hours = Counter()
    for it in items:
        st = it["meta"].get("start_time", "")
        if st:
            try:
                h = datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone().hour
                hours[h] += 1
            except Exception:
                pass

    night = sum(hours.get(h, 0) for h in range(18, 24))
    midnight = sum(hours.get(h, 0) for h in range(0, 6))
    morning = sum(hours.get(h, 0) for h in range(6, 12))
    afternoon = sum(hours.get(h, 0) for h in range(12, 18))

    total = tool_counter

    lines = []
    lines.append(f"# Claude Code 中文洞察报告")
    lines.append(f"")
    lines.append(f"**{n} 个会话 · {first_date} 至 {last_date} · {total_user_msgs} 条用户消息 · {total_dur} 分钟 · {total_commits}  个 commit**")
    lines.append(f"")
    has_jsonl = any(str(it.get("facet", {}).get("_source", "")).startswith("jsonl") for it in items)
    if has_jsonl:
        lines.append(f"> 数据范围说明：本报告基于 `~/.claude/projects/*.jsonl` 原始会话数据，共 {n} 个会话；若存在对应的 facets / session-meta，会优先补充这些已分析字段。无补充数据时，部分字段（如目标分类、摩擦点）由启发式规则推断。不含 Claude App（桌面端/网页端）会话。")
    else:
        lines.append(f"> 数据范围说明：本报告基于 `/insight` 已分析的 {n} 个会话（facets 数据）。Claude App 显示你有更多跨平台会话，此处仅包含 Claude Code CLI。")
    lines.append(f"")

    # ── 概览 ──
    lines.append("## 概览")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|---|---|")
    lines.append(f"| 会话数 | {n} |")
    lines.append(f"| 用户消息 | {total_user_msgs} |")
    lines.append(f"| Claude 回复 | {total_assist_msgs} |")
    lines.append(f"| 总时长 | {total_dur} 分钟（约 {total_dur // 60} 小时） |")
    lines.append(f"| Git commit | {total_commits} |")
    lines.append(f"| Input tokens | {total_input_tokens:,} |")
    lines.append(f"| Output tokens | {total_output_tokens:,} |")
    lines.append(f"| 平均每个会话 | {total_dur // max(n, 1)} 分钟 · {total_user_msgs // max(n, 1)} 消息 |")
    lines.append("")

    # 空白期检测
    all_dates = sorted(set(it["date"] for it in items if it["date"]))
    if len(all_dates) >= 2:
        gaps = []
        for i in range(1, len(all_dates)):
            gap = (all_dates[i] - all_dates[i - 1]).days - 1
            if gap >= 3:
                gaps.append((all_dates[i - 1], all_dates[i], gap))
        if gaps:
            lines.append("⚠️ **检测到数据空白期：**")
            lines.append("")
            for start_gap, end_gap, gap_days in sorted(gaps, key=lambda x: x[2], reverse=True)[:3]:
                lines.append(f"- {start_gap} → {end_gap}（空白 {gap_days} 天）")
            lines.append("")

    # ── 绘画方法论分析 ──
    lines.extend(generate_painting_analysis(items, total, total_dur, total_user_msgs, total_commits, hours, goals))

    # ── 你在做什么 ──
    lines.append("## 你在做什么")
    lines.append("")
    lines.append("**工作方向（大类）：**")
    lines.append("")
    max_g = max(goals.values()) if goals else 1
    for k, v in goals.most_common(8):
        bar = fmt_bar(v, max_g)
        lines.append(f"- {bar} {k}：{v}")
    lines.append("")

    # ── 你的使用方式 ──
    lines.append("## 你的使用方式")
    lines.append("")

    lines.append("**会话类型：**")
    lines.append("")
    for k, v in session_types.most_common():
        label = SESSION_TYPE_MAP.get(k, k)
        lines.append(f"- {label}：{v} 次")
    lines.append("")

    lines.append("**结果达成度：**")
    lines.append("")
    for k, v in outcomes.most_common():
        label = OUTCOME_MAP.get(k, k)
        lines.append(f"- {label}：{v} 次")
    lines.append("")

    lines.append("**Claude 帮助度：**")
    lines.append("")
    for k, v in helpfulness.most_common():
        label = HELPFULNESS_MAP.get(k, k)
        lines.append(f"- {label}：{v} 次")
    lines.append("")

    if satisfactions:
        lines.append("**满意度分布：**")
        lines.append("")
        SATISFACTION_MAP = {
            "happy": "开心", "satisfied": "满意", "likely_satisfied": "应该满意",
            "dissatisfied": "不满意", "frustrated": "受挫",
            "neutral": "中立", "confused": "困惑", "mildly_frustrated": "轻微受挫",
            "questioning": "存疑", "unclear": "不清楚",
        }
        for k, v in satisfactions.most_common():
            label = SATISFACTION_MAP.get(k, k)
            lines.append(f"- {label}：{v} 次")
        lines.append("")

    if tool_counter:
        lines.append("**工具使用 TOP 10：**")
        lines.append("")
        max_t = max(tool_counter.values())
        for k, v in tool_counter.most_common(10):
            bar = fmt_bar(v, max_t, width=20)
            lines.append(f"- {bar} {k}：{v}")
        lines.append("")

    if lang_counter:
        lines.append("**编程语言：**")
        lines.append("")
        for k, v in lang_counter.most_common():
            lines.append(f"- {k}：{v} 次")
        lines.append("")

    if hours:
        lines.append("**会话时段分布：**")
        lines.append("")
        segments = {
            "凌晨 (0-6)": sum(hours.get(h, 0) for h in range(0, 6)),
            "上午 (6-12)": sum(hours.get(h, 0) for h in range(6, 12)),
            "下午 (12-18)": sum(hours.get(h, 0) for h in range(12, 18)),
            "晚上 (18-24)": sum(hours.get(h, 0) for h in range(18, 24)),
        }
        max_s = max(segments.values()) if segments else 1
        for seg, v in segments.items():
            bar = fmt_bar(v, max_s, width=20)
            lines.append(f"- {bar} {seg}：{v} 次")
        lines.append("")

    # ── 做得好的地方 ──
    lines.append("## 做得好的地方")
    lines.append("")
    if successes:
        for k, v in successes.most_common(6):
            if k == "none" and v <= 1:
                continue
            label = SUCCESS_MAP.get(k, k)
            lines.append(f"- **{label}**（{v} 次会话）")
    else:
        lines.append("(数据不足)")
    lines.append("")

    high_sat = []
    for it in items:
        f = it["facet"]
        counts = f.get("user_satisfaction_counts", {})
        total_s = sum(counts.values())
        good = counts.get("satisfied", 0) + counts.get("happy", 0) + counts.get("likely_satisfied", 0)
        if total_s > 0 and good / total_s >= 0.8:
            goal = f.get("underlying_goal", "")
            high_sat.append((it["date"], translations.get(goal, goal)))
    if high_sat:
        lines.append(f"**高满意度会话（≥80% 满意）共 {len(high_sat)} 个，摘录：**")
        lines.append("")
        for dt, goal in high_sat[:5]:
            d_str = dt.strftime("%m-%d") if dt else ""
            lines.append(f"- [{d_str}] {goal}")
        lines.append("")

    # ── 哪里出了问题 ──
    lines.append("## 哪里出了问题")
    lines.append("")
    if frictions:
        max_f = max(frictions.values())
        for k, v in frictions.most_common(10):
            label = FRICTION_MAP.get(k, k)
            bar = fmt_bar(v, max_f, width=20)
            lines.append(f"- {bar} {label}：{v} 次")
        lines.append("")
    else:
        lines.append("(未检测到摩擦数据)")
        lines.append("")

    if friction_details:
        lines.append("**摩擦细节：**")
        lines.append("")
        for dt, fr in friction_details[:8]:
            d_str = dt.strftime("%m-%d") if dt else ""
            lines.append(f"- [{d_str}] {fr}")
        if len(friction_details) > 8:
            lines.append(f"- ... 还有 {len(friction_details) - 8} 条")
        lines.append("")

    if interruptions > 0:
        lines.append(f"**用户打断 Claude 共 {interruptions} 次。**")
        lines.append("")

    # ── Karpathy 风格深度建议（双轮轰炸之下层）──
    stats_dict = {
        "first_date": first_date,
        "last_date": last_date,
        "n": n,
        "total_dur": total_dur,
        "total_user_msgs": total_user_msgs,
        "total_commits": total_commits,
        "bash": tool_counter.get("Bash", 0),
        "read": tool_counter.get("Read", 0),
        "edit": tool_counter.get("Edit", 0),
        "write": tool_counter.get("Write", 0),
        "interruptions": interruptions,
        "morning": morning,
        "afternoon": afternoon,
        "night": night,
        "midnight": midnight,
        "frictions": frictions,
        "friction_details": friction_details,
        "goals": goals,
        "outcomes": outcomes,
        "habits": [],
    }
    deep_advice = generate_coaching_advice(stats_dict, translations)
    if deep_advice:
        lines.append("## 深度建议")
        lines.append("")
        lines.append("*Andrej Karpathy 风格——直接、有洞察、从数据里看出认知陷阱。*")
        lines.append("")
        for i, a in enumerate(deep_advice, 1):
            lines.append(f"### #{i:02d} {a['title']}")
            lines.append("")
            if a.get("evidence"):
                lines.append(f"**证据：**{a['evidence']}")
                lines.append("")
            if a.get("cause"):
                lines.append(f"**根因：**{a['cause']}")
                lines.append("")
            if a.get("action"):
                lines.append(f"**行动：**{a['action']}")
                lines.append("")
        lines.append("")

    # ── 会话摘要精选 ──
    if brief_summaries:
        lines.append("## 会话摘要精选")
        lines.append("")
        for dt, bs in brief_summaries[:6]:
            d_str = dt.strftime("%m-%d") if dt else ""
            lines.append(f"- [{d_str}] {bs}")
        if len(brief_summaries) > 6:
            lines.append(f"- ... 还有 {len(brief_summaries) - 6} 条")
        lines.append("")

    # ── 底部 ──
    has_jsonl = any(str(it.get("facet", {}).get("_source", "")).startswith("jsonl") for it in items)
    data_source = "~/.claude/projects/*/*.jsonl（原始会话数据）" if has_jsonl else "~/.claude/usage-data/facets/ + session-meta/"
    lines.append("---")
    lines.append(f"\n报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"数据来源：{data_source}")
    lines.append(f"生成命令：insight-zh")

    return "\n".join(lines) + "\n"


def detect_anomalies(items, translations=None):
    """跑多维度交叉分析，找出所有反常信号。
    返回 list of dict: {level, icon, category, title, evidence, meaning, samples}
    """
    if translations is None:
        translations = {}
    if not items:
        return []

    anomalies = []
    n = len(items)

    # 准备每个会话的完整数据
    sess = []
    for it in items:
        f = it["facet"]
        m = it["meta"]
        sid = (f.get("session_id") or "")[:8]
        goal = f.get("underlying_goal", "")
        goal_zh = translations.get(goal, goal)
        date_str = it["date"].strftime("%m-%d") if it["date"] else ""
        bash = m.get("tool_counts", {}).get("Bash", 0)
        read = m.get("tool_counts", {}).get("Read", 0)
        dur = m.get("duration_minutes", 0)
        umsgs = m.get("user_message_count", 0)
        inter = m.get("user_interruptions", 0)
        commits = get_git_commit_count(m)
        outcome = f.get("outcome", "unknown")
        st = f.get("session_type", "unknown")
        # 时段
        seg = "未知"
        if m.get("start_time"):
            try:
                h = datetime.fromisoformat(m["start_time"].replace("Z", "+00:00")).astimezone().hour
                if 0 <= h < 6: seg = "凌晨"
                elif 6 <= h < 12: seg = "上午"
                elif 12 <= h < 18: seg = "下午"
                else: seg = "晚上"
            except Exception:
                pass
        # 工作方向大类
        big_cats = set()
        for k in f.get("goal_categories", {}).keys():
            big_cats.add(classify_goal(k))
        frictions = list(f.get("friction_counts", {}).keys())
        sess.append({
            "sid": sid, "goal": goal_zh, "date": date_str,
            "bash": bash, "read": read, "dur": dur, "umsgs": umsgs,
            "inter": inter, "commits": commits,
            "outcome": outcome, "st": st, "seg": seg,
            "big_cats": big_cats, "frictions": frictions,
        })

    avg_dur = sum(s["dur"] for s in sess) / max(n, 1)
    avg_umsgs = sum(s["umsgs"] for s in sess) / max(n, 1)
    avg_bash = sum(s["bash"] for s in sess) / max(n, 1)
    failed = [s for s in sess if s["outcome"] in ("not_achieved", "partially_achieved")]
    fail_rate_overall = len(failed) / max(n, 1)

    def add_anomaly(level, category, title, evidence, meaning, samples=None, method=None):
        icon = {"red": "🔴", "green": "🟢", "yellow": "🟡"}[level]
        anomalies.append({
            "level": level, "icon": icon, "category": category,
            "title": title, "evidence": evidence, "meaning": meaning,
            "method": method or "",
            "samples": samples or [],
        })

    def _sample(s):
        """把 sess dict 简化成 sample 字段（保留所有有用信息给 drill 显示）"""
        return {
            "date": s["date"], "sid": s["sid"], "goal": s["goal"],
            "dur": s["dur"], "umsgs": s["umsgs"],
            "bash": s["bash"], "read": s["read"],
            "commits": s["commits"], "outcome": s["outcome"],
            "frictions": s["frictions"],
        }

    # ── 1. 会话类型 × 达成度 ──
    type_stats = {}
    for s in sess:
        type_stats.setdefault(s["st"], {"total": 0, "fail": 0, "full": 0})
        type_stats[s["st"]]["total"] += 1
        if s["outcome"] == "not_achieved":
            type_stats[s["st"]]["fail"] += 1
        if s["outcome"] == "fully_achieved":
            type_stats[s["st"]]["full"] += 1

    for st_key, st_label in SESSION_TYPE_MAP.items():
        st_data = type_stats.get(st_key, {"total": 0, "fail": 0, "full": 0})
        if st_data["total"] < 5:
            continue
        fail_r = st_data["fail"] / st_data["total"]
        full_r = st_data["full"] / st_data["total"]
        if fail_r > fail_rate_overall * 1.8 and fail_r > 0.2:
            samples = [_sample(s) for s in sess
                       if s["st"] == st_key and s["outcome"] == "not_achieved"][:6]
            add_anomaly(
                "red", "会话类型 × 达成度",
                f"「{st_label}」失败率 {fail_r*100:.0f}%，是全局均值 {fail_rate_overall*100:.0f}% 的 {fail_r/fail_rate_overall:.1f} 倍",
                f"{st_data['total']} 个{st_label}会话里，{st_data['fail']} 个未达成。全局失败率 {fail_rate_overall*100:.0f}%。",
                f"这类会话比其他类型更容易失败。深度排查根因可能是工具链、提示模糊、或外部依赖。",
                samples=samples,
            )
        if full_r > 0.55:
            green_samples = [_sample(s) for s in sess
                             if s["st"] == st_key and s["outcome"] == "fully_achieved"][:6]
            add_anomaly(
                "green", "会话类型 × 达成度",
                f"「{st_label}」完全达成率 {full_r*100:.0f}%，远高于其他类型",
                f"{st_data['total']} 个会话里 {st_data['full']} 个完全达成。",
                f"这是你做得好的工作模式，可以总结其中的规律。",
                samples=green_samples,
            )

    # ── 2. 时段 × 达成度 ──
    seg_stats = {}
    for s in sess:
        seg_stats.setdefault(s["seg"], {"total": 0, "fail": 0, "full": 0, "bash": 0, "read": 0})
        seg_stats[s["seg"]]["total"] += 1
        if s["outcome"] == "not_achieved":
            seg_stats[s["seg"]]["fail"] += 1
        if s["outcome"] == "fully_achieved":
            seg_stats[s["seg"]]["full"] += 1
        seg_stats[s["seg"]]["bash"] += s["bash"]
        seg_stats[s["seg"]]["read"] += s["read"]

    for seg_label, sd in seg_stats.items():
        if seg_label == "未知" or sd["total"] < 5:
            continue
        fail_r = sd["fail"] / sd["total"]
        if fail_r > fail_rate_overall * 1.5 and fail_r > 0.15:
            samples = [_sample(s) for s in sess
                       if s["seg"] == seg_label and s["outcome"] == "not_achieved"][:6]
            add_anomaly(
                "red", "时段 × 达成度",
                f"「{seg_label}」时段失败率 {fail_r*100:.0f}%，高于均值 {fail_rate_overall*100:.0f}%",
                f"{sd['total']} 个{seg_label}会话里，{sd['fail']} 个未达成。",
                f"这个时段你的产出质量明显下降。要么是认知状态，要么是外部干扰多。",
                samples=samples,
            )

    # ── 3. 时段 × Bash/Read 比 ──
    for seg_label, sd in seg_stats.items():
        if seg_label == "未知" or sd["total"] < 5 or sd["read"] == 0:
            continue
        ratio = sd["bash"] / sd["read"]
        if ratio > 5:
            seg_bash_samples = sorted(
                [s for s in sess if s["seg"] == seg_label and s["bash"] > 10],
                key=lambda x: -x["bash"]
            )[:6]
            samples = [_sample(s) for s in seg_bash_samples]
            add_anomaly(
                "red", "时段 × 工具使用",
                f"「{seg_label}」Bash/Read 比 {ratio:.1f}:1，远超健康基线 2:1",
                f"{seg_label} {sd['total']} 个会话，Bash {sd['bash']} 次 vs Read {sd['read']} 次。",
                f"这个时段你倾向于让 Claude 用 Bash 探索，而不是先读文件。"
                f"很可能跟时段的认知状态有关 — 累的时候更容易选'按一下试试'而不是'读一下想清楚'。",
                samples=samples,
            )

    # ── 4. 工作方向 × 达成度 ──
    goal_stats = {}
    for s in sess:
        for cat in s["big_cats"]:
            goal_stats.setdefault(cat, {"total": 0, "fail": 0, "full": 0, "commits": 0})
            goal_stats[cat]["total"] += 1
            if s["outcome"] == "not_achieved":
                goal_stats[cat]["fail"] += 1
            if s["outcome"] == "fully_achieved":
                goal_stats[cat]["full"] += 1
            goal_stats[cat]["commits"] += s["commits"]

    for cat, cd in goal_stats.items():
        if cd["total"] < 5:
            continue
        fail_r = cd["fail"] / cd["total"]
        if fail_r > fail_rate_overall * 1.5 and cd["fail"] >= 2:
            samples = [_sample(s) for s in sess
                       if cat in s["big_cats"] and s["outcome"] == "not_achieved"][:5]
            add_anomaly(
                "red", "工作方向 × 达成度",
                f"「{cat}」失败率 {fail_r*100:.0f}%，明显偏高",
                f"{cd['total']} 个相关会话，{cd['fail']} 个未达成，{cd['full']} 个完全达成。",
                f"这个方向你卡得最深。是技术栈问题、知识盲区、还是工具不顺手？",
                samples=samples,
            )

    # ── 5. 「修工具不出活」识别 ──
    for cat, cd in goal_stats.items():
        if cd["total"] >= 10 and cd["commits"] == 0:
            cat_samples = [_sample(s) for s in sess if cat in s["big_cats"]][:8]
            add_anomaly(
                "red", "投入产出比",
                f"「{cat}」{cd['total']} 个会话，0  个 commit",
                f"这个方向你投入了 {cd['total']} 个会话，但没有任何代码 commit 落地。",
                f"这是典型的'修工具'模式 —— 你花时间维护基础设施，但没产生可交付的产品。"
                f"问问自己：这些时间换来了什么？知识？还是只是消耗？",
                samples=cat_samples,
            )

    # ── 6. 沉没成本会话 ──
    sunk = [s for s in sess if s["dur"] > 60 and s["umsgs"] >= 20 and s["outcome"] in ("not_achieved", "partially_achieved")]
    if sunk:
        samples = [_sample(s) for s in sorted(sunk, key=lambda x: -x["dur"])[:6]]
        total_wasted = sum(s["dur"] for s in sunk)
        add_anomaly(
            "red", "沉没成本",
            f"{len(sunk)} 个会话超过 1 小时且消息 20+ 但最终失败，浪费 {total_wasted} 分钟",
            f"这些会话单次时长 60+ 分钟，用户消息 20+ 条，但 outcome 是未达成或部分达成。"
            f"总共浪费 {total_wasted} 分钟（约 {total_wasted//60} 小时）。",
            f"明知失败信号已出现，仍在'再试一种写法'。需要一条'3 次失败止损'规则。",
            samples=samples,
        )

    # ── 7. 打断频繁的会话 ──
    high_inter = [s for s in sess if s["inter"] >= 3]
    if len(high_inter) >= 3:
        # 打断高的会话失败率
        hi_fail = sum(1 for s in high_inter if s["outcome"] in ("not_achieved", "partially_achieved")) / len(high_inter)
        if hi_fail > fail_rate_overall * 1.3:
            samples = [_sample(s) for s in sorted(high_inter, key=lambda x: -x["inter"])[:5]]
            add_anomaly(
                "red", "打断频率 × 达成度",
                f"打断 ≥ 3 次的 {len(high_inter)} 个会话，失败率 {hi_fail*100:.0f}%（均值 {fail_rate_overall*100:.0f}%）",
                f"频繁打断意味着 Claude 跑偏了，但你没有从一开始就阻止它。",
                f"打断是事后补救。真正的功夫是开头那一句 prompt 就让它走对方向。"
                f"打断 3 次以上 = 你的 prompt 严重不到位。",
                samples=samples,
            )

    # ── 8. Bash 重度滥用会话 ──
    bash_heavy = [s for s in sess if s["bash"] > 30 and s["read"] < 5]
    if bash_heavy:
        bh_fail = sum(1 for s in bash_heavy if s["outcome"] in ("not_achieved", "partially_achieved")) / len(bash_heavy)
        samples = [_sample(s) for s in sorted(bash_heavy, key=lambda x: -x["bash"])[:5]]
        add_anomaly(
            "yellow", "Bash 滥用",
            f"{len(bash_heavy)} 个会话 Bash > 30 但 Read < 5",
            f"这些会话里 Claude 几乎只跑 shell，几乎不读文件。失败/部分达成率 {bh_fail*100:.0f}%。",
            f"这是'探索代替阅读'的极端表现。下次见到 Bash 超过 30 还没解决问题，"
            f"应该意识到方向不对了 —— 停下来要 Claude 给个假设再继续。",
            samples=samples,
        )

    # ── 9. 高产出会话特征 ──
    high_commit = [s for s in sess if s["commits"] >= 1]
    if len(high_commit) >= 5 and len(sess) - len(high_commit) >= 5:
        # 对比有 commit vs 没 commit 的会话特征
        hc_bash = sum(s["bash"] for s in high_commit) / len(high_commit)
        hc_read = sum(s["read"] for s in high_commit) / len(high_commit)
        nc = [s for s in sess if s["commits"] == 0]
        nc_bash = sum(s["bash"] for s in nc) / len(nc)
        nc_read = sum(s["read"] for s in nc) / len(nc)
        if hc_read > 0 and nc_read > 0:
            hc_ratio = hc_bash / hc_read
            nc_ratio = nc_bash / nc_read
            if hc_ratio < nc_ratio * 0.7:
                hc_samples = [_sample(s) for s in sorted(high_commit, key=lambda x: -x["commits"])[:8]]
                add_anomaly(
                    "green", "产出 vs 工具使用",
                    f"产出 commit 的会话，Bash/Read 比仅 {hc_ratio:.1f}:1；没产出的会话比 {nc_ratio:.1f}:1",
                    f"{len(high_commit)} 个有 commit 的会话平均 Bash {hc_bash:.0f} · Read {hc_read:.0f}。"
                    f"{len(nc)} 个无 commit 的会话平均 Bash {nc_bash:.0f} · Read {nc_read:.0f}。",
                    f"数据明确证明：**读得多的会话更容易出活**。这是经验法则，不是直觉。",
                    samples=hc_samples,
                )

    # ── 10. 长会话 vs 短会话效率 ──
    long_sess = [s for s in sess if s["dur"] > 120]
    short_sess = [s for s in sess if 5 <= s["dur"] < 30]
    if len(long_sess) >= 5 and len(short_sess) >= 5:
        long_full = sum(1 for s in long_sess if s["outcome"] == "fully_achieved") / len(long_sess)
        short_full = sum(1 for s in short_sess if s["outcome"] == "fully_achieved") / len(short_sess)
        if long_full < short_full * 0.7 and long_full < 0.3:
            long_bad = [_sample(s) for s in sorted(
                [s for s in long_sess if s["outcome"] != "fully_achieved"], key=lambda x: -x["dur"]
            )[:6]]
            add_anomaly(
                "yellow", "时长 vs 达成度",
                f"超过 2 小时的会话「完全达成率」{long_full*100:.0f}%，低于 30 分钟以下的 {short_full*100:.0f}%",
                f"长会话 {len(long_sess)} 个，完全达成 {sum(1 for s in long_sess if s['outcome'] == 'fully_achieved')} 个；"
                f"短会话 {len(short_sess)} 个，完全达成 {sum(1 for s in short_sess if s['outcome'] == 'fully_achieved')} 个。",
                f"长不等于深。超过 2 小时的会话通常意味着'卡住了不止损'，而不是'在深度思考'。",
                samples=long_bad,
            )

    # ── 11. 「问候即失败」模式 ──
    greetings = [s for s in sess if s["goal"] and ("greet" in s["goal"].lower() or "你好" in s["goal"] or "hello" in s["goal"].lower() or "initiate" in s["goal"].lower() or "start a conversation" in s["goal"].lower())]
    if len(greetings) >= 3:
        g_fail = sum(1 for s in greetings if s["outcome"] in ("not_achieved", "unclear_from_transcript"))
        if g_fail >= len(greetings) * 0.5:
            samples = [_sample(s) for s in greetings[:5]]
            add_anomaly(
                "red", "「打招呼即失败」",
                f"{len(greetings)} 个会话以打招呼/问候开始，{g_fail} 个失败",
                f"你养成了'开会话先打招呼测试'的习惯。但每次打招呼都在消耗工具链可靠性。",
                f"省下问候，第一句话就是任务。能立刻知道工具链是否就绪，省下打开和恢复的认知开销。",
                samples=samples,
            )

    # ── 12. 摩擦类型 × 时段交叉 ──
    seg_friction = {}
    for s in sess:
        if s["seg"] == "未知":
            continue
        for fr in s["frictions"]:
            seg_friction.setdefault(s["seg"], Counter())[fr] += 1
    # 找出特定时段特定摩擦异常高的
    fr_total = Counter()
    for s in sess:
        for fr in s["frictions"]:
            fr_total[fr] += 1
    for seg_label, fr_counter in seg_friction.items():
        for fr_key, fr_v in fr_counter.most_common(3):
            total_fr = fr_total[fr_key]
            if total_fr < 5 or fr_v < 3:
                continue
            seg_share = seg_stats[seg_label]["total"] / n
            expected = total_fr * seg_share
            if fr_v > expected * 1.8 and fr_v >= 3:
                fr_label = FRICTION_MAP.get(fr_key, fr_key)
                fr_seg_samples = [_sample(s) for s in sess
                                  if s["seg"] == seg_label and fr_key in s["frictions"]][:6]
                add_anomaly(
                    "yellow", "时段 × 摩擦类型",
                    f"「{seg_label}」时段「{fr_label}」摩擦 {fr_v} 次，比预期多 {(fr_v/expected-1)*100:.0f}%",
                    f"在{seg_label}，「{fr_label}」出现 {fr_v} 次；按这个时段的会话占比 {seg_share*100:.0f}%，预期约 {expected:.1f} 次。",
                    f"这种摩擦在该时段集中爆发，可能跟你那个时段的状态/工具/任务类型有关。",
                    samples=fr_seg_samples,
                )

    return anomalies


def get_advice_cache_path(stats_dict):
    payload = {
        "first_date": str(stats_dict.get("first_date", "")),
        "last_date": str(stats_dict.get("last_date", "")),
        "n": stats_dict.get("n", 0),
        "total_dur": stats_dict.get("total_dur", 0),
        "total_user_msgs": stats_dict.get("total_user_msgs", 0),
        "total_commits": stats_dict.get("total_commits", 0),
        "frictions": list(stats_dict.get("frictions", Counter()).most_common()) if hasattr(stats_dict.get("frictions"), "most_common") else stats_dict.get("frictions", {}),
        "goals": list(stats_dict.get("goals", Counter()).most_common()) if hasattr(stats_dict.get("goals"), "most_common") else stats_dict.get("goals", {}),
        "outcomes": list(stats_dict.get("outcomes", Counter()).most_common()) if hasattr(stats_dict.get("outcomes"), "most_common") else stats_dict.get("outcomes", {}),
    }
    digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    first_date = stats_dict.get("first_date") or "unknown"
    last_date = stats_dict.get("last_date") or "unknown"
    return REPORTS_DIR / f".advice-cache-{first_date}-{last_date}-{digest}.json"


def generate_coaching_advice(stats_dict, translations=None, force_regenerate=False, use_external_llm=False):
    """Generate coaching advice.

    Default path is local rule-based advice so the report never requires an API
    key. External LLM advice is opt-in via --llm-advice.
    """
    if translations is None:
        translations = {}

    if not use_external_llm:
        return generate_rule_coaching_advice(stats_dict)

    if not API_KEY:
        print("  未设置 INSIGHT_API_KEY，使用本地规则建议", file=sys.stderr)
        return generate_rule_coaching_advice(stats_dict)

    advice_cache_file = get_advice_cache_path(stats_dict)
    if not force_regenerate and advice_cache_file.exists():
        try:
            cached = json.loads(advice_cache_file.read_text(encoding="utf-8"))
            print(f"  使用深度建议缓存（{len(cached)} 条）", file=sys.stderr)
            return cached
        except Exception:
            pass

    # 构造证据材料
    s = stats_dict
    evidence_lines = []
    evidence_lines.append(f"总览：{s['n']} 个会话，{s['total_dur']} 分钟（约 {s['total_dur']//60} 小时），{s['total_user_msgs']} 条消息，{s['total_commits']}  个 commit")
    evidence_lines.append(f"时间分布：凌晨 {s['midnight']}、上午 {s['morning']}、下午 {s['afternoon']}、晚上 {s['night']}")
    evidence_lines.append(f"工具使用：Bash {s['bash']} · Read {s['read']} · Edit {s['edit']} · Write {s['write']}（Bash/Read 比 {s['bash']/max(s['read'],1):.1f}）")
    evidence_lines.append(f"用户打断 Claude 共 {s['interruptions']} 次")

    if s['frictions']:
        evidence_lines.append("摩擦类型 Top 5：")
        for k, v in s['frictions'].most_common(5):
            label = FRICTION_MAP.get(k, k)
            evidence_lines.append(f"  - {label}：{v} 次")

    if s['friction_details']:
        evidence_lines.append("具体摩擦案例（按时间倒序，前 8 条）：")
        for dt, fr in s['friction_details'][:8]:
            d_str = dt.strftime("%m-%d") if dt else ""
            tr = translations.get(fr, fr)
            evidence_lines.append(f"  [{d_str}] {tr}")

    if s['goals']:
        evidence_lines.append("工作方向 Top 5（按权重）：")
        for k, v in s['goals'].most_common(5):
            evidence_lines.append(f"  - {k}：{v}")

    if s['outcomes']:
        evidence_lines.append("会话结果分布：")
        for k, v in s['outcomes'].most_common():
            label = OUTCOME_MAP.get(k, k)
            evidence_lines.append(f"  - {label}：{v}")

    evidence_text = "\n".join(evidence_lines)

    prompt = f"""你是一位深刻的 AI 工作教练，风格类似 Andrej Karpathy ——
直接、有洞察、不说教，从数据里看出行为模式背后的认知陷阱。

下面是用户使用 Claude Code（AI 编程工具）的全部行为数据。
他自评「用得不够好」，想真正进步。请基于这些数据，给出 **5 条**深度建议。

**严格要求：**
1. 每条建议必须**引用具体证据**（数字、日期、案例），不能是空话
2. 给出**根因分析**：为什么会这样？背后的认知/习惯/工作模式问题是什么？
3. 提出**可执行的行动**：不要空泛的「多用 Read」，而是「下周一开始，每次开会话前先 ...」
4. 维度要**多样**：不只是工具使用，包括时间管理、注意力、产出节奏、心智模式、长期目标
5. 语气**直接、不奉承、不批判**，像 Karpathy 一样客观陈述事实
6. 中文**自然流畅**，不要翻译腔，不要"赋能""核心能力"这种空词
7. 每条建议大约 150-200 字

**输出格式**（严格遵守，每条建议之间用 `---` 分隔）：

### 标题（一句话，直击要害）

**证据**：[引用 2-3 个具体数字或案例]

**根因**：[1-2 句分析背后的模式或陷阱]

**行动**：[具体下一步，可执行，最好带时间或频率]

---

数据：

{evidence_text}

开始给出 5 条建议（每条都要有完整的标题/证据/根因/行动结构）："""

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=API_KEY, base_url=API_BASE_URL)
        print("  调用 LLM 生成深度建议...", file=sys.stderr)
        resp = client.messages.create(
            model=API_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text
        print(f"  LLM 返回 {len(raw)} 字符", file=sys.stderr)

        # 解析返回的建议
        advice_blocks = [b.strip() for b in raw.split("---") if b.strip()]
        advice_list = []
        for block in advice_blocks:
            if not block or "###" not in block:
                continue
            # 解析每个块
            parts = block.split("###", 1)
            if len(parts) < 2:
                continue
            content = parts[1].strip()
            lines = content.split("\n", 1)
            title = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""

            # 提取证据/根因/行动
            evidence = ""
            cause = ""
            action = ""
            current = None
            for line in body.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "**证据**" in line or "证据：" in line or line.startswith("**证据**"):
                    current = "evidence"
                    text = line.split("**", 2)[-1].replace("：", "", 1).strip() if "**" in line else line.replace("证据：", "").strip()
                    evidence = text
                elif "**根因**" in line or "根因：" in line:
                    current = "cause"
                    text = line.split("**", 2)[-1].replace("：", "", 1).strip() if "**" in line else line.replace("根因：", "").strip()
                    cause = text
                elif "**行动**" in line or "行动：" in line:
                    current = "action"
                    text = line.split("**", 2)[-1].replace("：", "", 1).strip() if "**" in line else line.replace("行动：", "").strip()
                    action = text
                else:
                    # 追加到当前字段
                    if current == "evidence":
                        evidence += " " + line
                    elif current == "cause":
                        cause += " " + line
                    elif current == "action":
                        action += " " + line

            if title and (evidence or cause or action):
                advice_list.append({
                    "title": title,
                    "evidence": evidence.strip(),
                    "cause": cause.strip(),
                    "action": action.strip(),
                })

        # 保存当天缓存
        if advice_list:
            try:
                advice_cache_file.write_text(json.dumps(advice_list, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  深度建议已缓存到 {advice_cache_file.name}", file=sys.stderr)
            except Exception as e:
                print(f"  (建议缓存保存失败: {e})", file=sys.stderr)

        return advice_list
    except Exception as e:
        print(f"  (LLM 建议生成失败: {e})", file=sys.stderr)
        return generate_rule_coaching_advice(stats_dict)


def generate_rule_coaching_advice(stats_dict):
    """Local rule-based coaching cards used by default."""
    s = stats_dict
    advice = []
    bash = s.get("bash", 0)
    read = s.get("read", 0)
    ratio = bash / max(read, 1)
    commits = s.get("total_commits", 0)
    total_dur = s.get("total_dur", 0)
    total_msgs = s.get("total_user_msgs", 0)
    n = s.get("n", 0)

    if commits == 0 and total_dur >= 180:
        advice.append({
            "title": "今天的工作没有留下 Git 产物",
            "evidence": f"{n} 个会话，约 {total_dur // 60} 小时活跃时长，检测到 0 个 Git commit。",
            "cause": "你把大量时间花在探索、排障和讨论上，但没有把中间状态固化成版本历史。",
            "action": "明天每个超过 30 分钟的会话结束前，必须产生一次 commit，哪怕只是 wip。报告会按真实 git log 统计，不依赖是哪一个 AI 提交。",
        })
    elif commits > 0 and total_dur / max(commits, 1) > 120:
        advice.append({
            "title": "有 commit，但落地频率偏低",
            "evidence": f"约 {total_dur // 60} 小时活跃时长，检测到 {commits} 个 Git commit，约 {int(total_dur / commits)} 分钟/commit。",
            "cause": "你不是完全没有产出，而是产出间隔太长。长时间探索后才提交，会让中间判断和失败路径消失。",
            "action": "把 commit 当作工作节拍器：每 60-90 分钟至少提交一次 wip，后面可以 squash，但当天不能没有过程记录。",
        })

    if ratio > 2:
        advice.append({
            "title": "Shell 探索压过了文件理解",
            "evidence": f"Bash {bash} 次，Read {read} 次，Bash/Read 比 {ratio:.1f}:1。",
            "cause": "这通常意味着 Claude 在用命令行试探系统，而不是先读关键文件建立模型。",
            "action": "给下一次会话加硬约束：前 5 分钟只允许 Read/Grep，不允许 Bash 改状态；需要执行命令时先说明目的。",
        })

    avg_msgs = total_msgs / max(n, 1)
    if avg_msgs > 12:
        advice.append({
            "title": "你在用来回修正补足开场定义",
            "evidence": f"平均每个会话 {avg_msgs:.1f} 条真实用户消息。",
            "cause": "消息数不算离谱，但已经说明不少需求是在过程中逐步补充的。",
            "action": "开新会话前先写三行：目标、不要做什么、完成标准。这样能减少后续纠偏。",
        })

    if not advice:
        advice.append({
            "title": "今天的使用模式没有明显红灯",
            "evidence": f"{n} 个会话，{total_msgs} 条真实用户消息，约 {total_dur // 60} 小时活跃时长。",
            "cause": "从规则指标看，当前最大问题不是单点故障，而是需要继续积累趋势数据。",
            "action": "继续每天生成 HTML 报告，重点看 Git 产物、Bash/Read 比、异常信号是否连续三天恶化。",
        })

    return advice[:5]



def _md_lines_to_html(md_lines):
    """将 markdown 行列表转为简单 HTML。"""
    html_parts = []
    for line in md_lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            html_parts.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("### "):
            html_parts.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("- "):
            text = line[2:]
            text = _md_bold(text)
            html_parts.append(f"<li>{text}</li>")
        elif re.match(r"^\*\*[^*]*可以试的\*\*", line):
            text = _md_bold(line)
            html_parts.append(f'<div style="margin:12px 0;padding:12px 16px;background:#f0fdf4;border-left:3px solid #22c55e;border-radius:4px;">{text}</div>')
        else:
            text = _md_bold(line)
            html_parts.append(f"<p>{text}</p>")
    return "\n".join(html_parts)


def _md_bold(text):
    """替换 **text** 为 <strong>。"""
    import re
    return re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)


def _escape_html(value):
    return html_lib.escape(str(value or ""), quote=True)


def _truncate(value, limit=120):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _theme_bucket(raw_categories, goal, summary):
    raw_categories = list(raw_categories or [])
    big_categories = {classify_goal(cat) for cat in raw_categories}
    raw_text = " ".join(raw_categories).lower()
    text = " ".join(raw_categories + [goal or "", summary or ""]).lower()

    if (
        "Skill 系统管理" in big_categories
        or "系统管理" in big_categories
        or "工具探索" in big_categories
        or any(k in text for k in ["claude code", "insight", "usage-data", "facets", "skill"])
        or any(k in text for k in ["缓存", "中文报告", "分析器"])
    ):
        return "AI 编程工具链与分析系统"
    if "内容创作" in big_categories or any(k in raw_text for k in ["publish", "wechat", "article", "content", "illustration"]):
        return "内容生产与发布流水线"
    if any(k in text for k in ["codepilot", "client", "ui", "model", "display", "sdk", "electron"]):
        return "应用配置与 UI 排障"
    if any(k in text for k in ["macos", "pmset", "caffeinate", "sleep", "lid", "power"]):
        return "本地环境与系统自动化"
    if any(k in text for k in ["feishu", "lark", "knowledge", "memory", "obsidian", "nowledge", "文档", "知识"]):
        return "知识管理与外部集成"
    if "Git 操作" in big_categories or any(k in raw_text for k in ["github", "git", "repo", "commit", "push"]):
        return "GitHub 与开源交付"
    if "调试与排障" in big_categories or any(k in raw_text for k in ["debug", "bug", "fix"]):
        return "代码修复与调试"
    if "代码与实现" in big_categories:
        return "代码修复与调试"
    return "临时问答与探索"


def _topic_reason(bucket, records):
    sessions = len(records)
    bash = sum(r["bash"] for r in records)
    read = sum(r["read"] for r in records)
    edit_write = sum(r["edit"] + r["write"] for r in records)
    commits = sum(r["commits"] for r in records)
    outcomes = Counter(r["outcome"] for r in records)
    top_outcome = OUTCOME_MAP.get(outcomes.most_common(1)[0][0], outcomes.most_common(1)[0][0]) if outcomes else "未知"
    sample = _truncate(records[0]["summary"] or records[0]["goal"], 160) if records else ""

    if bucket == "AI 编程工具链与分析系统":
        return f"这类会话把 AI 编程工具当成可改造的平台：围绕报告、skill、缓存、工作流做了 {sessions} 个会话，工具调用里 Bash {bash} 次、Edit/Write {edit_write} 次，说明它不是聊天问答，而是本地系统工程。代表会话：{sample}"
    if bucket == "内容生产与发布流水线":
        return f"这是可复用的内容生产线：{sessions} 个会话集中在文章、HTML、图片或发布链路，Read {read} 次、Edit/Write {edit_write} 次。重点不是一次写完，而是把每次摩擦沉淀成流水线能力。代表会话：{sample}"
    if bucket == "应用配置与 UI 排障":
        return f"这类会话集中在 UI 显示、配置文件和真实运行行为的分层排查，共 {sessions} 个。结果多为 {top_outcome}，通常卡在第三方客户端限制或显示层/运行层混淆。代表会话：{sample}"
    if bucket == "本地环境与系统自动化":
        return f"这类工作是典型本机自动化：{sessions} 个会话围绕系统状态、命令环境和长期运行可靠性。Bash {bash} 次说明大部分成本在验证环境事实。代表会话：{sample}"
    if bucket == "知识管理与外部集成":
        return f"这类会话在处理知识持久化、外部系统接入和可检索记录，共 {sessions} 个。它的难点不是写代码，而是权限、数据边界和工具能力是否真实可用。代表会话：{sample}"
    if bucket == "GitHub 与开源交付":
        return f"这类会话最终要落到仓库、README、commit 或 push。共 {sessions} 个会话、{commits} 个 commit，重点是把个人问题包装成可复用资产。代表会话：{sample}"
    if bucket == "代码修复与调试":
        return f"这类会话集中在 bug、测试和修复，共 {sessions} 个，Bash {bash} 次、Read {read} 次、Edit/Write {edit_write} 次。它最需要的是先定位再改，不然容易变成命令试错。代表会话：{sample}"
    return f"这类是低成本探索或临时问答，共 {sessions} 个。它们不一定要沉淀，但如果同一主题反复出现，就应该升级成明确项目。代表会话：{sample}"


def build_insights_like_sections(session_records, totals):
    if not session_records:
        return {
            "themes_html": "",
            "behavior_html": "",
            "friction_story_html": "",
            "playbook_html": "",
        }

    theme_records = {}
    for record in session_records:
        bucket = _theme_bucket(record["raw_categories"], record["goal"], record["summary"])
        theme_records.setdefault(bucket, []).append(record)

    theme_cards = ""
    for bucket, records in sorted(theme_records.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:5]:
        reason = _topic_reason(bucket, records)
        sessions = len(records)
        commits = sum(r["commits"] for r in records)
        msg_count = sum(r["user_messages"] for r in records)
        theme_cards += f"""
<div class="insight-card">
  <div class="insight-card-head"><strong>{_escape_html(bucket)}</strong><span>{sessions} 个会话 · {msg_count} 条消息 · {commits} commit</span></div>
  <p>{_escape_html(reason)}</p>
</div>"""

    themes_html = f"""
<div class="section" id="themes">
  <h2>主要工作流</h2>
  <p class="section-hint">这一段按语义层字段聚合多个会话，展示长期推进的工作流，而不是重复罗列工具指标。</p>
  <div class="insight-stack">{theme_cards}</div>
</div>"""

    bash = totals.get("bash", 0)
    read = totals.get("read", 0)
    ratio = bash / max(read, 1)
    total_msgs = totals.get("total_user_msgs", 0)
    n = totals.get("n", 1)
    avg_msgs = total_msgs / max(n, 1)
    total_dur = totals.get("total_dur", 0)
    commits = totals.get("total_commits", 0)
    frictions = totals.get("frictions", Counter())
    outcomes = totals.get("outcomes", Counter())
    mostly_ok = outcomes.get("fully_achieved", 0) + outcomes.get("mostly_achieved", 0)
    achieved_rate = mostly_ok / max(sum(outcomes.values()), 1)

    behavior_bits = []
    if ratio > 3:
        behavior_bits.append(f"你把 Claude Code 用成了本地 shell 放大器：Bash {bash} 次、Read {read} 次，Bash/Read={ratio:.1f}:1。")
    elif ratio > 1.5:
        behavior_bits.append(f"你在 shell 探索和文件阅读之间摇摆：Bash {bash} 次、Read {read} 次，Bash/Read={ratio:.1f}:1。")
    else:
        behavior_bits.append(f"你的工具使用更接近先读后改：Bash {bash} 次、Read {read} 次，Bash/Read={ratio:.1f}:1。")
    if avg_msgs < 15:
        behavior_bits.append(f"平均每会话 {avg_msgs:.1f} 条用户消息，说明很多需求在开场定义得比较短，后续靠执行结果校正。")
    else:
        behavior_bits.append(f"平均每会话 {avg_msgs:.1f} 条用户消息，说明你经常通过多轮纠偏把需求逐步压实。")
    behavior_bits.append(f"达成率约 {achieved_rate*100:.0f}%（完全/大部分达成），但 {commits} 个 commit 对 {total_dur//60} 小时活跃时间，说明交付节拍仍偏慢。")
    if frictions:
        top_fric = "、".join(FRICTION_MAP.get(k, k) for k, _ in frictions.most_common(3))
        behavior_bits.append(f"最常见摩擦是：{top_fric}。这更像协作协议问题，不只是代码问题。")

    behavior_html = f"""
<div class="section" id="behavior">
  <h2>使用方式画像</h2>
  <div class="narrative narrative-large">
    {''.join(f'<p>{_escape_html(bit)}</p>' for bit in behavior_bits)}
    <div class="key-insight">核心画像：这类使用更接近“可验证、可纠错、可沉淀的执行环境”，而不是一次性聊天问答。</div>
  </div>
</div>"""

    friction_cards = ""
    for ftype, count in frictions.most_common(4):
        label = FRICTION_MAP.get(ftype, ftype)
        examples = []
        for record in session_records:
            if ftype in record["friction_counts"]:
                detail = record["friction_detail"] or record["summary"] or record["goal"]
                if detail:
                    examples.append(_truncate(detail, 150))
            if len(examples) >= 2:
                break
        if not examples:
            examples = ["该摩擦有计数，但没有足够的文字细节。"]
        if ftype in ("wrong_approach", "misunderstood_request"):
            meaning = "Claude 在没有先把边界和事实查清前就行动，你被迫承担 reviewer 和纠偏者角色。"
        elif ftype == "buggy_code":
            meaning = "实现质量或验证不足，导致会话从创造变成返工。"
        elif ftype == "user_rejected_action":
            meaning = "Claude 做了你没有要求或不想要的动作，说明执行前确认不足。"
        else:
            meaning = "这类摩擦需要看具体会话，但它已经反复出现，不应只当偶发。"
        friction_cards += f"""
<div class="insight-card warning-card">
  <div class="insight-card-head"><strong>{_escape_html(label)}</strong><span>{count} 次</span></div>
  <p>{_escape_html(meaning)}</p>
  <ul>{''.join(f'<li>{_escape_html(ex)}</li>' for ex in examples)}</ul>
</div>"""

    friction_story_html = f"""
<div class="section" id="friction-story">
  <h2>反复出问题的地方</h2>
  <p class="section-hint">这一段不再只列摩擦类型，而是解释这些摩擦为什么会反复发生。</p>
  <div class="insight-stack">{friction_cards}</div>
</div>""" if friction_cards else ""

    playbook_items = []
    if frictions.get("wrong_approach", 0) or frictions.get("misunderstood_request", 0):
        playbook_items.append((
            "事实先于方案",
            "任何数量、路径、工具能力、系统状态，先用命令验证再下结论。没有验证就只能写“推测”。",
        ))
    if ratio > 2:
        playbook_items.append((
            "限制 Bash 探索",
            "连续 5 个 Bash 后暂停，要求 Claude 输出当前假设、已排除项、下一步要读哪个文件。",
        ))
    if commits == 0 or total_dur / max(commits, 1) > 120:
        playbook_items.append((
            "把 commit 当作检查点",
            "超过 60-90 分钟的会话必须留下 wip commit 或决策记录；后面可以 squash，但过程不能消失。",
        ))
    if avg_msgs < 15:
        playbook_items.append((
            "开场补三行边界",
            "目标、不要做什么、完成标准。这样不会阻止探索，但能减少中途纠偏。",
        ))
    if not playbook_items:
        playbook_items.append(("保持趋势观察", "当前没有明显重复红灯，继续观察同一指标是否连续恶化三天。"))

    playbook_html = f"""
<div class="section" id="playbook">
  <h2>可复用的协作规则</h2>
  <div class="insight-stack">
    {''.join(f'<div class="playbook-item"><strong>{_escape_html(title)}</strong><p>{_escape_html(body)}</p></div>' for title, body in playbook_items[:5])}
  </div>
</div>"""

    return {
        "themes_html": themes_html,
        "behavior_html": behavior_html,
        "friction_story_html": friction_story_html,
        "playbook_html": playbook_html,
    }


def generate_html_report(items, translations=None, force_regenerate_advice=False, use_external_llm_advice=False):
    if not items:
        return "<html><body><h1>无数据</h1></body></html>"

    if translations is None:
        translations = {}

    n = len(items)
    has_jsonl = any(str(it.get("facet", {}).get("_source", "")).startswith("jsonl") for it in items)
    data_source_html = "~/.claude/projects/*.jsonl（原始会话数据）" if has_jsonl else "Claude Code facets + session-meta"
    first_date = min(it["date"] for it in items if it["date"])
    last_date = max(it["date"] for it in items if it["date"])

    total_user_msgs = 0
    total_assist_msgs = 0
    total_dur = 0
    total_commits = 0
    total_commit_hashes = set()
    total_input_tokens = 0
    total_output_tokens = 0
    tool_counter = Counter()
    lang_counter = Counter()
    session_types = Counter()
    outcomes = Counter()
    satisfactions = Counter()
    helpfulness = Counter()
    successes = Counter()
    frictions = Counter()
    friction_details = []
    goals = Counter()
    goal_categories_raw = Counter()
    brief_summaries = []
    interruptions = 0
    success_sessions = {}    # 反向索引：success type → [(date, goal, sid), ...]
    friction_sessions = {}   # 反向索引：friction type → [(date, goal, fr_detail, sid), ...]
    goal_sessions = {}       # 反向索引：工作方向大类 → [(date, goal, sid), ...]
    session_type_sessions = {}  # 反向索引：会话类型 → [(date, goal, sid), ...]
    outcome_sessions = {}    # 反向索引：达成度 → [(date, goal, sid), ...]
    hour_segment_sessions = {}  # 反向索引：时段 → [(date, goal, sid), ...]
    session_records = []
    semantic_sources = Counter()

    for it in items:
        m = it["meta"]
        f = it["facet"]
        semantic_source = "insight-zh heuristic"
        if f.get("semantic_confidence") == "official" or f.get("_source") == "jsonl+facet":
            semantic_source = "official insights"
        elif f.get("semantic_confidence") == "llm" or str(f.get("_semantic_source", "")).startswith("insight-zh-semantic-v2"):
            semantic_source = "insight-zh LLM"
        semantic_sources[semantic_source] += 1
        total_user_msgs += m.get("user_message_count", 0)
        total_assist_msgs += m.get("assistant_message_count", 0)
        total_dur += m.get("duration_minutes", 0)
        commit_hashes = m.get("git_commit_hashes") or []
        if commit_hashes:
            total_commit_hashes.update(str(h) for h in commit_hashes)
            total_commits = len(total_commit_hashes)
        else:
            total_commits += get_git_commit_count(m)
        total_input_tokens += m.get("input_tokens", 0)
        total_output_tokens += m.get("output_tokens", 0)
        for k, v in m.get("tool_counts", {}).items():
            tool_counter[k] += v
        for k, v in m.get("languages", {}).items():
            lang_counter[k] += v
        session_types[f.get("session_type", "unknown")] += 1
        outcome = f.get("outcome")
        if outcome and outcome != "unknown":
            outcomes[outcome] += 1
        for k, v in f.get("user_satisfaction_counts", {}).items():
            satisfactions[k] += v
        helpful = f.get("claude_helpfulness")
        if helpful and helpful != "unknown":
            helpfulness[helpful] += 1
        primary_succ = f.get("primary_success")
        if primary_succ:
            successes[primary_succ] += 1
        for k, v in f.get("friction_counts", {}).items():
            frictions[k] += 1
        fr = f.get("friction_detail", "")
        goal = f.get("underlying_goal", "")
        goal_zh = translations.get(goal, goal) if goal else ""
        fr_zh = translations.get(fr, fr) if fr else ""
        # 反向索引：每个 success/friction 类型对应哪些会话
        sid_short = (f.get("session_id") or "")[:8]
        if primary_succ and primary_succ != "none":
            success_sessions.setdefault(primary_succ, []).append((it["date"], goal_zh, sid_short))
        for ftype in f.get("friction_counts", {}).keys():
            friction_sessions.setdefault(ftype, []).append((it["date"], goal_zh, fr_zh, sid_short))
        # 反向索引：会话类型、达成度
        st_key = f.get("session_type", "unknown")
        session_type_sessions.setdefault(st_key, []).append((it["date"], goal_zh, sid_short))
        oc_key = f.get("outcome")
        if oc_key and oc_key != "unknown":
            outcome_sessions.setdefault(oc_key, []).append((it["date"], goal_zh, sid_short))
        # 反向索引：时段
        st_iso = m.get("start_time", "")
        if st_iso:
            try:
                h = datetime.fromisoformat(st_iso.replace("Z", "+00:00")).astimezone().hour
                if 0 <= h < 6:
                    seg = "凌晨"
                elif 6 <= h < 12:
                    seg = "上午"
                elif 12 <= h < 18:
                    seg = "下午"
                else:
                    seg = "晚上"
                hour_segment_sessions.setdefault(seg, []).append((it["date"], goal_zh, sid_short))
            except Exception:
                pass
        if fr:
            friction_details.append((it["date"], translations.get(fr, fr)))
        for k, v in f.get("goal_categories", {}).items():
            goal_categories_raw[k] += v
            big_cat = classify_goal(k)
            goals[big_cat] += v
            # 反向索引：工作方向大类（每个会话可能贡献给多个大类，去重 sid）
            existing = goal_sessions.setdefault(big_cat, [])
            if not existing or existing[-1][2] != sid_short:  # 简单去重
                existing.append((it["date"], goal_zh, sid_short))
        bs = f.get("brief_summary", "")
        if bs:
            brief_summaries.append((it["date"], translations.get(bs, bs)))
        interruptions += m.get("user_interruptions", 0)
        session_records.append({
            "date": it["date"],
            "sid": sid_short,
            "goal": goal_zh,
            "summary": translations.get(bs, bs) if bs else goal_zh,
            "raw_categories": list((f.get("goal_categories") or {}).keys()),
            "outcome": f.get("outcome", "unknown"),
            "session_type": f.get("session_type", "unknown"),
            "friction_counts": dict(f.get("friction_counts") or {}),
            "friction_detail": fr_zh,
            "primary_success": primary_succ or "",
            "user_messages": m.get("user_message_count", 0),
            "duration": m.get("duration_minutes", 0),
            "bash": (m.get("tool_counts") or {}).get("Bash", 0),
            "read": (m.get("tool_counts") or {}).get("Read", 0),
            "edit": (m.get("tool_counts") or {}).get("Edit", 0),
            "write": (m.get("tool_counts") or {}).get("Write", 0),
            "commits": get_git_commit_count(m),
        })

    # ── 绘画方法论分析 ──
    hours = Counter()
    for it in items:
        st = it["meta"].get("start_time", "")
        if st:
            try:
                h = datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone().hour
                hours[h] += 1
            except Exception:
                pass
    total_tools = tool_counter
    painting_md = generate_painting_analysis(items, total_tools, total_dur, total_user_msgs, total_commits, hours, goals)
    painting_html_content = _md_lines_to_html([line for line in painting_md if line.strip() != "## 画室观察笔记"])
    painting_section_html = f'''<div class="section" id="painting">
  <h2>画室观察笔记</h2>
  <p style="color:var(--text-dim);margin-bottom:20px;font-size:0.95rem;">基于 Paul Graham《黑客与画家》的方法论，把你的 Claude Code 使用比作画家在画室创作。</p>
  {painting_html_content}
</div>'''

    # ── 新增：按日期分布数据（用于时间分布图）──
    date_distribution = {}  # date -> {"count": 0, "dur": 0, "msgs": 0}
    for it in items:
        d = it["date"]
        if not d:
            continue
        if d not in date_distribution:
            date_distribution[d] = {"count": 0, "dur": 0, "msgs": 0}
        date_distribution[d]["count"] += 1
        date_distribution[d]["dur"] += it["meta"].get("duration_minutes", 0)
        date_distribution[d]["msgs"] += it["meta"].get("user_message_count", 0)

    # ── 新增：commit 列表（用于详情展开）──
    commit_list = []
    for it in items:
        m = it["meta"]
        commits = get_git_commit_count(m)
        if commits > 0:
            goal = it["facet"].get("underlying_goal", "")
            goal_zh = translations.get(goal, goal) if goal else "(无目标)"
            sid = (it["facet"].get("session_id") or "")[:8]
            commit_list.append({
                "date": it["date"],
                "sid": sid,
                "goal": goal_zh,
                "commits": commits,
                "dur": m.get("duration_minutes", 0),
            })
    commit_list.sort(key=lambda x: x["date"] or date.min, reverse=True)

    hours = Counter()
    for it in items:
        st = it["meta"].get("start_time", "")
        if st:
            try:
                h = datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone().hour
                hours[h] += 1
            except Exception:
                pass

    bash = tool_counter.get("Bash", 0)
    read = tool_counter.get("Read", 0)
    ratio = bash / read if read > 0 else 0
    avg_msgs = total_user_msgs / max(n, 1)

    # 空白期
    all_dates = sorted(set(it["date"] for it in items if it["date"]))
    gaps = []
    for i in range(1, len(all_dates)):
        gap = (all_dates[i] - all_dates[i - 1]).days - 1
        if gap >= 3:
            gaps.append((all_dates[i - 1], all_dates[i], gap))

    # 时段
    night = sum(hours.get(h, 0) for h in range(18, 24))
    midnight = sum(hours.get(h, 0) for h in range(0, 6))
    morning = sum(hours.get(h, 0) for h in range(6, 12))
    afternoon = sum(hours.get(h, 0) for h in range(12, 18))

    # 工作模式推断
    infra_heavy = sum(v for k, v in goals.items() if k in ["调试与排障", "配置与安装", "Skill 系统管理"])
    content_heavy = sum(v for k, v in goals.items() if k in ["内容创作", "代码与实现"])

    work_mode_desc = ""
    if infra_heavy > content_heavy * 1.5:
        work_mode_desc = f"你是<strong>基础设施型工作者</strong>。{total_dur//60} 小时里，主要精力花在调试 MCP、配置代理、管理 skill 系统这些「修工具」的事情上，真正落到代码/内容产出的比例偏低。"
    elif content_heavy > infra_heavy:
        work_mode_desc = "你是<strong>产出型工作者</strong>。时间主要花在内容创作和代码实现上。"
    else:
        work_mode_desc = "你是<strong>混合型工作者</strong>。基础设施和内容产出各占一定比例。"

    interact_desc = ""
    if ratio > 3:
        interact_desc = f"你倾向于让 Claude 用 shell 命令探索，而不是直接读取文件定位问题。{bash} 次 Bash 调用 vs {read} 次 Read，比例 {ratio:.1f}:1，说明还没养成「先读再动」的习惯。"
    elif ratio > 1.5:
        interact_desc = f"Bash/Read 比例 {ratio:.1f}:1，略高于理想基线 2:1。你在探索和精准定位之间摇摆。"
    else:
        interact_desc = "Bash/Read 比例健康，已养成先读文件再动手的习惯。"

    if avg_msgs > 40:
        prompt_desc = f"平均每会话 {avg_msgs:.0f} 条消息，习惯扔简短指令然后快速修正，而不是 upfront 写清楚需求。"
    elif avg_msgs > 15:
        prompt_desc = f"平均每会话 {avg_msgs:.0f} 条消息，有来回修正但不算极端。"
    else:
        prompt_desc = f"平均每会话 {avg_msgs:.0f} 条消息，需求传达比较清晰。"

    time_desc = ""
    if night + midnight > n * 0.6:
        time_desc = f"{night} 个会话在晚上，{midnight} 个在凌晨，合计占 {(night+midnight)/max(n,1)*100:.0f}%。深夜工作注意力集中但认知资源有限。"
    elif morning > n * 0.4:
        time_desc = "上午会话占比高，这个时段认知状态最好。"
    else:
        time_desc = "时间分布比较分散，没有明显的时段偏好。"

    # 坏习惯
    habits = []
    if frictions.get("misunderstood_request", 0) >= 5:
        habits.append(("提示过短", frictions["misunderstood_request"], "你扔的 prompt 太短，Claude 只能猜。下次多写一句背景或约束。"))
    if frictions.get("wrong_approach", 0) >= 5:
        habits.append(("方向把控弱", frictions["wrong_approach"], "Claude 跑偏了你才发现。开头就声明「快诊还是深挖」，或要求「先给方案不动手」。"))
    if bash > 0 and read > 0 and bash / read > 2:
        habits.append(("Bash 依赖", int(bash/read), "能用 Read/Grep 的事你让 Claude 跑 Bash。上下文膨胀的元凶。"))
    if interruptions > 5:
        habits.append(("频繁打断", interruptions, "你中途打断 Claude 的次数很多。打断前先问自己：是 Claude 跑偏了，还是我没说清？"))
    if total_commits == 0 and total_dur > 600:
        habits.append(("只探索不落地", total_dur // 60, "小时里没有 commit。你在修工具，不在做产品。"))
    elif total_commits < 5 and total_dur > 1200:
        habits.append(("产出过低", total_commits, f"{total_dur//60} 小时只有 {total_commits}  个 commit。调试时间占比太高。"))
    if avg_msgs > 50:
        habits.append(("消息密度过高", int(avg_msgs), "每会话消息太多，说明你在用注意力补 prompt 的不足。"))
    habits.sort(key=lambda x: x[1], reverse=True)

    # Features to Try（基于数据生成 CLAUDE.md 建议）
    features = []
    if bash > 0 and read > 0 and bash / read > 2:
        features.append("诊断前先用 Read/Grep，Bash 最多 3 次")
    if frictions.get("misunderstood_request", 0) >= 5 or frictions.get("wrong_approach", 0) >= 5:
        features.append("开会话第一句给出 scope 约束：「快诊」还是「深挖」")
    if interruptions > 5:
        features.append("任何 Write/Edit 前先汇报计划，等确认再执行")
    if avg_msgs > 40:
        features.append("开新会话前先写 3 行需求草稿，减少来回修正")
    if total_commits < 5 and total_dur > 1200:
        features.append("每次会话前决定：今天要产出什么，超时未产出就止损")
    if not features:
        features.append("继续保持当前节奏，下次回顾时对比趋势变化")

    # New Ways（改进建议）
    new_ways = []
    if bash > 0 and read > 0 and bash / read > 2:
        new_ways.append("派 sub-agent 做探索任务，主上下文只看摘要 —— 避免 Bash 探索吃掉你的 context window。")
    if frictions.get("misunderstood_request", 0) >= 5:
        new_ways.append("用 [快诊] / [深挖] 标签开头 —— 让 Claude 在 2 个工具调用内给出假设，不深入。")
    if interruptions > 5:
        new_ways.append("写一个 /scope 自定义命令 —— 一键声明「今天只做 X，不改 Y」。")
    if total_commits < 5 and total_dur > 1200:
        new_ways.append("设置番茄钟：25 分钟探索 + 5 分钟整理并 commit —— 强制落地节奏。")
    if not new_ways:
        new_ways.append("数据上没有明显问题，保持当前工作流即可。")

    # On the Horizon
    horizon = []
    if ratio > 2:
        horizon.append(f"目标：把 Bash/Read 比从 {ratio:.1f}:1 压到 2:1 以下。每减少 0.5，效率提升约 15%。")
    if total_commits < 5 and total_dur > 1200:
        horizon.append(f"目标：把 commit 率从 {total_dur//max(total_commits,1) if total_commits else '∞'} 分钟/commit 降到 120 以下。")
    if avg_msgs > 30:
        horizon.append("目标：把平均每会话消息数压到 20 条以下。这意味着你 upfront 的需求描述质量在提升。")
    if not horizon:
        horizon.append("当前节奏健康。下阶段关注：跨项目知识复用、skill 自动化率提升。")

    # 条形图 helper
    def bar_html(label, value, max_val, color="blue"):
        pct = (value / max(max_val, 1)) * 100
        color_cls = {"blue": "bar-blue", "red": "bar-red", "green": "bar-green"}.get(color, "bar-blue")
        return f'<div class="bar-row"><span class="bar-label">{label}</span><div class="bar-track"><div class="bar-fill {color_cls}" style="width:{pct:.1f}%"></div></div><span class="bar-val">{value}</span></div>'

    # 通用 drill helper：渲染会话列表
    def drill_session_list(sessions, limit=30):
        if not sessions:
            return ""
        rows = ""
        for entry in sessions[:limit]:
            dt = entry[0]
            goal = entry[1]
            sid = entry[2]
            d_str = dt.strftime("%m-%d") if dt else ""
            short_goal = goal[:80] + ("…" if len(goal) > 80 else "") if goal else "(无目标)"
            rows += f'<div class="drill-row"><span class="drill-date">{d_str}</span><span class="drill-sid">{sid}</span><span>{short_goal}</span></div>'
        more = f'<div class="drill-more">共 {len(sessions)} 个会话</div>' if len(sessions) > limit else ""
        return f'<div class="drill-list">{rows}{more}</div>'

    # 工作方向（带 drill）
    goals_html = ""
    max_g = max(goals.values()) if goals else 1
    for k, v in goals.most_common(8):
        sessions = goal_sessions.get(k, [])
        pct = (v / max(max_g, 1)) * 100
        bar_summary = f'<div class="bar-row"><span class="bar-label">{k}</span><div class="bar-track"><div class="bar-fill bar-blue" style="width:{pct:.1f}%"></div></div><span class="bar-val">{v}</span></div>'
        if sessions:
            goals_html += f'<details class="bar-drill"><summary>{bar_summary}</summary>{drill_session_list(sessions)}</details>'
        else:
            goals_html += bar_summary

    tools_html = ""
    max_t = max(tool_counter.values()) if tool_counter else 1
    for k, v in tool_counter.most_common(10):
        tools_html += bar_html(k, v, max_t)

    fric_html = ""
    max_f = max(frictions.values()) if frictions else 1
    for k, v in frictions.most_common(10):
        label = FRICTION_MAP.get(k, k)
        fric_html += bar_html(label, v, max_f, "red")

    # 时段（带 drill）
    segs = [("凌晨", midnight), ("上午", morning), ("下午", afternoon), ("晚上", night)]
    max_s = max(v for _, v in segs) if any(v for _, v in segs) else 1
    seg_html = ""
    for label, v in segs:
        sessions = hour_segment_sessions.get(label, [])
        pct = (v / max(max_s, 1)) * 100
        bar_summary = f'<div class="bar-row"><span class="bar-label">{label}</span><div class="bar-track"><div class="bar-fill bar-blue" style="width:{pct:.1f}%"></div></div><span class="bar-val">{v}</span></div>'
        if sessions:
            seg_html += f'<details class="bar-drill"><summary>{bar_summary}</summary>{drill_session_list(sessions, limit=20)}</details>'
        else:
            seg_html += bar_summary

    # 会话类型（带 drill）
    sess_type_html = ""
    for k, v in session_types.most_common():
        label = SESSION_TYPE_MAP.get(k, k)
        sessions = session_type_sessions.get(k, [])
        row_summary = f'<div class="stat-row"><span>{label}</span><span class="stat-num">{v}</span></div>'
        if sessions:
            sess_type_html += f'<details class="stat-drill"><summary>{row_summary}</summary>{drill_session_list(sessions, limit=20)}</details>'
        else:
            sess_type_html += row_summary

    # 达成度（带 drill）
    outcome_html = ""
    for k, v in outcomes.most_common():
        label = OUTCOME_MAP.get(k, k)
        sessions = outcome_sessions.get(k, [])
        row_summary = f'<div class="stat-row"><span>{label}</span><span class="stat-num">{v}</span></div>'
        if sessions:
            outcome_html += f'<details class="stat-drill"><summary>{row_summary}</summary>{drill_session_list(sessions, limit=20)}</details>'
        else:
            outcome_html += row_summary

    # 高满意度会话
    high_sat = []
    for it in items:
        f = it["facet"]
        counts = f.get("user_satisfaction_counts", {})
        total_s = sum(counts.values())
        good = counts.get("satisfied", 0) + counts.get("happy", 0) + counts.get("likely_satisfied", 0)
        if total_s > 0 and good / total_s >= 0.8:
            goal = f.get("underlying_goal", "")
            high_sat.append((it["date"], translations.get(goal, goal)))

    wins_html = ""
    if high_sat:
        for dt, goal in high_sat[:5]:
            d_str = dt.strftime("%m-%d") if dt else ""
            wins_html += f'<div class="win-item"><span class="win-date">{d_str}</span><span>{goal}</span></div>'

    # 做得好的地方：每个成功因素可展开查看具体会话
    successes_html = ""
    for k, v in successes.most_common(6):
        if k == "none" and v <= 1:
            continue
        label = SUCCESS_MAP.get(k, k)
        sessions = success_sessions.get(k, [])
        if sessions:
            session_rows = ""
            for dt, goal, sid in sessions[:30]:
                d_str = dt.strftime("%m-%d") if dt else ""
                short_goal = goal[:80] + ("…" if len(goal) > 80 else "")
                session_rows += f'<div class="drill-row"><span class="drill-date">{d_str}</span><span class="drill-sid">{sid}</span><span>{short_goal}</span></div>'
            more_note = f'<div class="drill-more">共 {len(sessions)} 个会话</div>' if len(sessions) > 30 else ""
            successes_html += f'<details class="success-block"><summary><strong>{label}</strong>（{v} 个会话）<span class="hint">点击展开</span></summary><div class="drill-list">{session_rows}{more_note}</div></details>'
        else:
            successes_html += f'<div class="success-flat"><strong>{label}</strong>（{v} 个会话）</div>'

    # 哪里出了问题：每个摩擦类型可展开查看具体会话
    frictions_drill_html = ""
    max_f = max(frictions.values()) if frictions else 1
    for k, v in frictions.most_common(10):
        label = FRICTION_MAP.get(k, k)
        sessions = friction_sessions.get(k, [])
        bar_pct = (v / max(max_f, 1)) * 100
        bar = f'<div class="bar-track" style="flex:1;height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden;"><div class="bar-fill bar-red" style="width:{bar_pct:.1f}%;height:100%;"></div></div>'
        if sessions:
            session_rows = ""
            for dt, goal, fr_detail, sid in sessions[:20]:
                d_str = dt.strftime("%m-%d") if dt else ""
                short_goal = goal[:60] + ("…" if len(goal) > 60 else "")
                short_fr = fr_detail[:200] + ("…" if len(fr_detail) > 200 else "")
                detail_part = f'<div class="drill-detail">{short_fr}</div>' if short_fr else ""
                session_rows += f'<div class="drill-row"><span class="drill-date">{d_str}</span><span class="drill-sid">{sid}</span><div style="flex:1;"><div>{short_goal}</div>{detail_part}</div></div>'
            more_note = f'<div class="drill-more">共 {len(sessions)} 个会话触发了这个摩擦</div>' if len(sessions) > 20 else ""
            frictions_drill_html += f'<details class="friction-block"><summary><div class="fric-summary-row"><span class="fric-label">{label}</span>{bar}<span class="fric-count">{v}</span></div></summary><div class="drill-list">{session_rows}{more_note}</div></details>'
        else:
            frictions_drill_html += f'<div class="fric-flat"><div class="fric-summary-row"><span class="fric-label">{label}</span>{bar}<span class="fric-count">{v}</span></div></div>'

    # 摩擦细节
    fric_detail_html = ""
    if friction_details:
        for dt, fr in friction_details[:8]:
            d_str = dt.strftime("%m-%d") if dt else ""
            fric_detail_html += f'<div class="fric-item"><span class="fric-date">{d_str}</span><span>{fr}</span></div>'
        if len(friction_details) > 8:
            # 用 details 标签实现折叠展开
            rest_html = ""
            for dt, fr in friction_details[8:]:
                d_str = dt.strftime("%m-%d") if dt else ""
                rest_html += f'<div class="fric-item"><span class="fric-date">{d_str}</span><span>{fr}</span></div>'
            fric_detail_html += f'<details class="expandable"><summary>展开剩余 {len(friction_details) - 8} 条</summary>{rest_html}</details>'

    # ── 调用 LLM 生成深度教练建议（基于证据）──
    stats_dict = {
        "first_date": first_date,
        "last_date": last_date,
        "n": n,
        "total_dur": total_dur,
        "total_user_msgs": total_user_msgs,
        "total_commits": total_commits,
        "bash": bash,
        "read": read,
        "edit": tool_counter.get("Edit", 0),
        "write": tool_counter.get("Write", 0),
        "interruptions": interruptions,
        "morning": morning,
        "afternoon": afternoon,
        "night": night,
        "midnight": midnight,
        "frictions": frictions,
        "friction_details": friction_details,
        "goals": goals,
        "outcomes": outcomes,
        "habits": habits,
    }
    deep_advice = generate_coaching_advice(
        stats_dict,
        translations,
        force_regenerate=force_regenerate_advice,
        use_external_llm=use_external_llm_advice,
    )
    insights_sections = build_insights_like_sections(session_records, stats_dict)

    # ── 自动反常检测 ──
    anomalies = detect_anomalies(items, translations)
    anomalies_html = ""
    if anomalies:
        # 按 level 排序：red 在前，yellow 中间，green 在后
        level_order = {"red": 0, "yellow": 1, "green": 2}
        anomalies_sorted = sorted(anomalies, key=lambda a: level_order.get(a["level"], 99))

        outcome_zh_map = {
            "fully_achieved": ("完全达成", "outcome-good"),
            "mostly_achieved": ("大部分达成", "outcome-ok"),
            "partially_achieved": ("部分达成", "outcome-warn"),
            "not_achieved": ("未达成", "outcome-bad"),
            "unclear_from_transcript": ("无法判断", "outcome-unknown"),
        }

        # 自动按 category 注入"怎么算的"说明
        METHOD_BY_CATEGORY = {
            "会话类型 × 达成度": "全局失败率 = 未达成会话数 / 总会话数。某类型失败率 > 全局 × 1.8 且绝对值 > 20% 时红色触发；完全达成率 > 55% 时绿色触发。",
            "时段 × 达成度": "时段定义：凌晨 0-6 点 / 上午 6-12 点 / 下午 12-18 点 / 晚上 18-24 点（按会话 start_time 本地时区分类）。某时段失败率 > 均值 × 1.5 且 > 15% 时触发。",
            "时段 × 工具使用": "把该时段所有会话的 Bash 总次数除以 Read 总次数。比例 > 5:1 时触发（健康基线 < 2:1）。",
            "工作方向 × 达成度": "工作方向是把每个会话的 goal_categories（细分类）通过 classify_goal() 函数归到 ~25 个大类之一。某大类失败率 > 均值 × 1.5 且失败 ≥ 2 次时触发。",
            "投入产出比": "某工作方向投入 ≥ 10 个会话，但这些会话里 git commit 总数 = 0。",
            "沉没成本": "单个会话时长 > 60 分钟 且 用户消息 ≥ 20 条 且 outcome 是「未达成」或「部分达成」。",
            "打断频率 × 达成度": "单个会话 user_interruptions ≥ 3 次。这类会话失败率 > 均值 × 1.3 时触发。",
            "Bash 滥用": "单个会话 Bash 调用 > 30 次 且 Read 调用 < 5 次。",
            "产出 vs 工具使用": "对比'有 commit'和'无 commit'两组会话的平均 Bash/Read 比。有 commit 组的比例 < 无 commit 组 × 0.7 时绿色触发。",
            "时长 vs 达成度": "长会话 = 时长 > 120 分钟；短会话 = 时长 5-30 分钟。长会话完全达成率 < 短会话 × 0.7 且 < 30% 时触发。",
            "「打招呼即失败」": "underlying_goal 字段包含 '你好/greet/hello/initiate/start a conversation' 等关键词的会话。50%+ 失败时触发。",
            "时段 × 摩擦类型": "期望摩擦数 = 全局该摩擦总数 × 该时段会话占比。实际 > 期望 × 1.8 且 ≥ 3 次时触发。",
        }

        for i, a in enumerate(anomalies_sorted, 1):
            samples_html = ""
            if a["samples"]:
                rows = ""
                for s in a["samples"]:
                    g = s["goal"][:80] + ("…" if len(s["goal"]) > 80 else "") if s["goal"] else "(无目标)"
                    oc_label, oc_cls = outcome_zh_map.get(s["outcome"], (s["outcome"], "outcome-unknown"))
                    badges = []
                    badges.append(f'<span class="badge badge-dur">⏱ {s["dur"]}分</span>')
                    badges.append(f'<span class="badge badge-msg">💬 {s["umsgs"]} 消息</span>')
                    if s["bash"] > 0:
                        badges.append(f'<span class="badge badge-bash">Bash {s["bash"]}</span>')
                    if s["read"] > 0:
                        badges.append(f'<span class="badge badge-read">Read {s["read"]}</span>')
                    if s["commits"] > 0:
                        badges.append(f'<span class="badge badge-commit">✅ {s["commits"]} commit</span>')
                    badges.append(f'<span class="badge {oc_cls}">{oc_label}</span>')
                    if s["frictions"]:
                        fr_text = "、".join(FRICTION_MAP.get(fr, fr) for fr in s["frictions"][:3])
                        badges.append(f'<span class="badge badge-friction">⚠ {fr_text}</span>')
                    badges_html = "".join(badges)
                    rows += f'''
<div class="drill-rich">
  <div class="drill-main"><span class="drill-date">{s["date"]}</span><span class="drill-sid">{s["sid"]}</span><span class="drill-goal">{g}</span></div>
  <div class="drill-meta">{badges_html}</div>
</div>'''
                samples_html = f'<details class="anomaly-samples"><summary>查看 {len(a["samples"])} 个相关会话（含时长、工具、结果等细节）</summary><div class="drill-rich-list">{rows}</div></details>'

            method_text = a.get("method") or METHOD_BY_CATEGORY.get(a["category"], "")
            method_html = ""
            if method_text:
                method_html = f'''<div class="anomaly-block anomaly-method">
    <span class="anomaly-label">📐 怎么算的</span>
    <span class="anomaly-text">{method_text}</span>
  </div>'''

            anomalies_html += f'''
<div class="anomaly-card anomaly-{a["level"]}">
  <div class="anomaly-header">
    <span class="anomaly-icon">{a["icon"]}</span>
    <span class="anomaly-cat">{a["category"]}</span>
    <span class="anomaly-no">#{i:02d}</span>
  </div>
  <div class="anomaly-title">{a["title"]}</div>
  <div class="anomaly-block">
    <span class="anomaly-label">📊 数据</span>
    <span class="anomaly-text">{a["evidence"]}</span>
  </div>
  <div class="anomaly-block">
    <span class="anomaly-label">💡 含义</span>
    <span class="anomaly-text">{a["meaning"]}</span>
  </div>
  {method_html}
  {samples_html}
</div>
'''

    # 深度建议 HTML
    advice_html = ""
    if deep_advice:
        for i, a in enumerate(deep_advice, 1):
            advice_html += f"""
<div class="advice-card">
  <div class="advice-rank">#{i:02d}</div>
  <div class="advice-title">{a['title']}</div>
  <div class="advice-body">
    <div class="advice-block evidence">
      <span class="advice-label">📊 证据</span>
      <div class="advice-text">{a['evidence']}</div>
    </div>
    <div class="advice-block cause">
      <span class="advice-label">🔍 根因</span>
      <div class="advice-text">{a['cause']}</div>
    </div>
    <div class="advice-block action">
      <span class="advice-label">⚡ 行动</span>
      <div class="advice-text">{a['action']}</div>
    </div>
  </div>
</div>
"""
    else:
        # fallback：用旧的规则化建议
        for feat in features:
            advice_html += f'<div class="feature-item"><div class="feature-check">☐</div><div class="feature-text">{feat}</div></div>'

    # Features to Try HTML（保留 fallback 用）
    features_html = ""
    for feat in features:
        features_html += f'<div class="feature-item"><div class="feature-check">☐</div><div class="feature-text">{feat}</div></div>'

    # New Ways HTML
    new_ways_html = ""
    for way in new_ways:
        new_ways_html += f'<li>{way}</li>'

    # Horizon HTML
    horizon_html = ""
    for h in horizon:
        horizon_html += f'<li>{h}</li>'

    # Summary HTML
    summary_html = ""
    if brief_summaries:
        rows = ""
        for dt, bs in brief_summaries[:10]:
            d_str = dt.strftime("%m-%d") if dt else ""
            rows += f'<div class="win-item"><span class="win-date">{d_str}</span><span>{bs}</span></div>'
        more = ""
        if len(brief_summaries) > 10:
            rest_rows = ""
            for dt, bs in brief_summaries[10:]:
                d_str = dt.strftime("%m-%d") if dt else ""
                rest_rows += f'<div class="win-item"><span class="win-date">{d_str}</span><span>{bs}</span></div>'
            more = f'<details class="expandable"><summary>展开剩余 {len(brief_summaries) - 10} 条</summary>{rest_rows}</details>'
        summary_html = f'<div class="section"><h2>会话摘要精选（{len(brief_summaries)} 条）</h2><div class="narrative">{rows}{more}</div></div>'

    # ── 新增：概览指标「怎么算的」展开详情 ──
    if has_jsonl:
        ov_session_details = f"""
    <details class="ov-details">
      <summary>📐 这个数怎么算的？</summary>
      <div class="method-text">
        <strong>定义：</strong>选定日期范围内有活动的 Claude Code CLI 会话数量。<br>
        <strong>数据来源：</strong>~/.claude/projects/*/*.jsonl，本报告纳入 {n} 个会话文件。<br>
        <strong>注意：</strong>这是原始 JSONL + insight-zh 中文缓存，不要求先跑官方 /insights。目标分类、摩擦点等语义字段优先用官方 facets，缺失时由 insight-zh 规则推断。
      </div>
    </details>"""
    else:
        ov_session_details = f"""
    <details class="ov-details">
      <summary>📐 这个数怎么算的？</summary>
      <div class="method-text">
        <strong>定义：</strong>被官方 /insights 命令深度分析过的 Claude Code CLI 会话数量。<br>
        <strong>数据来源：</strong>每个 facets JSON 文件对应一个会话，共找到 {n} 个 facets 文件。<br>
        <strong>注意：</strong>这不包括 Claude App（桌面端/网页端）的会话，仅限 CLI 端。
      </div>
    </details>"""
    ov_msgs_details = f"""
    <details class="ov-details">
      <summary>📐 这个数怎么算的？</summary>
      <div class="method-text">
        <strong>定义：</strong>所有会话中你实际输入的文本消息总数。<br>
        <strong>计算：</strong>从 JSONL 的 user 消息里排除 tool_result 回传、/command 包装、local-command caveat 等系统包装后累加。<br>
        <strong>不含：</strong>Claude 的回复、工具调用结果、系统注入消息。
      </div>
    </details>"""
    ov_dur_details = f"""
    <details class="ov-details">
      <summary>📐 这个数怎么算的？</summary>
      <div class="method-text">
        <strong>定义：</strong>所有会话的活跃时长估算。<br>
        <strong>计算：</strong>按 JSONL 相邻事件时间差累加，但单个空闲间隔最多按 15 分钟计入；总计约 {total_dur//60} 小时。<br>
        <strong>另有：</strong>墙钟跨度保存在 meta.elapsed_duration_minutes，用于审计跨日长会话，但不作为概览时长。
      </div>
    </details>"""
    ov_commit_details = f"""
    <details class="ov-details">
      <summary>📐 这个数怎么算的？</summary>
      <div class="method-text">
        <strong>定义：</strong>会话项目所在 Git 仓库在对应会话时间窗口内产生的 commit 总数。<br>
        <strong>计算：</strong>对每个会话的 project_path 找到 Git repo root，再用 git log --since/--until 统计 commit。<br>
        <strong>注意：</strong>不区分 Claude Code、Codex、其他 AI 或你手动提交；只要底层 repo 在窗口内有 commit 就算。
      </div>
    </details>"""
    semantic_source_details = f"""
    <details class="semantic-source-details">
      <summary>语义来源</summary>
      <div class="method-text">
        <strong>事实指标：</strong>会话数、消息数、时长、工具调用、commit 均来自本地 JSONL 与 git log。<br>
        <strong>语义字段：</strong>{semantic_sources.get("official insights", 0)} 个会话来自官方 /insights facets，{semantic_sources.get("insight-zh LLM", 0)} 个会话来自 insight-zh LLM，{semantic_sources.get("insight-zh heuristic", 0)} 个会话来自 insight-zh 规则推断。<br>
        <strong>说明：</strong>语义字段包括目标、摘要、达成度、摩擦点、成功因素和工作流聚合；规则推断可用但深度低于逐会话 LLM 或官方 facets。
      </div>
    </details>"""

    # ── 新增：时间分布图 ──
    timeline_html = ""
    if date_distribution:
        sorted_dates = sorted(date_distribution.keys())
        max_dur = max(v["dur"] for v in date_distribution.values()) if date_distribution else 1
        max_count = max(v["count"] for v in date_distribution.values()) if date_distribution else 1
        timeline_rows = ""
        # 最多显示 30 天，超出则按周聚合
        if len(sorted_dates) <= 30:
            for d in sorted_dates:
                v = date_distribution[d]
                pct = (v["dur"] / max(max_dur, 1)) * 100
                d_str = d.strftime("%m-%d") if hasattr(d, "strftime") else str(d)[5:]
                timeline_rows += f'<div class="timeline-row"><span class="timeline-date">{d_str}</span><div class="timeline-bar-track"><div class="timeline-bar-fill" style="width:{pct:.1f}%"></div></div><span class="timeline-val">{v["count"]}会话/{v["dur"]}分</span></div>'
        else:
            # 按周聚合
            week_data = {}
            for d in sorted_dates:
                # 获取该周周一
                if hasattr(d, "weekday"):
                    monday = d - timedelta(days=d.weekday())
                    week_key = monday
                else:
                    week_key = d
                if week_key not in week_data:
                    week_data[week_key] = {"count": 0, "dur": 0}
                week_data[week_key]["count"] += date_distribution[d]["count"]
                week_data[week_key]["dur"] += date_distribution[d]["dur"]
            sorted_weeks = sorted(week_data.keys())
            max_week_dur = max(v["dur"] for v in week_data.values()) if week_data else 1
            for w in sorted_weeks:
                v = week_data[w]
                pct = (v["dur"] / max(max_week_dur, 1)) * 100
                w_str = w.strftime("%m-%d") if hasattr(w, "strftime") else str(w)[5:]
                timeline_rows += f'<div class="timeline-row"><span class="timeline-date">{w_str}</span><div class="timeline-bar-track"><div class="timeline-bar-fill" style="width:{pct:.1f}%"></div></div><span class="timeline-val">{v["count"]}会话/{v["dur"]}分</span></div>'
        timeline_html = f'''
        <div class="timeline-chart">
          <h4>📅 每日会话时长分布（按日期）</h4>
          {timeline_rows}
        </div>'''

    # ── 新增：commit 详情列表 ──
    commit_details_html = ""
    if commit_list:
        commit_rows = ""
        for c in commit_list[:20]:
            d_str = c["date"].strftime("%m-%d") if c["date"] and hasattr(c["date"], "strftime") else ""
            goal_short = c["goal"][:60] + ("…" if len(c["goal"]) > 60 else "") if c["goal"] else "(无目标)"
            commit_rows += f'<div class="drill-row"><span class="drill-date">{d_str}</span><span class="drill-sid">{c["sid"]}</span><span>{goal_short}</span><span style="margin-left:auto;color:var(--text-dim);font-size:0.78rem;">{c["commits"]} commit · {c["dur"]}分</span></div>'
        more_commit = ""
        if len(commit_list) > 20:
            more_commit = f'<div class="drill-more">共 {len(commit_list)} 个有 commit 的会话</div>'
        commit_details_html = f'''
        <details class="expandable" style="margin-top:16px;">
                    <summary>查看 {len(commit_list)} 个有 commit 的会话详情</summary>
          <div class="drill-list">{commit_rows}{more_commit}</div>
        </details>'''

    # ── 新增：工作模式画像定义卡片 ──
    work_portrait_html = ""
    # 基础设施型 vs 产出型的判断依据
    infra_cats = ["调试与排障", "配置与安装", "Skill 系统管理"]
    content_cats = ["内容创作", "代码与实现"]
    infra_count = sum(goals.get(k, 0) for k in infra_cats)
    content_count = sum(goals.get(k, 0) for k in content_cats)
    total_goals = sum(goals.values()) if goals else 1

    work_type_method = f"""
    <div class="portrait-method">
      <strong>📐 怎么判断的：</strong>
      把每个会话的 goal_categories 通过 classify_goal() 函数归到 ~25 个大类。
      基础设施类（调试与排障 + 配置与安装 + Skill 系统管理）共 {infra_count} 次，
      产出类（内容创作 + 代码与实现）共 {content_count} 次。
      基础设施 > 产出 × 1.5 时触发「基础设施型」标签。
      当前比例：{infra_count}:{content_count}（{(infra_count/max(total_goals,1)*100):.0f}% vs {(content_count/max(total_goals,1)*100):.0f}%）
    </div>"""

    interact_method = f"""
    <div class="portrait-method">
      <strong>📐 怎么判断的：</strong>
      统计所有会话的 Bash 调用次数 ({bash}) 和 Read 调用次数 ({read})。
      比例 > 3:1 时触发「Bash 探索型」，1.5-3:1 时触发「混合探索型」，< 1.5:1 时触发「精准定位型」。
      健康基线是 < 2:1。
      当前比例：{ratio:.1f}:1。
    </div>"""

    prompt_method = f"""
    <div class="portrait-method">
      <strong>📐 怎么判断的：</strong>
      总用户消息数 ({total_user_msgs}) ÷ 总会话数 ({n}) = 平均每会话 {avg_msgs:.1f} 条消息。
      > 40 条时触发「短促迭代型」，15-40 条时触发「中等迭代型」，< 15 条时触发「精准表达型」。
    </div>"""

    time_method = f"""
    <div class="portrait-method">
      <strong>📐 怎么判断的：</strong>
      按会话 start_time 的本地小时分类：凌晨 0-6 点 / 上午 6-12 点 / 下午 12-18 点 / 晚上 18-24 点。
      晚上+凌晨 > 60% 时触发「夜猫子型」，上午 > 40% 时触发「晨型」，否则「分散型」。
      当前分布：凌晨 {midnight} / 上午 {morning} / 下午 {afternoon} / 晚上 {night}。
    </div>"""

    work_portrait_html = f"""
    <div class="portrait-card">
      <div class="portrait-label">工作类型画像</div>
      <div class="portrait-text">{work_mode_desc}</div>
      {work_type_method}
    </div>
    <div class="portrait-card">
      <div class="portrait-label">交互风格画像</div>
      <div class="portrait-text">{interact_desc}</div>
      {interact_method}
    </div>
    <div class="portrait-card">
      <div class="portrait-label">提示风格画像</div>
      <div class="portrait-text">{prompt_desc}</div>
      {prompt_method}
    </div>
    <div class="portrait-card">
      <div class="portrait-label">时间模式画像</div>
      <div class="portrait-text">{time_desc}</div>
      {time_method}
    </div>
    """

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code 洞察报告 — {first_date} 至 {last_date}</title>
<style>
  :root {{
    --bg: #f8fafc;
    --card: #ffffff;
    --text: #1e293b;
    --text-dim: #64748b;
    --accent: #3b82f6;
    --accent-light: #60a5fa;
    --danger: #ef4444;
    --danger-bg: #fef2f2;
    --success: #22c55e;
    --warning: #f59e0b;
    --border: #e2e8f0;
    --border-strong: #cbd5e1;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    line-height: 1.7;
    padding: 48px 24px;
  }}
  .container {{ max-width: 800px; margin: 0 auto; }}

  h1 {{ font-size: 2rem; font-weight: 700; margin-bottom: 6px; color: #0f172a; }}
  .subtitle {{ color: var(--text-dim); font-size: 0.95rem; margin-bottom: 32px; }}

  /* 导航 */
  .nav {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 32px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }}
  .nav a {{
    color: var(--text-dim);
    text-decoration: none;
    font-size: 0.85rem;
    padding: 4px 10px;
    border-radius: 6px;
    transition: all 0.2s;
  }}
  .nav a:hover {{ color: var(--accent); background: #eff6ff; }}

  /* 概览卡片 */
  .overview-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 32px;
  }}
  .ov-card {{
    background: var(--card);
    border-radius: 12px;
    padding: 24px 16px;
    text-align: center;
    border: 1px solid var(--border);
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  .ov-num {{ font-size: 2rem; font-weight: 700; color: var(--accent); }}
  .ov-label {{ font-size: 0.85rem; color: var(--text-dim); margin-top: 4px; }}

  /* 章节 */
  .section {{ margin-bottom: 40px; }}
  .section h2 {{
    font-size: 1.4rem;
    font-weight: 600;
    margin-bottom: 16px;
    color: #0f172a;
  }}
  .section h3 {{
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-dim);
    margin: 20px 0 10px;
  }}

  /* 空白期 */
  .gap-alert {{
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 8px;
    padding: 14px 18px;
    margin-bottom: 24px;
    color: #991b1b;
    font-size: 0.9rem;
  }}
  .gap-alert div {{ margin: 3px 0; }}

  /* 定性段落 */
  .narrative {{
    background: var(--card);
    border-radius: 10px;
    padding: 20px 24px;
    border: 1px solid var(--border);
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    margin-bottom: 12px;
  }}
  .narrative p {{ margin-bottom: 10px; color: #334155; }}
  .narrative p:last-child {{ margin-bottom: 0; }}
  .narrative strong {{ color: #0f172a; }}
  .narrative-large p {{ margin-bottom: 12px; }}
  .key-insight {{
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    color: #166534;
    border-radius: 8px;
    padding: 12px 14px;
    margin-top: 12px;
    font-size: 0.92rem;
  }}
  .insight-stack {{
    display: flex;
    flex-direction: column;
    gap: 12px;
  }}
  .insight-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  .insight-card-head {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: baseline;
    margin-bottom: 8px;
  }}
  .insight-card-head strong {{
    color: #0f172a;
    font-size: 1rem;
  }}
  .insight-card-head span {{
    color: var(--text-dim);
    font-size: 0.82rem;
    white-space: nowrap;
  }}
  .insight-card p {{
    color: #334155;
    font-size: 0.94rem;
    line-height: 1.65;
  }}
  .insight-card ul {{
    margin: 10px 0 0 18px;
    color: #475569;
    font-size: 0.88rem;
  }}
  .warning-card {{
    border-left: 4px solid #f59e0b;
  }}
  .playbook-item {{
    background: #eff6ff;
    border: 1px solid #bfdbfe;
    border-radius: 8px;
    padding: 14px 16px;
  }}
  .playbook-item strong {{
    color: #1e40af;
    display: block;
    margin-bottom: 4px;
  }}
  .playbook-item p {{
    color: #334155;
    font-size: 0.92rem;
  }}

  /* 坏习惯 */
  .habit-list {{ display: flex; flex-direction: column; gap: 10px; }}
  .habit-item {{
    background: var(--danger-bg);
    border: 1px solid #fecaca;
    border-radius: 8px;
    padding: 14px 18px;
    display: flex;
    gap: 14px;
    align-items: flex-start;
  }}
  .habit-rank {{
    font-size: 0.75rem;
    font-weight: 700;
    color: var(--danger);
    background: #fee2e2;
    padding: 2px 8px;
    border-radius: 4px;
    white-space: nowrap;
  }}
  .habit-body {{ flex: 1; }}
  .habit-title {{ font-weight: 600; color: #7f1d1d; margin-bottom: 2px; }}
  .habit-desc {{ font-size: 0.9rem; color: #991b1b; }}

  /* 对比 */
  .compare-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
  }}
  .compare-row:last-child {{ border-bottom: none; }}
  .compare-label {{ color: var(--text-dim); }}
  .compare-value {{ font-weight: 600; font-size: 1.1rem; }}
  .compare-target {{ font-size: 0.85rem; color: var(--text-dim); margin-left: 12px; }}

  /* 条形图 */
  .bar-row {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 6px 0;
    font-size: 0.9rem;
  }}
  .bar-label {{ min-width: 160px; color: #475569; }}
  .bar-track {{
    flex: 1;
    height: 6px;
    background: #e2e8f0;
    border-radius: 3px;
    overflow: hidden;
  }}
  .bar-fill {{
    height: 100%;
    border-radius: 3px;
    transition: width 0.4s;
  }}
  .bar-blue {{ background: linear-gradient(90deg, #3b82f6, #60a5fa); }}
  .bar-red {{ background: linear-gradient(90deg, #ef4444, #f87171); }}
  .bar-green {{ background: linear-gradient(90deg, #22c55e, #4ade80); }}
  .bar-val {{ min-width: 36px; text-align: right; font-size: 0.85rem; color: var(--text-dim); font-variant-numeric: tabular-nums; }}

  /* 三列 */
  .three-col {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
  }}
  .col-box {{
    background: var(--card);
    border-radius: 8px;
    padding: 16px;
    border: 1px solid var(--border);
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  .stat-row {{
    display: flex;
    justify-content: space-between;
    padding: 5px 0;
    border-bottom: 1px solid #f1f5f9;
    font-size: 0.9rem;
  }}
  .stat-row:last-child {{ border-bottom: none; }}
  .stat-num {{ color: var(--text-dim); font-weight: 500; }}

  /* Features to Try */
  .feature-item {{
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
  }}
  .feature-item:last-child {{ border-bottom: none; }}
  .feature-check {{ font-size: 1.1rem; color: var(--accent); margin-top: 1px; }}
  .feature-text {{ font-size: 0.95rem; color: #334155; }}

  /* 亮点 */
  .win-item {{
    display: flex;
    gap: 12px;
    padding: 8px 0;
    border-bottom: 1px solid #f1f5f9;
    font-size: 0.9rem;
  }}
  .win-item:last-child {{ border-bottom: none; }}
  .win-date {{ color: var(--text-dim); white-space: nowrap; font-size: 0.85rem; }}

  /* 摩擦 */
  .fric-item {{
    display: flex;
    gap: 12px;
    padding: 8px 0;
    border-bottom: 1px solid #f1f5f9;
    font-size: 0.9rem;
    color: #475569;
  }}
  .fric-item:last-child {{ border-bottom: none; }}
  .fric-date {{ color: var(--text-dim); white-space: nowrap; font-size: 0.85rem; }}
  .fric-more {{ color: var(--text-dim); font-size: 0.85rem; padding: 8px 0; }}

  /* 列表 */
  .simple-list {{ padding-left: 20px; }}
  .simple-list li {{ margin: 8px 0; color: #334155; }}

  /* 折叠展开 */
  .expandable {{
    margin-top: 12px;
    padding-top: 8px;
    border-top: 1px dashed var(--border);
  }}
  .expandable summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.9rem;
    padding: 6px 0;
    user-select: none;
    list-style: none;
  }}
  .expandable summary::before {{
    content: "▶ ";
    font-size: 0.7rem;
    margin-right: 4px;
    transition: transform 0.2s;
    display: inline-block;
  }}
  .expandable[open] summary::before {{
    content: "▼ ";
  }}
  .expandable summary:hover {{ color: var(--accent-light); }}
  .expandable[open] {{ padding-bottom: 8px; }}

  /* 深度建议卡片 */
  .advice-card {{
    background: linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
    border-radius: 14px;
    padding: 24px 28px;
    margin-bottom: 16px;
    border: 1px solid var(--border);
    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
    position: relative;
    overflow: hidden;
  }}
  .advice-card::before {{
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    width: 4px;
    height: 100%;
    background: linear-gradient(180deg, var(--accent), var(--accent-light));
  }}
  .advice-rank {{
    font-family: 'SF Mono', Monaco, monospace;
    font-size: 0.75rem;
    color: var(--text-dim);
    letter-spacing: 1.5px;
    margin-bottom: 8px;
    font-weight: 600;
  }}
  .advice-title {{
    font-size: 1.15rem;
    font-weight: 600;
    color: #0f172a;
    margin-bottom: 16px;
    line-height: 1.4;
  }}
  .advice-body {{
    display: flex;
    flex-direction: column;
    gap: 14px;
  }}
  .advice-block {{
    display: flex;
    gap: 12px;
    align-items: flex-start;
  }}
  .advice-label {{
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--text-dim);
    min-width: 60px;
    padding-top: 1px;
    letter-spacing: 0.3px;
  }}
  .advice-text {{
    flex: 1;
    color: #334155;
    font-size: 0.93rem;
    line-height: 1.6;
  }}
  .advice-block.evidence .advice-text {{ color: #1e40af; }}
  .advice-block.cause .advice-text {{ color: #475569; font-style: italic; }}
  .advice-block.action .advice-text {{ color: #0f172a; font-weight: 500; }}

  /* 章节提示 */
  .section-hint {{
    color: var(--text-dim);
    font-size: 0.9rem;
    margin-bottom: 16px;
    line-height: 1.6;
    padding: 10px 14px;
    background: #f1f5f9;
    border-left: 3px solid var(--accent-light);
    border-radius: 4px;
  }}

  /* drill-down 区块 */
  .drill-section {{ display: flex; flex-direction: column; gap: 8px; }}
  .success-block, .friction-block {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }}
  .success-block summary, .friction-block summary {{
    cursor: pointer;
    padding: 12px 16px;
    list-style: none;
    user-select: none;
    transition: background 0.15s;
  }}
  .success-block summary:hover, .friction-block summary:hover {{
    background: #f8fafc;
  }}
  .success-block summary::before, .friction-block summary::before {{
    content: "▶";
    font-size: 0.7rem;
    color: var(--text-dim);
    margin-right: 8px;
    display: inline-block;
    transition: transform 0.2s;
  }}
  .success-block[open] summary::before, .friction-block[open] summary::before {{
    transform: rotate(90deg);
  }}
  .success-block .hint {{
    font-size: 0.78rem;
    color: var(--text-dim);
    margin-left: 12px;
  }}
  .success-flat, .fric-flat {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
  }}
  .fric-summary-row {{
    display: inline-flex;
    align-items: center;
    gap: 12px;
    width: calc(100% - 20px);
  }}
  .fric-label {{ min-width: 130px; color: #475569; font-weight: 500; }}
  .fric-count {{ min-width: 36px; text-align: right; color: var(--text-dim); font-size: 0.85rem; font-variant-numeric: tabular-nums; }}
  .drill-list {{
    padding: 4px 16px 16px;
    background: #f8fafc;
    border-top: 1px solid var(--border);
  }}
  .drill-row {{
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 8px 0;
    border-bottom: 1px solid #e2e8f0;
    font-size: 0.88rem;
    color: #334155;
  }}
  .drill-row:last-child {{ border-bottom: none; }}
  .drill-date {{
    color: var(--text-dim);
    white-space: nowrap;
    font-size: 0.82rem;
    min-width: 44px;
  }}
  .drill-sid {{
    font-family: 'SF Mono', Monaco, monospace;
    color: #94a3b8;
    font-size: 0.78rem;
    min-width: 64px;
  }}
  .drill-detail {{
    color: var(--text-dim);
    font-size: 0.82rem;
    margin-top: 3px;
    line-height: 1.5;
    padding-left: 0;
    border-left: 2px solid #e2e8f0;
    padding-left: 8px;
  }}
  .drill-more {{
    color: var(--text-dim);
    font-size: 0.8rem;
    padding-top: 8px;
    font-style: italic;
  }}

  /* 条形图带 drill */
  .bar-drill {{
    border-bottom: 1px dashed transparent;
  }}
  .bar-drill summary {{
    cursor: pointer;
    list-style: none;
    padding: 2px 0;
    border-radius: 4px;
    transition: background 0.15s;
  }}
  .bar-drill summary::-webkit-details-marker {{ display: none; }}
  .bar-drill summary:hover {{ background: #f1f5f9; }}
  .bar-drill[open] {{
    background: #f8fafc;
    border-radius: 6px;
    padding: 4px 8px;
    margin: 2px 0;
  }}
  .bar-drill[open] .drill-list {{
    background: transparent;
    padding: 4px 12px 8px;
    border-top: 1px dashed var(--border);
    margin-top: 4px;
  }}

  /* 统计行带 drill */
  .stat-drill {{ }}
  .stat-drill summary {{
    cursor: pointer;
    list-style: none;
    border-radius: 4px;
    transition: background 0.15s;
  }}
  .stat-drill summary::-webkit-details-marker {{ display: none; }}
  .stat-drill summary:hover .stat-row {{ background: #f8fafc; }}
  .stat-drill[open] .stat-row {{ background: #f1f5f9; }}
  .stat-drill[open] .drill-list {{
    background: transparent;
    padding: 4px 0 8px;
    border-top: 1px dashed var(--border);
    margin-top: 2px;
  }}

  /* 反常信号卡片 */
  .anomaly-card {{
    background: var(--card);
    border-radius: 12px;
    padding: 18px 22px;
    margin-bottom: 12px;
    border: 1px solid var(--border);
    box-shadow: 0 1px 4px rgba(15, 23, 42, 0.04);
    border-left: 5px solid #94a3b8;
  }}
  .anomaly-red {{ border-left-color: #ef4444; background: linear-gradient(135deg, #fff 0%, #fef2f2 100%); }}
  .anomaly-yellow {{ border-left-color: #f59e0b; background: linear-gradient(135deg, #fff 0%, #fffbeb 100%); }}
  .anomaly-green {{ border-left-color: #22c55e; background: linear-gradient(135deg, #fff 0%, #f0fdf4 100%); }}
  .anomaly-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 6px;
    font-size: 0.78rem;
  }}
  .anomaly-icon {{ font-size: 1rem; }}
  .anomaly-cat {{
    color: var(--text-dim);
    font-weight: 500;
    letter-spacing: 0.3px;
    text-transform: uppercase;
    font-size: 0.72rem;
  }}
  .anomaly-no {{
    margin-left: auto;
    font-family: 'SF Mono', Monaco, monospace;
    color: var(--text-dim);
    font-size: 0.72rem;
    letter-spacing: 1px;
  }}
  .anomaly-title {{
    font-size: 1.05rem;
    font-weight: 600;
    color: #0f172a;
    margin-bottom: 12px;
    line-height: 1.5;
  }}
  .anomaly-block {{
    display: flex;
    gap: 10px;
    margin-top: 8px;
    align-items: flex-start;
    font-size: 0.9rem;
    line-height: 1.55;
  }}
  .anomaly-label {{
    font-size: 0.75rem;
    color: var(--text-dim);
    font-weight: 600;
    min-width: 56px;
    padding-top: 1px;
    letter-spacing: 0.3px;
  }}
  .anomaly-text {{ flex: 1; color: #334155; }}
  .anomaly-samples {{
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px dashed var(--border);
  }}
  .anomaly-samples summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.85rem;
    list-style: none;
  }}
  .anomaly-samples summary::-webkit-details-marker {{ display: none; }}
  .anomaly-samples summary::before {{
    content: "▶ ";
    font-size: 0.7rem;
    margin-right: 4px;
  }}
  .anomaly-samples[open] summary::before {{ content: "▼ "; }}
  .anomaly-summary-count {{
    text-align: right;
    font-size: 0.85rem;
    color: var(--text-dim);
    margin-bottom: 16px;
  }}

  /* badge 标签 */
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 0.74rem;
    font-weight: 500;
    margin-right: 6px;
    margin-bottom: 4px;
    background: #f1f5f9;
    color: #475569;
    border: 1px solid #e2e8f0;
    white-space: nowrap;
  }}
  .badge-dur {{ background: #fef3c7; color: #92400e; border-color: #fde68a; }}
  .badge-msg {{ background: #e0e7ff; color: #3730a3; border-color: #c7d2fe; }}
  .badge-bash {{ background: #fee2e2; color: #991b1b; border-color: #fecaca; }}
  .badge-read {{ background: #dbeafe; color: #1e40af; border-color: #bfdbfe; }}
  .badge-commit {{ background: #dcfce7; color: #166534; border-color: #bbf7d0; }}
  .badge-friction {{ background: #fef2f2; color: #991b1b; border-color: #fecaca; }}
  .outcome-good {{ background: #dcfce7; color: #166534; border-color: #bbf7d0; }}
  .outcome-ok {{ background: #d1fae5; color: #065f46; border-color: #a7f3d0; }}
  .outcome-warn {{ background: #fef3c7; color: #92400e; border-color: #fde68a; }}
  .outcome-bad {{ background: #fee2e2; color: #991b1b; border-color: #fecaca; }}
  .outcome-unknown {{ background: #f1f5f9; color: #64748b; border-color: #e2e8f0; }}

  /* 富 drill 行 */
  .drill-rich-list {{
    padding: 8px 16px 12px;
    background: #f8fafc;
    border-top: 1px solid var(--border);
  }}
  .drill-rich {{
    padding: 10px 0;
    border-bottom: 1px solid #e2e8f0;
  }}
  .drill-rich:last-child {{ border-bottom: none; }}
  .drill-rich .drill-main {{
    display: flex;
    align-items: flex-start;
    gap: 10px;
    margin-bottom: 6px;
  }}
  .drill-rich .drill-goal {{
    flex: 1;
    font-size: 0.92rem;
    color: #1e293b;
    font-weight: 500;
  }}
  .drill-rich .drill-meta {{
    padding-left: 56px;
  }}
  .anomaly-method {{
    background: rgba(59, 130, 246, 0.04);
    border-radius: 6px;
    padding: 8px 12px;
    margin-top: 6px;
  }}
  .anomaly-method .anomaly-text {{
    color: #475569;
    font-size: 0.85rem;
    font-style: italic;
  }}

  /* KPI 注释 */
  .ov-card {{ position: relative; cursor: pointer; }}
  .ov-tooltip {{
    display: block;
    margin-top: 6px;
    font-size: 0.7rem;
    color: var(--text-dim);
    line-height: 1.4;
    padding-top: 6px;
    border-top: 1px dashed var(--border);
  }}

  /* 概览卡片展开详情 */
  .ov-details {{
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px dashed var(--border);
    text-align: left;
  }}
  .ov-details summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.8rem;
    list-style: none;
    user-select: none;
  }}
  .ov-details summary::-webkit-details-marker {{ display: none; }}
  .ov-details summary::before {{
    content: "▶ ";
    font-size: 0.7rem;
    margin-right: 2px;
  }}
  .ov-details[open] summary::before {{ content: "▼ "; }}
  .ov-details .method-text {{
    font-size: 0.78rem;
    color: #475569;
    line-height: 1.6;
    margin-top: 8px;
    padding: 8px 10px;
    background: #f8fafc;
    border-radius: 6px;
  }}
  .semantic-source-details {{
    margin-top: 16px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 16px;
  }}
  .semantic-source-details summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.9rem;
    font-weight: 600;
    list-style: none;
    user-select: none;
  }}
  .semantic-source-details summary::-webkit-details-marker {{ display: none; }}
  .semantic-source-details summary::before {{
    content: "▶ ";
    font-size: 0.7rem;
    margin-right: 4px;
  }}
  .semantic-source-details[open] summary::before {{ content: "▼ "; }}
  .semantic-source-details .method-text {{
    font-size: 0.82rem;
    color: #475569;
    line-height: 1.6;
    margin-top: 8px;
    padding: 8px 10px;
    background: #f8fafc;
    border-radius: 6px;
  }}

  /* 时间分布图 */
  .timeline-chart {{
    margin-top: 16px;
    padding: 16px;
    background: var(--card);
    border-radius: 10px;
    border: 1px solid var(--border);
  }}
  .timeline-chart h4 {{
    font-size: 0.9rem;
    color: var(--text-dim);
    margin-bottom: 12px;
    font-weight: 600;
  }}
  .timeline-row {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 4px 0;
    font-size: 0.82rem;
  }}
  .timeline-date {{
    min-width: 60px;
    color: var(--text-dim);
    font-size: 0.78rem;
  }}
  .timeline-bar-track {{
    flex: 1;
    height: 8px;
    background: #e2e8f0;
    border-radius: 4px;
    overflow: hidden;
  }}
  .timeline-bar-fill {{
    height: 100%;
    border-radius: 4px;
    background: linear-gradient(90deg, #3b82f6, #60a5fa);
  }}
  .timeline-val {{
    min-width: 40px;
    text-align: right;
    color: var(--text-dim);
    font-size: 0.78rem;
  }}

  /* 工作模式画像定义卡片 */
  .portrait-card {{
    background: var(--card);
    border-radius: 10px;
    padding: 16px 20px;
    border: 1px solid var(--border);
    margin-bottom: 10px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  .portrait-label {{
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--accent);
    margin-bottom: 4px;
    letter-spacing: 0.3px;
  }}
  .portrait-text {{
    font-size: 0.95rem;
    color: #334155;
    line-height: 1.6;
  }}
  .portrait-method {{
    margin-top: 8px;
    padding: 8px 12px;
    background: #f8fafc;
    border-radius: 6px;
    font-size: 0.8rem;
    color: #64748b;
    line-height: 1.5;
    border-left: 3px solid var(--accent-light);
  }}

  /* 饼图容器 */
  .pie-chart {{
    width: 120px;
    height: 120px;
    border-radius: 50%;
    background: conic-gradient(var(--colors));
    margin: 0 auto 12px;
  }}
  .pie-legend {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    justify-content: center;
    font-size: 0.78rem;
  }}
  .pie-legend-item {{
    display: flex;
    align-items: center;
    gap: 4px;
  }}
  .pie-dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
  }}

  /* 底部 */
  .footer {{
    text-align: center;
    color: var(--text-dim);
    font-size: 0.8rem;
    margin-top: 48px;
    padding-top: 24px;
    border-top: 1px solid var(--border);
  }}

  @media (max-width: 700px) {{
    .overview-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .three-col {{ grid-template-columns: 1fr; }}
    .nav {{ display: none; }}
    body {{ padding: 24px 16px; }}
  }}
</style>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
</head>
<body>
<div class="container">
  <h1>Claude Code 洞察报告</h1>
  <div class="subtitle">{n} 个会话 · {first_date} 至 {last_date} · {total_user_msgs:,} 条消息 · {total_dur//60} 小时 · {total_commits}  个 commit</div>

  <div class="nav">
    <a href="#overview">概览</a>
    <a href="#themes">工作流</a>
    <a href="#behavior">画像</a>
    <a href="#anomalies">⚡ 反常信号</a>
    <a href="#friction-story">重复摩擦</a>
    <a href="#usage">使用方式</a>
    <a href="#wins">亮点</a>
    <a href="#friction">摩擦</a>
    <a href="#playbook">规则</a>
    <a href="#features">深度建议</a>
  </div>

  <!-- 概览 -->
  <div class="section" id="overview">
    <div class="overview-grid">
      <div class="ov-card"><div class="ov-num">{n}</div><div class="ov-label">会话</div><div class="ov-tooltip">选定日期范围内有活动的 Claude Code CLI 会话数</div>{ov_session_details}</div>
      <div class="ov-card"><div class="ov-num">{total_user_msgs:,}</div><div class="ov-label">消息</div><div class="ov-tooltip">这些会话中你实际输入的文本消息数，不含工具结果回传</div>{ov_msgs_details}</div>
      <div class="ov-card"><div class="ov-num">{total_dur//60}h</div><div class="ov-label">活跃时长</div><div class="ov-tooltip">按相邻事件间隔估算的活跃时长，单个空闲间隔最多计 15 分钟</div>{ov_dur_details}</div>
    <div class="ov-card"><div class="ov-num">{total_commits}</div><div class="ov-label">Commit</div><div class="ov-tooltip">这些会话项目底层 Git 仓库在对应时间窗口内产生的 git commit 总数</div>{ov_commit_details}</div>
    </div>

    {semantic_source_details}
    {timeline_html}
    {commit_details_html}

    {"<div class='gap-alert'>" + "".join(f"<div>⚠️ {s} → {e}（空白 {d} 天）</div>" for s, e, d in sorted(gaps, key=lambda x: x[2], reverse=True)[:3]) + "</div>" if gaps else ""}
  </div>

  {insights_sections["themes_html"]}
  {insights_sections["behavior_html"]}
  {painting_section_html}\n\n  <!-- 反常信号 -->
  <div class="section" id="anomalies">
    <h2>⚡ 自动发现的反常信号</h2>
    <p class="section-hint">从交叉分析中自动找出的「跟均值差异显著」的信号 — 🔴 红色 = 问题，🟡 黄色 = 警示，🟢 绿色 = 优势。每条带数据 + 含义 + 相关会话。</p>
    {f'<div class="anomaly-summary-count">检测到 <strong>{len(anomalies)}</strong> 个反常信号</div>' if anomalies else ''}
    {anomalies_html if anomalies_html else '<p style="color:var(--text-dim);">数据上没有发现显著反常 — 这是好事，说明各维度表现接近均值，没有特别突出的问题或优势。</p>'}
  </div>

  {insights_sections["friction_story_html"]}

  <!-- 你在做什么 -->
  <div class="section" id="work">
    <h2>工作方向明细</h2>
    <p class="section-hint">这里保留原始 goal_categories 的聚合明细，作为上面“工作流叙事”的可追溯证据。</p>
    {goals_html}
  </div>

  <!-- 使用方式 -->
  <div class="section" id="usage">
    <h2>你的使用方式</h2>
    <p class="section-hint">下面是基于数据自动推断的 4 个画像维度。每个画像都有「📐 怎么判断的」说明，告诉你这个标签是怎么算出来的。</p>
    {work_portrait_html}

    <h3>坏习惯 Top 3</h3>
    <div class="habit-list">
      {"".join(f'<div class="habit-item"><div class="habit-rank">#{i}</div><div class="habit-body"><div class="habit-title">{name}</div><div class="habit-desc">{desc}</div></div></div>' for i, (name, count, desc) in enumerate(habits[:3], 1))}
    </div>

    <h3>与平均用户的对比</h3>
    <div class="narrative">
      <div class="compare-row"><span class="compare-label">Bash/Read 比</span><div><span class="compare-value" style="color:{'#ef4444' if ratio > 2 else '#22c55e'}">{ratio:.1f}:1</span><span class="compare-target">理想 &lt; 2:1</span></div></div>
      <div class="compare-row"><span class="compare-label">平均每会话时长</span><div><span class="compare-value">{total_dur//max(n,1)} 分钟</span><span class="compare-target">{'偏长' if total_dur//max(n,1) > 120 else '正常'}</span></div></div>
      <div class="compare-row"><span class="compare-label">平均每会话消息</span><div><span class="compare-value">{total_user_msgs//max(n,1)} 条</span><span class="compare-target">{'偏高' if avg_msgs > 30 else '正常'}</span></div></div>
    <div class="compare-row"><span class="compare-label">Commit 率</span><div><span class="compare-value" style="color:{'#ef4444' if (total_dur//max(total_commits,1) if total_commits else 9999) > 120 else '#22c55e'}">{total_dur//max(total_commits,1) if total_commits else '∞'} 分钟/commit</span><span class="compare-target">理想 &lt; 120</span></div></div>
    </div>

    <h3>会话类型、结果、时段</h3>
    <div class="three-col">
      <div class="col-box">
        <div style="font-weight:600;margin-bottom:8px;font-size:0.9rem;color:var(--text-dim);">会话类型</div>
        {sess_type_html}
      </div>
      <div class="col-box">
        <div style="font-weight:600;margin-bottom:8px;font-size:0.9rem;color:var(--text-dim);">结果达成度</div>
        {outcome_html}
      </div>
      <div class="col-box">
        <div style="font-weight:600;margin-bottom:8px;font-size:0.9rem;color:var(--text-dim);">时段分布</div>
        {seg_html}
      </div>
    </div>
  </div>

  <!-- 亮点 -->
  <div class="section" id="wins">
    <h2>做得好的地方</h2>
    <p class="section-hint">下面每条是 Claude 在你会话中表现好的「主要成功因素」。点击展开看具体是哪些会话触发的。</p>
    <div class="drill-section">
      {successes_html}
    </div>
    {"<h3 style='margin-top:20px;'>高满意度会话（≥80% 满意）</h3>" + "<div class=\"narrative\">" + wins_html + "</div>" if wins_html else ""}
  </div>

  <!-- 摩擦 -->
  <div class="section" id="friction">
    <h2>哪里出了问题</h2>
    <p class="section-hint">下面每条是 Claude 在你会话中触发的「摩擦类型」。点击展开看具体哪些会话遇到这个问题、问题细节是什么。</p>
    <div class="drill-section">
      {frictions_drill_html}
    </div>
  </div>

  {insights_sections["playbook_html"]}

  <!-- 深度教练建议 -->
  <div class="section" id="features">
    <h2>深度建议</h2>
    <p style="color:var(--text-dim);margin-bottom:20px;font-size:0.95rem;">基于你的数据，从摩擦案例和行为模式里看出的卡点 — 每条都有证据、根因、可执行的行动。</p>
    {advice_html}
  </div>

  <!-- 工具使用 -->
  <div class="section">
    <h2>工具使用 TOP 10</h2>
    {tools_html}
  </div>

  <!-- 会话摘要 -->
  {summary_html}

  <div class="footer">
    生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} · 数据来源：{data_source_html}
  </div>
</div>
</body>
</html>"""

    return html


def main():
    args = parse_args()
    if args.help:
        print(__doc__)
        return

    start_d, end_d = resolve_range(args)

    # 优先尝试 JSONL 新数据源，无数据则回退到 facets
    items = load_data_from_jsonl(start_d, end_d)
    source_label = "jsonl"
    if not items:
        items = load_data(start_d, end_d)
        source_label = "facets"

    translations = {}
    if args.translate and not args.no_translate and items and source_label == "facets":
        print(f"收集到 {len(items)} 个会话的 facets 数据，准备翻译...", file=sys.stderr)
        texts = collect_texts_to_translate(items)
        print(f"需要翻译的 unique 文本：{len(texts)} 条", file=sys.stderr)
        if texts:
            translations = translate_batch(texts)
            print(f"翻译完成", file=sys.stderr)

    if args.html:
        report = generate_html_report(
            items,
            translations,
            force_regenerate_advice=args.regen_advice,
            use_external_llm_advice=args.llm_advice,
        )
        path = REPORTS_DIR / f"{report_basename(start_d, end_d)}.html"
        path.write_text(report, encoding="utf-8")
        print(f"HTML 报告已保存：{path}")
        try:
            subprocess.run(["open", str(path)], check=False)
        except Exception:
            pass
        return

    report = generate_report(items, translations)

    if args.print_only:
        print(report)
        return

    path = REPORTS_DIR / f"{report_basename(start_d, end_d)}.md"
    path.write_text(report, encoding="utf-8")
    print(f"报告已保存：{path}")

    try:
        subprocess.run(["open", str(path)], check=False)
    except Exception:
        pass


if __name__ == "__main__":
    main()
