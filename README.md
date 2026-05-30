# Claude Code Insight 中文报告

> 一个中文 HTML 报告入口：1 天、7 天、30 天都走同一套原始 JSONL 分析 + 中文缓存 + 可视化报告。

## 模式

| 模式 | 命令 | 看什么 |
|------|------|--------|
| 今日 | `python3 insight-zh.py 1 --html --save` | 今天相关会话、异常信号、活跃时长、工具使用 |
| 周报 | `python3 insight-zh.py 7 --html --save` | 近 7 天趋势和根因 |
| 月报 | `python3 insight-zh.py 30 --html --save` | 近 30 天长期模式 |
| 日期范围 | `python3 insight-zh.py 2026-04-01 2026-05-01 --html --save` | 任意时间窗口 |

产出统一放在：

```text
~/.claude/usage-data-zh/reports/YYYY-MM-DD.html
~/.claude/usage-data-zh/reports/YYYY-MM-DD_to_YYYY-MM-DD.html
```

## 当前结构

```text
.
├── SKILL.md
├── insight-zh.py              # 稳定入口
├── insight_zh/
│   ├── insight_cli.py         # HTML/Markdown 报告主逻辑
│   ├── analysis/              # 启发式与绘画方法论分析
│   ├── domain/                # 统一 session 模型
│   └── sources/               # JSONL / 官方 facets / 中文缓存
└── tests/
```

## 数据来源

本工具读取 Claude Code CLI 的本地使用数据：

```text
~/.claude/projects/*/*.jsonl        # 原始会话记录，主数据源
~/.claude/usage-data/               # 官方 /insights 产物，可选增强源
~/.claude/usage-data-zh/            # insight-zh 自己的中文缓存和报告
```

不读取 Claude App 网页端/桌面端会话。

## 关键口径

- **会话数**：选定日期范围内有活动的 Claude Code CLI JSONL 会话。
- **消息数**：你实际输入的文本消息数；不含 `tool_result` 回传、`/command` 包装、local-command caveat 等系统包装。
- **活跃时长**：按 JSONL 相邻事件时间差估算，单个空闲间隔最多计 15 分钟；墙钟跨度另存为 `elapsed_duration_minutes`，不再用来做概览时长。
- **语义字段**：官方 facets 有就优先用官方；没有就由 insight-zh 从 JSONL 启发式推断。
- **绘画方法论字段**：`painting_stage`、`energy_flow`、`topic_count` 等永远由 insight-zh 自己分析。

## 缓存机制

```text
~/.claude/usage-data-zh/
  session-meta/*.json
  facets/*.json
  reports/*.html
  index.json
```

缓存会记录：

- 原始 JSONL 的 path / mtime / size
- 官方 `usage-data/session-meta` 和 `usage-data/facets` 的 mtime / size
- 自动计算的 analyzer version

analyzer version 由相关分析代码内容自动 hash 生成。你改了 JSONL 解析、会话合并、绘画阶段、能量流向或摩擦判断，旧缓存会自动失效，不需要手动改目录名。

## 安装

```bash
git clone https://github.com/yang1996202-cpu/claude-code-insight-zh.git
cd claude-code-insight-zh
pip install -r requirements.txt
```

默认报告不需要 API key，也不会调用外部模型。事实统计、语义 fallback、深度建议卡片都可以本地生成。

可选：如果你明确想用外部 LLM 增强建议或翻译官方 facets 英文文本，再安装 SDK 并配置 API。

```bash
pip install "anthropic>=0.40.0"
export INSIGHT_API_KEY="sk-your-key"
export INSIGHT_API_BASE="https://api.kimi.com/coding/"
export INSIGHT_API_MODEL="kimi-for-coding"
```

使用外部 LLM 时必须显式加参数：

```bash
python3 insight-zh.py 7 --html --save --llm-advice
python3 insight-zh.py 7 --html --save --translate
```

没有这些参数时，即使环境里存在 `INSIGHT_API_KEY`，也不会调用外部模型。

## 用法

```bash
# 今日 HTML 报告
python3 insight-zh.py 1 --html --save

# 近 7 天 HTML 报告
python3 insight-zh.py 7 --html --save

# 近 30 天 HTML 报告
python3 insight-zh.py 30 --html --save

# 纯文本输出
python3 insight-zh.py 7 --print-only --no-translate
```

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
python3 insight-zh.py 1 --html --save --no-translate
```

## License

MIT
