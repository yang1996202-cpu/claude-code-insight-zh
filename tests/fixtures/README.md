# Fixtures

这里放 Phase 0 和 Phase 1 的最小 fixture 约定。

当前测试优先使用 `tempfile.TemporaryDirectory()` 在运行期动态写入数据，
避免维护大量静态样本文件。

后续如果出现以下情况，再引入静态 fixture 文件：

- 单个样本跨多个测试复用
- 某类 jsonl/meta/facet 结构过长，不适合内联构造
- 需要保留历史回归样本

当前约定至少覆盖三种数据组合：

- `jsonl_only`
- `jsonl_plus_meta`
- `jsonl_plus_meta_plus_facet`
