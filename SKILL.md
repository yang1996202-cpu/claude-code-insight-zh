---
name: insight-zh
description: |
  Claude Code 使用分析套件。一个入口，三层模式：

  /daily   — 快速日报（秒出，规则引擎，看今天哪里有问题）
  /weekly  — 深度周报（LLM 理解，看本周趋势和根因）
  /monthly — 深度月报（LLM 理解，看长期进化）

  数据同源：都基于 ~/.claude/projects/*/*.jsonl 会话数据。
  触发词：insight, 洞察, 报告, 复盘, 日报, 周报, 月报, daily, weekly
allowed-tools:
  - Bash
  - Read
---

## 用法

当前仓库根目录的 `di-review.py` 和 `insight-zh.py` 是稳定入口；真实实现已经迁入 `insight_zh/` 包内。
如果后续需要维护仓库，优先改：

- `insight_zh/daily_cli.py`
- `insight_zh/insight_cli.py`
- `insight_zh/analysis/`
- `insight_zh/sources/`

### 快速入口（推荐）

```
/insight-zh /daily     # 快速日报，秒出，显示当天指标和摩擦
/insight-zh /weekly    # 深度周报，LLM 分析 7 天数据
/insight-zh /monthly   # 深度月报，LLM 分析 30 天数据
```

### 详细参数

#### /daily 模式（di-review.py）

```bash
# 今天日报（默认）
python3 ~/projects/claude-code-insight-zh/di-review.py --print-only

# 指定日期
python3 ~/projects/claude-code-insight-zh/di-review.py 2026-05-13 --print-only

# 本周趋势（stdout，不写文件）
python3 ~/projects/claude-code-insight-zh/di-review.py --week

# 重新生成（覆盖自动部分，保留反思）
python3 ~/projects/claude-code-insight-zh/di-review.py --regen --quiet
```

产出：`~/.claude/daily-reports/YYYY-MM-DD.md`

特点：
- 秒出，纯规则引擎，不调用 LLM
- 双轨制：有 /insight facet 用 facet，没有则消息推断
- 指标：Bash/Read 比、消息密度、达成率、/compact 次数、反复编辑等

#### /weekly /monthly 模式（insight-zh.py）

```bash
# 周报（7 天）
python3 ~/projects/claude-code-insight-zh/insight-zh.py 7 --html --save

# 月报（30 天）
python3 ~/projects/claude-code-insight-zh/insight-zh.py 30 --html --save

# 纯文本输出（不生成 HTML）
python3 ~/projects/claude-code-insight-zh/insight-zh.py 7 --print-only

# 跳过翻译（更快，英文输出）
python3 ~/projects/claude-code-insight-zh/insight-zh.py 7 --no-translate --print-only
```

产出：`~/.claude/insight-reports/YYYY-MM-DD.html`

特点：
- 几分钟，LLM 驱动，深度理解上下文
- 输出：工作模式画像、坏习惯分析、反常信号、改进建议
- 支持 HTML 可视化报告

### 仓库维护备注

- 兼容入口保留在根目录：`di-review.py`、`insight-zh.py`
- 不要把新逻辑继续堆进 wrapper；真正实现放到 `insight_zh/` 包里
- 回归测试命令：`python3 -m unittest discover -s tests -v`
- 如果只做 smoke，优先跑根目录入口，而不是直接 `python -m insight_zh...`

### Step 1: 识别模式

用户输入 `/insight-zh` 时，检查参数：

| 用户输入 | 模式 | 执行 |
|---------|------|------|
| `/insight-zh /daily` | daily | `di-review.py --print-only` |
| `/insight-zh /weekly` | weekly | `insight-zh.py 7 --html --save` |
| `/insight-zh /monthly` | monthly | `insight-zh.py 30 --html --save` |
| `/insight-zh` 无参数 | 询问 | 用 AskUserQuestion 让用户选 |

询问选项：
- A) /daily — 快速日报（秒出）
- B) /weekly — 深度周报（7 天，LLM 分析）
- C) /monthly — 深度月报（30 天，LLM 分析）

### Step 2: 执行并展示

**daily 模式：**
1. 运行 `di-review.py --print-only`
2. 把报告内容直接展示给用户
3. 告诉用户报告也保存在 `~/.claude/daily-reports/YYYY-MM-DD.md`

**weekly/monthly 模式：**
1. 运行 `insight-zh.py N --html --save`
2. 告诉用户报告保存路径
3. 询问要不要打开浏览器查看
4. 用 2-3 句话总结报告中最值得注意的点

## 两个引擎的区别

| | di-review（/daily） | insight-zh（/weekly /monthly） |
|--|---------------------|-------------------------------|
| 速度 | 秒出 | 几分钟 |
| 引擎 | 规则 + 关键词 | LLM 深度理解 |
| 范围 | 当天 | 7/30 天 |
| 产出 | markdown 日报 | HTML 中文报告 |
| 核心问题 | 今天哪里有问题？ | 这周/月趋势和根因是什么？ |

## 注意事项

- 所有脚本基于 `~/.claude/projects/*/*.jsonl` 原始会话数据
- daily 模式不调用 LLM，weekly/monthly 可能调用 Kimi API 翻译
- 首次运行 weekly/monthly 可能需要翻译（有缓存后会快）
- daily 报告路径：`~/.claude/daily-reports/`
- weekly/monthly 报告路径：`~/.claude/insight-reports/`
- 深度建议缓存按日期范围 + 摘要 hash 分桶，不再按自然日共享
