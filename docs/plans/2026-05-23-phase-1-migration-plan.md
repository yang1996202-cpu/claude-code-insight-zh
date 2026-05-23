# Phase 1 Shared Core Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 抽出第一个共享内核，把会话加载与规范化从大脚本中剥离出来，为后续 analysis / renderer 拆分建立稳定边界。

**Architecture:** 保留现有 `insight-zh.py` 和 `di-review.py` 作为薄入口，先新增 `insight_zh/` 包，并只迁移“数据接入 + 统一 session 模型”这一层。Phase 1 不追求把所有分析逻辑搬走，只要求两个脚本都能开始依赖同一个会话结构。

**Tech Stack:** Python 3.9+、标准库、`anthropic`（现有依赖，仅深度模式使用）、`unittest`

---

## 目标范围

Phase 1 只做三件事：

1. 引入 `NormalizedSession`
2. 抽出 source adapter + session loader
3. 让 `insight-zh.py` 先切到新 loader，`di-review.py` 保留兼容但不新增重复逻辑

## 非目标

- 不拆 renderer
- 不迁移 anomaly / painting / advice
- 不重写 CLI
- 不引入新依赖

## Task 1: 建立测试骨架与 fixture

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/fixtures/README.md`
- Create: `tests/test_session_loading.py`

**Step 1: 创建测试目录**

创建：

- `tests/__init__.py`
- `tests/fixtures/README.md`

`tests/fixtures/README.md` 说明至少三种 fixture：

- `jsonl_only`
- `jsonl_plus_meta`
- `jsonl_plus_meta_plus_facet`

**Step 2: 写第一批失败测试**

在 `tests/test_session_loading.py` 中先写以下断言：

- 可以从 fixture 加载 session
- `git_pushes` 统一可读
- facet/meta 优先级符合预期
- 跨天会话不会因日期过滤被静默丢掉

建议测试骨架：

```python
import unittest


class SessionLoadingTest(unittest.TestCase):
    def test_merge_jsonl_meta_facet(self):
        self.assertTrue(False)

    def test_cross_day_session_in_range(self):
        self.assertTrue(False)


if __name__ == "__main__":
    unittest.main()
```

**Step 3: 运行测试确认失败**

Run:

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 -m unittest tests.test_session_loading -v
```

Expected:

- FAIL
- 报告未实现的 loader / model

**Step 4: Commit**

```bash
git add tests
git commit -m "test: add phase 1 session loading skeleton"
```

## Task 2: 引入统一领域模型

**Files:**
- Create: `insight_zh/__init__.py`
- Create: `insight_zh/domain/__init__.py`
- Create: `insight_zh/domain/session.py`

**Step 1: 新建包骨架**

创建目录：

- `insight_zh/`
- `insight_zh/domain/`

**Step 2: 实现最小 `NormalizedSession`**

在 `insight_zh/domain/session.py` 中定义 dataclass，字段至少包括：

- `session_id`
- `project_path`
- `start_time`
- `end_time`
- `report_date`
- `duration_minutes`
- `user_message_count`
- `assistant_message_count`
- `tool_counts`
- `input_tokens`
- `output_tokens`
- `git_pushes`
- `first_prompt`
- `all_user_texts`
- `facet`
- `meta`

并提供一个 helper：

- `get_tool_count(name: str) -> int`

**Step 3: 运行测试**

Run:

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 -m unittest tests.test_session_loading -v
```

Expected:

- 仍然 FAIL
- 但错误推进到 loader 尚未实现

**Step 4: Commit**

```bash
git add insight_zh/domain tests/test_session_loading.py
git commit -m "feat: add normalized session model"
```

## Task 3: 抽出 source adapter

**Files:**
- Create: `insight_zh/sources/__init__.py`
- Create: `insight_zh/sources/jsonl_source.py`
- Create: `insight_zh/sources/facets_source.py`
- Create: `insight_zh/sources/session_meta_source.py`

**Step 1: 实现最小只读 adapter**

要求：

- 每个 adapter 只负责读取，不做业务判断
- 不拼 report
- 不写缓存

建议接口：

- `load_jsonl_session(path)`
- `load_facet(session_id, facets_dir)`
- `load_session_meta(session_id, meta_dir)`

**Step 2: 为 adapter 补测试用例**

在 `tests/test_session_loading.py` 里加断言：

- adapter 能处理缺失文件
- adapter 遇到坏 JSON 返回空结构而不是崩溃

**Step 3: 运行测试**

Run:

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 -m unittest tests.test_session_loading -v
```

Expected:

- 仍可能 FAIL
- 但错误应该集中在 session loader 组装层

**Step 4: Commit**

```bash
git add insight_zh/sources tests/test_session_loading.py
git commit -m "feat: add session source adapters"
```

