# 面向长链 Web Agent 的可恢复执行 Runtime

[English](README.md) | [简体中文](README.zh-CN.md)

基于 **Browser Use v0.13.10** 构建的容错执行层，面向包含外部副作用的长链 Web Agent 任务。

这个项目重点解决的不是普通的“Agent 执行失败”，而是一类更危险的恢复问题：

```text
Agent 可能已经改变了外部世界
            +
本地进程在确认结果前发生崩溃
            =
盲目重放可能再次执行副作用
```

典型情况包括：重复提交申请、重复创建订单、重复发送消息，或者重复修改远端资料。

本仓库在 Browser Use 之上增加了一套语义级恢复 Runtime：在执行外部副作用前持久化执行意图，通过 Checkpoint 保存运行状态，通过 Verification 验证真实后置条件，并在结果不确定时通过 Reconciliation 对历史 attempt 进行对账，而不是直接重试。

---

## 项目新增了什么

```text
Browser Use Agent
       |
       v
Semantic Task Contract
       |
       v
Recoverable Harness
       |
       +--> Runtime State
       |
       +--> Effect Ledger --------+
       |                          |
       +--> Checkpoint            |
       |                          v
       +--> Verification --> Reconciliation
                                  |
                                  v
                            Safe Resume / Retry
```

核心能力：

- **Semantic Task Contract（语义任务契约）**：在易变化的 Browser Step 和自然语言 Plan 之上，引入相对稳定的语义执行单元。
- **Durable Effect Ledger（持久化副作用账本）**：记录外部副作用从 `PREPARED -> ATTEMPTED -> COMMITTED / NOT_APPLIED / UNKNOWN` 的事实链。
- **Checkpoint + SQLite 持久化**：保存 Unit Runtime State 和 Effect Ledger 游标，用于进程崩溃后的状态恢复。
- **Verification（验证）**：通过只读观察确认真实后置条件，而不是仅相信模型声称“任务完成”。
- **Reconciliation（对账）**：针对结果未知的历史 attempt 进行观察和确认，不产生新的外部执行 attempt。
- **Recovery Safety Gate（恢复安全门）**：只要旧副作用结果仍不确定，就禁止重新执行。
- **Fault Injection（故障注入）**：在持久化边界附近主动注入崩溃，验证恢复逻辑，而不是只测试普通 Python 异常。
- **Browser Use 集成**：保留原生 Browser Use Agent 与浏览器执行链路，在其外部增加可恢复运行层。

更详细的设计说明见：[docs/recoverable-runtime/architecture.md](docs/recoverable-runtime/architecture.md)

---

## 核心故障语义

一次具有外部副作用的执行过程如下：

```text
ACTIVE
  |
  v
PREPARED   <- 外部动作执行前，先持久化执行意图
  |
  v
执行外部动作
  |
  v
ATTEMPTED  <- Runtime 已确认跨过外部副作用边界
  |
  v
验证真实后置条件
  |
  +---- VERIFIED ------> COMMITTED
  |
  +---- REJECTED ------> NOT_APPLIED
  |
  +---- INCONCLUSIVE --> UNKNOWN
```

其中最重要的一条规则是：

> **UNKNOWN 状态禁止直接重试。**

如果 Runtime 无法确认副作用到底有没有发生，就不能把“不确定”当作“没有发生”。必须先针对原来的 attempt 做 Reconciliation。只有观察证据明确表明副作用没有生效，新的执行才可能合法。

因此可以避免：

```text
提交申请
-> 提交后进程崩溃
-> 结果未知
-> 从任务重新执行
-> 再提交一次
-> 产生重复副作用
```

---

## Recovery 实验

仓库不仅包含单元测试，还设计了可控故障实验，用于验证真正的崩溃恢复行为。

### Recovery Matrix

实验位于：

```text
experiments/recovery_matrix/
```

提交的实验结果：

[experiments/recovery_matrix/results/latest.md](experiments/recovery_matrix/results/latest.md)

实验分别在以下三个位置制造故障：

- `after_prepared`：已经持久化 PREPARED，但外部动作尚未执行；
- `after_attempted`：外部动作已经执行并记录 ATTEMPTED；
- `verifier_unavailable`：副作用可能已经发生，但首次 Verification 无法得到确定结论。

