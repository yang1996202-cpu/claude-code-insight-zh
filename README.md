# Claude Code Insight 中文报告

> 一个入口，三层模式：快速日报（秒出）+ 深度周报/月报（LLM 理解）。像体检单一样帮你发现 Claude Code 使用模式，持续改进。

## 三种模式

| 模式 | 命令 | 速度 | 引擎 | 看什么 |
|------|------|------|------|--------|
| **/daily** | `/insight-zh /daily` | 秒出 | 规则 + 关键词 | 今天哪里有问题？ |
| **/weekly** | `/insight-zh /weekly` | 几分钟 | LLM 深度理解 | 这周趋势和根因 |
| **/monthly** | `/insight-zh /monthly` | 几分钟 | LLM 深度理解 | 这个月进化了吗？ |

**daily 是体检单，weekly/monthly 是专家会诊。**

---

## 两个引擎

| 引擎 | 负责模式 | 特点 |
|------|---------|------|
| `di-review.py` | /daily | 纯规则引擎，双轨制（facet + 消息推断），不调用 LLM |
| `insight-zh.py` | /weekly /monthly | LLM 驱动，工作模式画像、坏习惯分析、反常信号、趋势追踪 |

---

## 功能特性

### /daily 日报
- **双轨制达成评估**：有 /insight facet 用 facet，没有则消息推断
- **新增指标**：/compact 次数、纯对话轮数、文件产出数、反复编辑文件数
- **精准定位**：Bash 密集区段具体到时间点、消息摩擦信号具体到某条消息
- **预填反思区**：自动给出观察和约束，你只需要补充

### /weekly /monthly 深度报告
- **工作模式画像**：编码/调试/探索/文档比例、时间分布、项目分布
- **坏习惯检测**：Bash/Read 比超标、消息密度过高、反复修正模式
- **反常信号**：偏离个人基线的异常行为（红色标记）
- **趋势追踪**：任意天数范围，纵向对比
- **LLM 翻译层**：自动翻译 facets 英文标签为中文，带缓存加速
- **HTML 可视化**：时间线图、分布图、可折叠详情

---

## 安装

```bash
# 1. 克隆仓库
git clone https://github.com/yang1996202-cpu/claude-code-insight-zh.git

# 2. 安装依赖（仅 weekly/monthly 需要，用于 LLM 翻译）
pip install anthropic

# 3. 配置 API（可选，用于翻译 facets 标签。跳过则使用原文）
export INSIGHT_API_KEY="sk-your-key"
export INSIGHT_API_BASE="https://api.kimi.com/coding/"
export INSIGHT_API_MODEL="kimi-for-coding"
```

## 数据前提

本工具读取 Claude Code CLI 的本地使用数据：

```
~/.claude/projects/*/*.jsonl        # 原始会话记录（主要数据源）
~/.claude/usage-data/facets/        # 会话分析 facets（JSON）
```

> 只包含 Claude Code CLI 的会话，不含 Claude App（桌面端/网页端）。

---

## 用法

### Claude Code Skill（推荐）

将 `SKILL.md` 放到 `~/.claude/skills/insight-zh/` 下，即可在 Claude Code 中使用：

```
/insight-zh /daily      # 快速日报，秒出
/insight-zh /weekly     # 深度周报，LLM 分析 7 天
/insight-zh /monthly    # 深度月报，LLM 分析 30 天
/insight-zh             # 无参数时询问选择
```

### 命令行直接跑

#### /daily 日报

```bash
# 今天日报（默认）
python3 di-review.py

# 指定日期
python3 di-review.py 2026-05-13

# 本周趋势
python3 di-review.py --week

# 重新生成（覆盖自动部分，保留你的反思）
python3 di-review.py --regen

# 只输出到 stdout
python3 di-review.py --print-only

# 静默模式
python3 di-review.py --quiet
```

产出：`~/.claude/daily-reports/YYYY-MM-DD.md`

#### /weekly /monthly 深度报告

```bash
# 周报（7 天）
python3 insight-zh.py 7 --html --save

# 月报（30 天）
python3 insight-zh.py 30 --html --save

# 纯文本输出
python3 insight-zh.py 7 --print-only

# 跳过翻译（更快，英文输出）
python3 insight-zh.py 7 --no-translate --print-only

# 指定日期范围
python3 insight-zh.py 2026-04-01 2026-05-01 --html --save
```

产出：`~/.claude/insight-reports/YYYY-MM-DD.html`

### 终端别名（可选）

在 `~/.zshrc` 中添加：

```zsh
alias diary='python3 /path/to/di-review.py'
alias insight='python3 /path/to/insight-zh.py'
```

---

## 核心指标说明

| 指标 | 含义 | 健康基线 |
|------|------|---------|
| Bash/Read 比 | Bash 调用次数 / Read 调用次数 | < 2.0 |
| 消息密度 | 用户消息数 / 会话数 | < 40/会话 |
| 达成率 | (完全达成 + 大部分达成) / 有评估会话 | > 70% |
| /compact 次数 | 上下文压缩次数 | 越少越好 |
| 反复编辑 | 同一文件被 Edit 2+ 次 | 开发场景合理，否则注意 |
| Bash 质量 | cat/head/tail/wc 等本可用 Read 替代的比例 | < 15% |
| 工具连发 | Claude 连续调用工具最多轮数 | < 8 |

---

## 双轨制说明

`/daily` 的达成与结果有两个数据来源：

**基于 /insight 评估**：
- 来源：你手动跑过 `/insight` 的 session 生成的 facet 文件
- 特点：LLM 定性理解，准确度高，但覆盖整个 session（跨天 session 会标注）

**基于消息推断**：
- 来源：di-review.py 分析当天消息日志
- 特点：规则引擎，从用户消息文本中推断摩擦/达成/放弃信号
- 信号词：「不对」「错了」「重来」「好了」「搞定」「算了」等

两者互补：facet 有就用 facet，没有就消息推断兜底。

---

## 缓存机制

- 翻译缓存：`~/.claude/insight-reports/.translation-cache.json`
- 建议缓存：`~/.claude/insight-reports/.advice-cache-YYYY-MM-DD.json`
- 首次运行 weekly/monthly 需要翻译（会调用 LLM API），后续有缓存会快很多

---

## License

MIT
