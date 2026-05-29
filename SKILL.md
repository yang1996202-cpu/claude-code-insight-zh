---
name: insight-zh
description: |
  Claude Code 中文 HTML 使用洞察报告。

  统一入口：
  - 今日：insight-zh.py 1 --html --save
  - 周报：insight-zh.py 7 --html --save
  - 月报：insight-zh.py 30 --html --save

  数据同源：基于 ~/.claude/projects/*/*.jsonl，会维护 ~/.claude/usage-data-zh/ 中文缓存。
  触发词：insight, 洞察, 报告, 复盘, 日报, 周报, 月报, daily, weekly
allowed-tools:
  - Bash
  - Read
---

## 用法

当前仓库根目录只有一个稳定入口：

- `insight-zh.py`

真实实现位于：

- `insight_zh/insight_cli.py`
- `insight_zh/analysis/`
- `insight_zh/sources/`

### 快速入口

```bash
# 今日 HTML 报告
python3 ~/projects/claude-code-insight-zh/insight-zh.py 1 --html --save

# 近 7 天 HTML 报告
python3 ~/projects/claude-code-insight-zh/insight-zh.py 7 --html --save

# 近 30 天 HTML 报告
python3 ~/projects/claude-code-insight-zh/insight-zh.py 30 --html --save
```

产出：

```text
~/.claude/usage-data-zh/reports/YYYY-MM-DD.html
~/.claude/usage-data-zh/reports/YYYY-MM-DD_to_YYYY-MM-DD.html
```

### Step 1: 识别模式

用户输入 `/insight-zh` 时，检查参数：

| 用户输入 | 执行 |
|---------|------|
| `/insight-zh /daily` 或 `/insight-zh daily` | `insight-zh.py 1 --html --save` |
| `/insight-zh /weekly` 或 `/insight-zh weekly` | `insight-zh.py 7 --html --save` |
| `/insight-zh /monthly` 或 `/insight-zh monthly` | `insight-zh.py 30 --html --save` |
| `/insight-zh` 无参数 | 询问用户要 1 天、7 天还是 30 天 |

### Step 2: 执行并展示

1. 运行对应的 `insight-zh.py N --html --save`
2. 告诉用户报告保存路径
3. 用 2-3 句话总结报告里最值得注意的点
4. 如果用户要看浏览器，再打开 HTML

## 数据口径

- 会话数：选定日期范围内有活动的 Claude Code CLI JSONL 会话。
- 消息数：用户真实输入文本数，不含 tool_result、/command 包装、local-command caveat。
- 时长：活跃时长估算，按相邻 JSONL 事件时间差累加，单个空闲间隔最多计 15 分钟。
- 官方 `/insights` 的 `usage-data` 是可选增强源，不是必需上游。
- 中文分析缓存位于 `~/.claude/usage-data-zh/`。

## 注意事项

- 不再使用旧的 markdown daily 引擎。
- 所有周期都走同一套 HTML 报告逻辑。
- 缓存带自动计算的 analyzer version；原始 JSONL、官方 usage-data 或分析代码变化后会自动失效重算。
- 没有 `INSIGHT_API_KEY` 时，报告仍会生成，只跳过 LLM 深度建议。
- 回归测试命令：`python3 -m unittest discover -s tests -v`