每个场景在 baseline 与 harness 下各重复 5 次，最终结果如下：

| 指标 | Restart Baseline | Recoverable Harness |
| --- | ---: | ---: |
| Task Completion Rate | 100.0% | 100.0% |
| Safe Recovery Rate | 33.3% | 100.0% |
| Duplicate Effect Rate | 66.7% | 0.0% |
| Unsafe Retry Rate | 66.7% | 0.0% |

这里需要特别说明：

> 表格中的 Baseline 是“restart-from-task，不保存 durable effect history”的对照实验，不代表 Browser Use 所有可能的恢复方式。

项目另外实现了 Native Browser Use `AgentHistory` baseline：

```text
experiments/native_history_baseline/
```

用于单独评估 Browser Use 原生 History / Replay 能力，与语义级副作用状态管理进行区分。

### 跨进程 Crash Recovery

跨进程实验位于：

```text
experiments/process_recovery/
```

实验使用独立 Worker Process 和 hard exit 制造真实进程终止，再依赖 SQLite 中的持久化状态恢复 Workflow。

结果：

[experiments/process_recovery/results/day10_summary.md](experiments/process_recovery/results/day10_summary.md)

| 场景 | 最终 Unit | 最终 Effect | Verification | 外部 submit 次数 |
| --- | --- | --- | --- | ---: |
| after_prepared | completed | committed | verified | 1 |
| after_attempted | completed | committed | verified | 1 |
| verifier_unavailable | completed | committed | verified | 1 |

完整实验设计与指标定义见：

[docs/recoverable-runtime/experiments.md](docs/recoverable-runtime/experiments.md)

---

## 为什么 Task Completion Rate 不够

只看“任务最后有没有成功”会掩盖副作用恢复中的严重问题。

例如：

```text
系统 A：
submit
-> crash
-> restart
-> submit again
-> 最终状态 SUBMITTED

系统 B：
submit
-> crash
-> 恢复历史 attempt
-> Reconciliation
-> 最终状态 SUBMITTED
```

两个系统的 Task Completion Rate 都可以是 100%。

但系统 A 实际执行了两次外部副作用。

因此本项目除了最终完成率，还单独衡量：

- Safe Recovery Rate
- Duplicate Effect Rate
- Unsafe Retry Rate
- Reconciliation Outcome

项目关注的是：

> **任务恢复成功的同时，是否保持了副作用级安全性。**

---

## 仓库结构

为了能在真实 Browser Use Agent 和浏览器执行链路上做端到端验证，本仓库保留了上游 Browser Use 源码树。

与本项目直接相关的主要模块如下：

```text
browser_use/recovery/
├── contracts.py                 # Semantic Contract 与 Runtime Schema
├── runtime_state.py             # Unit / Effect / Verification 状态迁移
├── harness.py                   # Browser Use 集成与 Resume
├── side_effects.py              # 外部副作用持久化执行边界
├── verification.py              # 结构化 Verification
├── observational_verifier.py    # Browser 只读观察
├── reconciliation.py            # UNKNOWN 历史 attempt 对账
├── bootstrap.py                 # 持久化 Runtime 恢复
├── effect_boundary.py           # Browser Action 与 Effect Boundary 映射
└── persistence/
    ├── storage.py               # SQLite Runtime Storage
    ├── checkpoint.py
    ├── effect_ledger.py
    └── models.py

tests/ci/recovery/               # Recovery 回归测试

experiments/
├── recovery_smoke/              # 可控的本地外部世界
├── recovery_matrix/             # Baseline vs Harness 故障矩阵
├── process_recovery/            # 跨进程 hard-crash 恢复
└── native_history_baseline/     # Native AgentHistory 对照实验
```

---

## 与上游 Browser Use 的边界

这个项目是 **基于 Browser Use 的二次开发 / Runtime 扩展**，不是从零实现 Browser Agent。

Recovery Runtime 基于 Browser Use v0.13.10 开发。用于区分上游与本项目改动的基线 Commit 为：

