---
name: insight-zh
description: |
  生成 Claude Code 中文洞察报告。分析你的使用数据（facets + session-meta），
  输出工作模式画像、坏习惯分析、反常信号、改进建议等。
  支持任意天数范围（1天/3天/7天/全量）和 HTML/文本两种输出格式。
  核心目的：像每日体检单一样帮你发现自己的使用模式，持续改进。
  触发词：insight, 洞察, 报告, 复盘, 日报, 周报
allowed-tools:
  - Bash
  - Read
---

## 用法

用户输入 `/insight-zh` 或类似触发词时，按以下流程执行：

### Step 1: 询问范围（如果用户没有指定）

如果用户只输入了 `/insight-zh` 没有带参数，用 AskUserQuestion 询问：

> 想看多长时间范围的数据？

选项：
- A) 今天/昨天（最近 1 天）
- B) 最近 3 天
- C) 最近 7 天
- D) 全部历史

根据选择运行对应命令。

### Step 2: 运行报告

根据用户选择或传入的参数，运行 insight-zh.py：

```bash
# 默认（用户选了"全部"或没选）
python3 ~/.claude/insight-zh.py --html --save

# 最近 N 天
python3 ~/.claude/insight-zh.py N --html --save

# 只输出文本到终端（不生成 HTML 文件）
python3 ~/.claude/insight-zh.py N --print-only

# 跳过翻译（更快）
python3 ~/.claude/insight-zh.py N --no-translate --print-only
```

### Step 3: 展示结果

报告生成后：

1. 如果是 `--print-only` 模式：直接输出报告文本内容
2. 如果是 `--html` 模式：告诉用户报告保存路径，并询问要不要打开浏览器查看

报告默认保存路径：`~/.claude/insight-reports/YYYY-MM-DD.html`

### Step 4: 简要总结

用 2-3 句话总结报告中最值得注意的点：
- 有没有红色反常信号
- 最大的坏习惯是什么
- 今天/这周做得好的地方

不要全文朗读，只给精华摘要。

## 参数速查

| 用户输入 | 执行命令 |
|---------|---------|
| `/insight-zh` | 询问范围后执行 |
| `/insight-zh 1` | `python3 ~/.claude/insight-zh.py 1 --html --save` |
| `/insight-zh 3` | `python3 ~/.claude/insight-zh.py 3 --html --save` |
| `/insight-zh 7` | `python3 ~/.claude/insight-zh.py 7 --html --save` |
| `/insight-zh --text` | `python3 ~/.claude/insight-zh.py --print-only` |
| `/insight-zh --no-translate` | 跳过 LLM 翻译，纯规则生成（更快） |

## 注意事项

- 报告基于 `~/.claude/usage-data/facets/` 和 `session-meta/` 数据
- 只包含 Claude Code CLI 的会话，不含 Claude App（桌面端/网页端）
- 首次运行可能需要翻译（会调用 Kimi API），后续有缓存会快很多
- 如果报告生成失败，检查 `~/.claude/usage-data/` 目录是否有数据
