# Claude Code Insight 中文报告

> 像每日体检单一样，帮你发现 Claude Code 使用模式，持续改进。

从 `facets` + `session-meta` 生成中文洞察报告，替代 Claude Code 原生 `/insight` 的英文模板。支持工作模式画像、坏习惯分析、反常信号、改进建议等。

## 包含两个工具

| 工具 | 输出 | 用途 |
|------|------|------|
| `insight-zh.py` | HTML 报告 | 全量/多天洞察，可视化图表，趋势分析 |
| `di-review.py` | Markdown 日报 | 每日快速回顾，健康度指标，改进建议 |

## 功能特性

- **多维度画像**：工作模式（编码/调试/探索/文档）、时间分布、项目分布
- **坏习惯检测**：Bash/Read 比超标、消息密度过高、反复修正模式
- **反常信号**：偏离个人基线的异常行为（红色标记）
- **趋势追踪**：任意天数范围（1天/3天/7天/全量），纵向对比
- **LLM 翻译层**：自动翻译 facets 中的英文标签为中文，带缓存加速
- **HTML 可视化**：时间线图、分布图、可折叠详情、方法论说明
- **每日自动**：终端启动时若当天日报缺失则静默生成（可选）

## 安装

```bash
# 1. 克隆仓库
git clone https://github.com/yang1996202-cpu/claude-code-insight-zh.git

# 2. 安装依赖（仅 insight-zh 需要，用于 LLM 翻译）
pip install anthropic

# 3. 配置 API（可选，用于翻译 facets 标签。跳过则使用原文）
export INSIGHT_API_KEY="sk-your-key"
export INSIGHT_API_BASE="https://api.kimi.com/coding/"  # 或其他兼容 Anthropic SDK 的 API
export INSIGHT_API_MODEL="kimi-for-coding"
```

## 数据前提

本工具读取 Claude Code CLI 的本地使用数据：

```
~/.claude/usage-data/facets/        # 会话分析 facets（JSON）
~/.claude/usage-data/session-meta/  # 会话元数据（JSON）
~/.claude/projects/*/*.jsonl        # 原始会话记录
```

> 只包含 Claude Code CLI 的会话，不含 Claude App（桌面端/网页端）。

## 用法

### insight-zh（洞察报告）

```bash
# 全部历史数据，生成 HTML 并保存
python3 insight-zh.py --html --save

# 最近 N 天
python3 insight-zh.py 7 --html --save

# 只输出文本到终端（不生成文件）
python3 insight-zh.py 3 --print-only

# 跳过 LLM 翻译（更快，纯规则生成）
python3 insight-zh.py 7 --no-translate --print-only

# 指定日期范围
python3 insight-zh.py 2026-04-01 2026-05-01 --html --save
```

HTML 报告默认保存到 `~/.claude/insight-reports/YYYY-MM-DD.html`。

### di-review（每日日报）

```bash
# 生成今天的报告并打开编辑器
python3 di-review.py

# 生成指定日期
python3 di-review.py 2026-05-10

# 强制重新生成（覆盖 Claude 写的部分，保留你的反思）
python3 di-review.py --regen

# 只输出到 stdout，不写文件
python3 di-review.py --print-only

# 静默模式（适合自动脚本）
python3 di-review.py --quiet
```

日报默认保存到 `~/.claude/daily-reports/YYYY-MM-DD.md`。

### 终端自动日报（可选）

在 `~/.zshrc` 或 `~/.bashrc` 中添加：

```zsh
alias di-review='python3 /path/to/di-review.py'
alias di='${EDITOR:-open} ~/.claude/daily-reports/$(date +%Y-%m-%d).md'

# 终端启动时若今天日报缺失则后台静默生成（不阻塞 shell）
[[ ! -f ~/.claude/daily-reports/$(date +%Y-%m-%d).md ]] && \
  (python3 /path/to/di-review.py --quiet >/dev/null 2>&1 &) 2>/dev/null
```

### Claude Code Skill 模式

将 `SKILL.md` 放到 `~/.claude/skills/insight-zh/` 下，即可在 Claude Code 中使用：

```
/insight-zh          # 询问范围后执行
/insight-zh 1        # 最近 1 天
/insight-zh 3        # 最近 3 天
/insight-zh 7        # 最近 7 天
/insight-zh --text   # 纯文本输出
```

## 核心指标说明

| 指标 | 含义 | 健康基线 |
|------|------|---------|
| Bash/Read 比 | Bash 调用次数 / Read 调用次数 | < 2.0 |
| 用户消息数 | 每天发送的消息条数 | < 80 |
| 单次会话消息密度 | 消息数 / 会话数 | 过高 = 反复修正 |
| 打断次数 | 用户中途打断 AI 的次数 | 越少越好 |
| 探索率 | 探索类会话占比 | 适度即可 |

## 缓存机制

- 翻译缓存：`~/.claude/insight-reports/.translation-cache.json`
- 建议缓存：`~/.claude/insight-reports/.advice-cache-YYYY-MM-DD.json`
- 首次运行需要翻译（会调用 LLM API），后续有缓存会快很多

## 截图

<!-- 建议上传一张 HTML 报告截图到仓库，替换下方链接 -->
<!-- ![报告示例](screenshots/report-example.png) -->

## License

MIT