```text
5c892e013a73e6622e6f50336e1eb0aa2c4405f2
```

本项目新增工作主要集中在：

- `browser_use/recovery/`
- `tests/ci/recovery/`
- `experiments/recovery_smoke/`
- `experiments/recovery_matrix/`
- `experiments/process_recovery/`
- `experiments/native_history_baseline/`

如果想直接查看相对于上游基线新增的 Recovery Runtime：

```bash
git diff 5c892e013a73e6622e6f50336e1eb0aa2c4405f2..main -- browser_use/recovery
```

上游项目：

[browser-use/browser-use](https://github.com/browser-use/browser-use)

---

## 复现实验

开发环境沿用 Browser Use，要求 Python 3.11+ 和 `uv`。

### Recovery 回归测试

```bash
uv run pytest tests/ci/recovery -q
```

### Recovery Matrix

终端 1：

```bash
uv run python experiments/recovery_smoke/server.py --port 8765
```

终端 2：

```bash
uv run python -m experiments.recovery_matrix.runner --headless --trials 5
```

### 跨进程 Crash Recovery

```bash
uv run python -m experiments.process_recovery.run_experiment
```

该实验会自动启动本地可控应用 Server 和独立 Worker Process。

---

## 关键设计问题

### 为什么不能直接使用 Browser Use 的 Step 作为 Checkpoint？

Browser Step 是执行层概念。

同一个业务意图可能因为页面变化、Replan、重试等原因，在不同运行中对应不同数量的 Step。

因此 Recovery 不能依赖：

```text
step_7
step_8
step_9
```

来表达稳定业务语义。

本项目在 Browser Step 之上增加 Semantic Unit，使恢复身份绑定到“业务意图”，而不是某一次具体执行轮次。

### 为什么既需要 Checkpoint，又需要 Effect Ledger？

Checkpoint 是 Runtime 的状态快照。

Effect Ledger 则记录外部副作用 attempt 的有序事实。

崩溃可能发生在：

```text
新的 Effect Record 已经写入
        ↓
进程崩溃
        ↓
下一份 Checkpoint 还没有写入
```

因此恢复时不能只相信旧 Checkpoint，还必须读取 Checkpoint 游标之后新增的 Ledger 事实。

### 为什么 Verification 要和执行分开？

Executor 正常返回，并不等于外部世界一定已经满足目标状态。

同样，HTTP 超时或连接断开，也不意味着外部操作一定失败。

因此 Verification 被建模为独立的只读观察过程：

```text
VERIFIED
REJECTED
INCONCLUSIVE
```

而不是简单使用：

```text
action success / exception
```

作为真实世界结果。

### 为什么 Reconciliation 必须复用原来的 attempt？

Reconciliation 要回答的是：

> “历史上这个已经发生过、但结果未知的 attempt，最终到底发生了什么？”

它不是一次新的业务执行。

因此会复用原来的：

```text
effect_id
attempt_id
```

只追加新的观察证据，而不是创建新的 side-effect attempt。

---

## 当前边界与限制

当前版本并不是一个通用的分布式事务系统，边界包括：

- 通用 `RecoverableHarness` v1 正式支持的是 **Browser-based Verification**；
- Schema 中已经预留 `EXTERNAL_TOOL` 和 `MIXED`，但尚未形成通用的、具备 capability sandbox 的 External Tool Verification；
- 部分底层实验可以注入自定义 Verifier，但这不等价于正式支持任意 External Tool；
- Semantic Contract 当前仍然是显式输入，不是一个能够可靠自动生成任意任务契约的通用 Planner；
- 系统保证的是当前建模边界内的 Recovery Safety，而不是任意网站上的严格分布式 Exactly-Once；
- 项目解决的是 Runtime Recovery 与副作用安全问题，不负责提升底层 LLM 的规划能力或浏览器操作准确率。

这些限制被明确保留，是因为项目目标不是通过 Task Completion 掩盖不确定性，而是让恢复行为具备明确、可验证的语义。

---

## License

仓库保留了 Browser Use 原项目的 MIT License 和版权声明，见：

[LICENSE](LICENSE)

本仓库新增的 Recovery Runtime 代码沿用当前仓库许可证。