## Task 4: 实现统一 session loader

**Files:**
- Create: `insight_zh/sources/session_loader.py`
- Modify: `insight_zh/domain/session.py`
- Modify: `tests/test_session_loading.py`

**Step 1: 实现 loader**

至少提供两个函数：

- `load_sessions_from_workspace(start_date, end_date, claude_dir)`
- `merge_session_sources(...)`

合并规则固定为：

1. `jsonl` 为主源
2. `session-meta` 覆盖结构化指标
3. `facet` 补解释层字段
4. 缺失字段再交给旧启发式逻辑

**Step 2: 让测试覆盖四个关键断言**

- `git_pushes` 来自统一 helper
- `facet` 存在时不被默认值覆盖
- 跨天会话按日期范围正确纳入
- 纯 jsonl 情况下仍可构造 `NormalizedSession`

**Step 3: 运行测试**

Run:

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 -m unittest tests.test_session_loading -v
```

Expected:

- PASS

**Step 4: Commit**

```bash
git add insight_zh/sources insight_zh/domain tests/test_session_loading.py
git commit -m "feat: add shared session loader"
```

## Task 5: 在 `insight-zh.py` 接入新 loader

**Files:**
- Modify: `insight-zh.py`
- Modify: `tests/test_session_loading.py`

**Step 1: 保留旧入口，替换内部加载实现**

要求：

- CLI 参数不变
- 文件输出行为不变
- `load_data_from_jsonl()` 可先改为调用新 loader，再转换成旧 renderer 还认识的结构

这里允许保留一个短期 adapter，例如：

- `normalized_session -> legacy item dict`

但不允许新代码反向 import 旧脚本内部实现。

**Step 2: 添加一个兼容测试**

断言：

- 调用 `load_data_from_jsonl()` 后仍返回现有 renderer 能处理的数据结构

**Step 3: 运行测试**

Run:

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 -m unittest tests.test_session_loading -v
```

Expected:

- PASS

**Step 4: 做一个 smoke run**

Run:

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 insight-zh.py 7 --print-only --no-translate >/tmp/insight-smoke.txt
```

Expected:

- 命令退出码为 0
- 报告头部存在 `Claude Code 中文洞察报告`

**Step 5: Commit**

```bash
git add insight-zh.py insight_zh tests/test_session_loading.py
git commit -m "refactor: route insight loading through shared session core"
```

## Task 6: 让 `di-review.py` 只做最小兼容接入

**Files:**
- Modify: `di-review.py`
- Create: `tests/test_daily_loader_compat.py`

**Step 1: 只迁移底层会话读取，不迁移日报分析逻辑**

要求：

- 保持 daily 的规则分析函数不动
- 仅让底层会话读取复用共享 loader 或共享 adapter

**Step 2: 补兼容测试**

断言：

- daily 仍能按目标日期筛选会话
- 不破坏现有 markdown 输出主结构

**Step 3: 运行测试**

Run:

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 -m unittest tests.test_session_loading tests.test_daily_loader_compat -v
```

Expected:

- PASS

**Step 4: 做 smoke run**

Run:

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 di-review.py --print-only >/tmp/daily-smoke.txt
```

Expected:

- 命令退出码为 0
- 输出包含 `Claude Code 日报`

**Step 5: Commit**

```bash
git add di-review.py tests/test_daily_loader_compat.py
git commit -m "refactor: reuse shared session loading in daily report"
```

## Task 7: 收尾与文档同步

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`

**Step 1: 更新文档**

说明：

- 共享 core 已建立
- 当前只完成 Phase 1
- renderer / analysis 仍在旧脚本中，属于后续阶段

**Step 2: 跑最终检查**

Run:

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 -m unittest tests.test_session_loading tests.test_daily_loader_compat -v
```

以及：

```bash
cd /Users/yang/projects/claude-code-insight-zh && python3 insight-zh.py 7 --print-only --no-translate >/tmp/insight-final.txt && python3 di-review.py --print-only >/tmp/daily-final.txt
```

Expected:

- 测试全部通过
- 两个 smoke run 都成功

**Step 3: Commit**

```bash
git add README.md docs/ARCHITECTURE.md
git commit -m "docs: document shared core phase 1 migration"
```

## 完成定义

Phase 1 完成后，应满足：

1. 仓库里存在共享的 session 领域模型
2. `insight-zh.py` 已不再直接承担底层多源加载细节
3. `di-review.py` 开始复用共享 loader 或 adapter
4. 关键行为有自动测试兜底
5. 现有 CLI 入口和用户习惯不变

## 后续衔接

Phase 1 结束后，下一阶段才进入：

- 抽共享 analysis
- 抽 `ReportViewModel`
- 拆 renderer
- 隔离 LLM 与缓存