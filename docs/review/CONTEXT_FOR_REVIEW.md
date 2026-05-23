# 审查上下文 —— claude-code-insight-zh

## 一句话定位

Claude Code 中文使用分析套件。基于 `~/.claude/projects/*/*.jsonl` 原始会话数据，生成日报/周报/月报。

## 核心架构（3 个文件）

| 文件 | 职责 | 引擎 |
|------|------|------|
| `di-review.py` | 日报（/daily） | 纯规则，秒出，不写 LLM |
| `insight-zh.py` | 周报/月报（/weekly /monthly） | LLM 驱动，分钟级 |
| `SKILL.md` | Claude Code skill 定义 | 入口路由 |

## 关键设计决策（不可逆）

### 1. 绘画方法论（用户强制要求）

- 来源：保罗·格雷厄姆《黑客与画家》
- 原则：**不做好坏评判，只描述创作阶段和能量流向**
- 创作阶段：试笔 / 临摹 / 素描 / 探索 / 上色 / 装裱 / 整理
- 能量流向：消耗型 / 创造型 / 学习型 / 整理型
- 实现：`load_data_from_jsonl()` 中启发式识别，`generate_painting_analysis()` 生成"画室观察笔记"

### 2. 双轮轰炸结构（当前状态）

报告分两层，**同时存在**：

1. **上层 —— 画室观察笔记**：温和、描述性，基于绘画方法论
2. **下层 —— Karpathy 深度建议**：直接、认知陷阱直击，LLM 生成 5 条结构化建议（证据/根因/行动）

用户原话："就算重复，但是上边基于绘画那段洞察，下边是这个卡帕西，双轮轰炸"

### 3. 指标：git push 而非 git commit

用户明确："只有 push 才有意义"。代码中所有 `git_commits` 已改为 `git_pushes`。

### 4. 动态时间词

根据数据范围自动调整：<=7天="这周"，<=14天="这两周"，<=31天="这月"，>31天="这段时期"。实现见 `insight-zh.py` 中 `day_span` 计算逻辑。

## 当前已知问题（待审查重点）

1. **LLM 不遵守数量要求**：prompt 要求 5 条建议，实际常返回 3 条。可能需加大 `max_tokens`（当前 4000）或在 prompt 中更强势约束
2. **绘画阶段识别准确率**：纯启发式规则（关键词匹配 + 工具计数），无 ground truth 验证，可能误分类
3. **文本报告渲染格式**：深度建议刚加入文本报告（`generate_report`），格式较简陋，仅有 ### 标题 + **证据/根因/行动**
4. **缓存策略粗糙**：深度建议按自然日缓存（`.advice-cache-YYYY-MM-DD.json`），不同日期范围跑同一天的报告会复用同一份缓存，可能数据不匹配

## 调用链路

```
用户输入 /insight-zh /weekly
  → SKILL.md 路由 → python3 insight-zh.py 7 --html --save
    → load_data_from_jsonl() 解析原始数据
      → 聚合统计 + 绘画阶段/能量流向识别
    → generate_painting_analysis() 生成画室观察笔记
    → generate_coaching_advice() 调用 LLM 生成 Karpathy 建议（带天级缓存）
    → detect_anomalies() 规则化反常检测
    → generate_html_report() / generate_report() 输出
```

## API 配置

- 默认：Kimi API (`https://api.kimi.com/coding/`，模型 `kimi-for-coding`)
- 通过环境变量可切换：INSIGHT_API_KEY / INSIGHT_API_BASE / INSIGHT_API_MODEL

## 用户偏好（来自对话）

- 不要固定标准 KPI，不要"好坏"评判
- 要"发挥主动性"的洞察
- 质量 > 数量
- 语言不能晦涩，用户在学习阶段
- 所有报告、文档、总结必须统一中文

## 审查建议方向

1. `generate_coaching_advice()` 的 prompt 是否足够强制 5 条输出？
2. 绘画阶段识别的启发式规则是否合理？有无更精准的信号？
3. 双轮结构是否有内容过度重复？如何差异化两层定位？
4. 缓存策略是否应改为按查询参数（日期范围）而非仅按自然日？
