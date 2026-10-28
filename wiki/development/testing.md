# 测试分组、耗时与精简规则

[文档首页](../README.md) · [English README](../../README.md) · [简体中文 README](../../README.zh-CN.md) · [参与开发](contributing.md)

## 日常与显式测试

~~~console
uv run pytest -q
uv run pytest -q --suite quick
uv run pytest -q --suite backend
uv run pytest -q --suite browser
uv run pytest -q --domain oauth
uv run pytest -q -m "catalog and integration"
uv run pytest -q --changed-from <GIT_REF>
~~~

默认 daily 运行功能回归，包括浏览器、密码池容量、限流、授权撤销及安全边界检查；自动使用最多 4 个 worker 和 worksteal 调度。显式 `-n N` 优先，`-n 0` 串行，调试或仅收集时不自动并行。默认不执行长耗时压测及真实外部服务测试。并发上限用于控制浏览器和密码哈希的内存占用。

| suite | 选择范围 |
| --- | --- |
| daily | unit、integration、browser |
| quick | unit 加少量关键目录、协议、审批 smoke |
| backend | unit、integration |
| browser | 实际使用浏览器的用例 |
| performance | 显式压力与容量基准，默认串行 |
| all | 日常加性能；外部服务仍需显式选择 |

~~~console
uv run pytest -q --suite performance
uv run pytest -q -m performance
uv run pytest -q tests/performance/test_catalog_capacity.py
uv run pytest -q --suite all
uv run pytest -q --run-live -m live -n 0
~~~

`--run-performance` 在所选套件中额外纳入性能测试；指定对应性能文件、目录或节点也视为显式请求。`-m "not performance"` 不会启用压测。性能时延、吞吐和内存指标易受争用影响，保持串行并在空闲环境下运行。

live 测试保留原有环境开关、授权及服务可用性检查；选择不等于已配置外部服务。MySQL 使用原有 `--db-backend`、`--require-mysql`、`--managed-mysql` 验证；默认日常不代表 MySQL 矩阵通过。

Node 前端单元测试独立运行：

~~~console
node --test tests/frontend/core.test.mjs tests/frontend/translation.test.mjs
~~~

## 分类规则

主要层为 `unit`、`integration`、`browser`、`performance`、`live`，每项只允许一个。业务域为 `auth`、`catalog`、`gateway`、`oauth`、`runtime`、`storage`、`imports`、`ui`、`deployment`、`core`，可多选；`smoke` 是附加标记。

`tests/suite_manifest.py` 保存审核后的模块及测试模块依赖。`tests/suite_policy.py` 添加标记、过滤套件并决定并发。显式函数/模块标记可覆盖主要层，冲突层报错。unit 清单中依赖共享应用/数据库 fixture 的用例仍归 integration。

不能只根据文件名分组：静态浏览器预算检查属于 unit；诊断摘要 API 属于 integration；密码池/运行时容量边界不是压测。新增未分类模块默认 integration，并关联所有业务域，继续进入日常回归和保守选测，审核后再细化。

## 按改动选择

`--changed-from <GIT_REF>` 考虑该提交以来的已提交变更、工作区修改及未跟踪文件。业务域映射包含相邻消费者；测试模块改动包含导入它的测试模块。

配置、身份、数据库、传输、运行时、迁移、未知源码或共享测试基础设施改动扩大到完整日常回归。只有 README/wiki/docs 改动时运行 unit 和 smoke。错误 Git 引用直接报错，不以空集合冒充验证成功。

它是日常辅助选择，不替代合并/发布前按风险进行日常全量、性能或后端矩阵验收。

## 耗时报告

默认显示最慢 15 个阶段，将每项 setup、call、teardown、结果、主要层和业务域写入 `.pytest_cache/d/mmg/timings.json`。并行报告由控制进程汇总，worker 不竞争写文件。

~~~console
uv run pytest -q --timing-report artifacts/test-timings/daily.json
uv run pytest -q --durations=30
uv run pytest --collect-only -q --suite performance
~~~

默认保存最后一次运行；对比时用独立 `--timing-report` 路径保存。仅收集不会覆盖报告。阶段合计与并行墙钟时间不能混用。

## 覆盖保留与精简

- 合并同一流程中的独立断言，减少重复登录、建库和启动浏览器，保留各测试独立状态。
- 缩小数据必须继续跨越原来的分页、大小、批次或深度边界，并断言前提。
- 大输入压力场景可移入 performance，日常保留紧邻边界的功能断言。
- 删除须有等价覆盖位置；安全隔离、竞态和未知结果不重放不可仅因耗时移出日常。
- 不共享可变数据库/应用，不关闭脱敏、持久化或授权检查来换速度。

能力卡片注解与换行的两组三入口浏览器流程已合并，保留四类描述、注解错误类型、四个标签、标题转义及三种视口断言，每个入口在同一次登录及浏览器流程中完成这些断言。

审计日常测试用 4,097 字符验证 4,096 字符截断，10 万字符输入保留为 performance 用例。两个大目录摘要 API 共用 51 条大 Schema，仍断言总数据超过 1 MiB、超过一页及响应只有有界摘要。目录分页测试继续跨越 250 条 staging 批次边界。


## 环境同步与 Windows 文件锁

若 uv 尚未进入 pytest 就因运行中的入口可执行文件被占用而同步失败，已安装依赖可用 `uv run --no-sync pytest -q` 验证。依赖版本发生变化时仍需完成正常同步，跳过同步不代表依赖已更新。
