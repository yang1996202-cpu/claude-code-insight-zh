# Phase 0 Issue 清单

## 目的

在开始架构重构之前，先把当前系统的关键行为冻结下来，避免后续抽模块时悄悄回归。

Phase 0 不解决“优雅”，只解决“行为可验证”。

## 必须冻结的行为

### P0-1 跨天会话日期归属

- 风险：同一会话跨午夜时，日报、周报、月报可能落在不同日期口径上
- 当前期望：深度报告应按可见范围正确纳入跨天会话，至少不能因为 `first_ts` / `last_ts` 归属错误而静默丢失
- 验证方式：准备一个跨天 fixture，断言指定日期范围内仍能命中该会话

### P0-2 JSONL + facets + session-meta 合并规则

- 风险：如果三个来源优先级不明确，会出现统计结果被默认值覆盖
- 当前期望：
  - `jsonl` 提供事实主干
  - `session-meta` 提供结构化指标增强
  - `facets` 提供解释层增强
  - 缺失字段才回退到启发式推断
- 验证方式：准备三种 fixture 组合
  - 只有 jsonl
  - jsonl + meta
  - jsonl + meta + facet

### P0-3 push 口径统一

- 风险：文本、HTML、异常检测、建议层各自读取 `git_commits` / `git_pushes`
- 当前期望：所有报告层都统一读取 `git_pushes`，旧数据仅作为兼容回退
- 验证方式：
  - 文本报告中出现 `Git push`
  - HTML 报告中出现 `Push` 与 `Push 率`
  - 不再出现 `Commit 率`

### P0-4 深度建议缓存按范围分桶

- 风险：同一天跑 7 天和 30 天报告复用同一个 advice cache
- 当前期望：缓存 key 至少包含时间范围和统计摘要
- 验证方式：同一份统计对象改 `last_date`，缓存文件名必须变化

### P0-5 文本与 HTML 的关键指标一致

- 风险：两个 renderer 目前各自重新聚合，极易漂移
- 当前期望：下列指标在文本和 HTML 里同源同值
  - session count
  - total duration
  - push count
  - goal summary
  - friction summary
- 验证方式：同一批 fixture 同时生成 markdown/html，对关键字段做字符串断言

### P0-6 无 LLM 时核心分析仍可运行

- 风险：翻译或 advice 调用失败污染事实层
- 当前期望：在 mock 掉 `generate_coaching_advice()` 和翻译逻辑后，报告仍能完整生成
- 验证方式：在测试里 stub 掉 LLM 调用，跑文本和 HTML smoke test

### P0-7 启发式字段不能制造“假精确”

- 风险：没有真实 facets 时把 outcome / helpfulness 写死，用户会误以为这是真统计
- 当前期望：
  - 有真实增强数据时展示真实结果
  - 没有时展示推断结果或明确缺失
  - 不允许写死为单一默认值
- 验证方式：只有 jsonl 的 fixture 不应让所有会话都自动变成同一种 outcome / helpfulness

## 建议产出

Phase 0 完成后，仓库至少应新增：

- `tests/fixtures/`：最小样本数据
- `tests/test_session_loading.py`
- `tests/test_report_consistency.py`
- `tests/test_advice_cache.py`

## 完成标准

满足以下条件，才进入 Phase 1：

1. 关键行为都能被自动化验证
2. 文本与 HTML 的关键指标没有口径漂移
3. LLM 不可用时不影响事实层
4. 后续重构不再依赖手工 eyeballing

## 不要在 Phase 0 做的事

- 不改目录结构
- 不大规模重写函数
- 不先追求 dataclass / 包结构优雅
- 不引入新的前端或 Web 层

Phase 0 的本质是：先把现在这个系统钉住，再谈抽象。