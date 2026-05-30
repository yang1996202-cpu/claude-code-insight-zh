from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class ThemeRule:
    name: str
    big_categories: Tuple[str, ...] = ()
    raw_keywords: Tuple[str, ...] = ()
    text_keywords: Tuple[str, ...] = ()
    reason_template: str = ""


@dataclass(frozen=True)
class ReportLens:
    """Controls the report's analytical direction.

    Facts and semantic facets are upstream data. The lens decides which patterns
    the report treats as important, how sessions are grouped, and how the final
    narrative is framed.
    """

    lens_id: str
    name: str
    description: str
    themes_title: str
    themes_hint: str
    behavior_title: str
    behavior_key_insight: str
    friction_title: str
    friction_hint: str
    playbook_title: str
    work_detail_title: str
    work_detail_hint: str
    theme_rules: Tuple[ThemeRule, ...]
    fallback_theme: str
    playbook_rules: Dict[str, Tuple[str, str]]


DEFAULT_REPORT_LENS = ReportLens(
    lens_id="workflow_behavior",
    name="Workflow Behavior Lens",
    description=(
        "Group sessions by recurring workflows, then explain behavior patterns, "
        "repeated friction, and reusable collaboration rules."
    ),
    themes_title="主要工作流",
    themes_hint="这一段按语义层字段聚合多个会话，展示长期推进的工作流，而不是重复罗列工具指标。",
    behavior_title="使用方式画像",
    behavior_key_insight="核心画像：这类使用更接近“可验证、可纠错、可沉淀的执行环境”，而不是一次性聊天问答。",
    friction_title="反复出问题的地方",
    friction_hint="这一段不再只列摩擦类型，而是解释这些摩擦为什么会反复发生。",
    playbook_title="可复用的协作规则",
    work_detail_title="工作方向明细",
    work_detail_hint="这里保留原始 goal_categories 的聚合明细，作为上面“工作流叙事”的可追溯证据。",
    fallback_theme="临时问答与探索",
    theme_rules=(
        ThemeRule(
            name="AI 编程工具链与分析系统",
            big_categories=("Skill 系统管理", "系统管理", "工具探索"),
            text_keywords=("claude code", "insight", "usage-data", "facets", "skill", "缓存", "中文报告", "分析器"),
            reason_template=(
                "这类会话把 AI 编程工具当成可改造的平台：围绕报告、skill、缓存、工作流做了 "
                "{sessions} 个会话，工具调用里 Bash {bash} 次、Edit/Write {edit_write} 次，"
                "说明它不是聊天问答，而是本地系统工程。代表会话：{sample}"
            ),
        ),
        ThemeRule(
            name="内容生产与发布流水线",
            big_categories=("内容创作",),
            raw_keywords=("publish", "wechat", "article", "content", "illustration"),
            reason_template=(
                "这是可复用的内容生产线：{sessions} 个会话集中在文章、HTML、图片或发布链路，"
                "Read {read} 次、Edit/Write {edit_write} 次。重点不是一次写完，而是把每次摩擦沉淀成流水线能力。"
                "代表会话：{sample}"
            ),
        ),
        ThemeRule(
            name="应用配置与 UI 排障",
            text_keywords=("codepilot", "client", "ui", "model", "display", "sdk", "electron"),
            reason_template=(
                "这类会话集中在 UI 显示、配置文件和真实运行行为的分层排查，共 {sessions} 个。"
                "结果多为 {top_outcome}，通常卡在第三方客户端限制或显示层/运行层混淆。代表会话：{sample}"
            ),
        ),
        ThemeRule(
            name="本地环境与系统自动化",
            text_keywords=("macos", "pmset", "caffeinate", "sleep", "lid", "power"),
            reason_template=(
                "这类工作是典型本机自动化：{sessions} 个会话围绕系统状态、命令环境和长期运行可靠性。"
                "Bash {bash} 次说明大部分成本在验证环境事实。代表会话：{sample}"
            ),
        ),
        ThemeRule(
            name="知识管理与外部集成",
            text_keywords=("feishu", "lark", "knowledge", "memory", "obsidian", "nowledge", "文档", "知识"),
            reason_template=(
                "这类会话在处理知识持久化、外部系统接入和可检索记录，共 {sessions} 个。"
                "它的难点不是写代码，而是权限、数据边界和工具能力是否真实可用。代表会话：{sample}"
            ),
        ),
        ThemeRule(
            name="GitHub 与开源交付",
            big_categories=("Git 操作",),
            raw_keywords=("github", "git", "repo", "commit", "push"),
            reason_template=(
                "这类会话最终要落到仓库、README、commit 或 push。共 {sessions} 个会话、{commits} 个 commit，"
                "重点是把个人问题包装成可复用资产。代表会话：{sample}"
            ),
        ),
        ThemeRule(
            name="代码修复与调试",
            big_categories=("调试与排障", "代码与实现"),
            raw_keywords=("debug", "bug", "fix"),
            reason_template=(
                "这类会话集中在 bug、测试和修复，共 {sessions} 个，Bash {bash} 次、Read {read} 次、"
                "Edit/Write {edit_write} 次。它最需要的是先定位再改，不然容易变成命令试错。代表会话：{sample}"
            ),
        ),
    ),
    playbook_rules={
        "verify_first": (
            "事实先于方案",
            "任何数量、路径、工具能力、系统状态，先用命令验证再下结论。没有验证就只能写“推测”。",
        ),
        "limit_bash": (
            "限制 Bash 探索",
            "连续 5 个 Bash 后暂停，要求 Claude 输出当前假设、已排除项、下一步要读哪个文件。",
        ),
        "commit_checkpoint": (
            "把 commit 当作检查点",
            "超过 60-90 分钟的会话必须留下 wip commit 或决策记录；后面可以 squash，但过程不能消失。",
        ),
        "opening_scope": (
            "开场补三行边界",
            "目标、不要做什么、完成标准。这样不会阻止探索，但能减少中途纠偏。",
        ),
        "watch_trends": (
            "保持趋势观察",
            "当前没有明显重复红灯，继续观察同一指标是否连续恶化三天。",
        ),
    },
)


def available_lenses() -> List[ReportLens]:
    return [DEFAULT_REPORT_LENS]


def get_report_lens(lens_id: str = "") -> ReportLens:
    if not lens_id:
        return DEFAULT_REPORT_LENS
    for lens in available_lenses():
        if lens.lens_id == lens_id:
            return lens
    known = ", ".join(lens.lens_id for lens in available_lenses())
    raise ValueError(f"Unknown report lens: {lens_id}. Available lenses: {known}")
