# AI 知识库 · 三 Agent Issues

来源：`specs/agents-prd.md`

## Issue 1: 搭建每日串行 Agent 工作流

**类型**：feature

**标签**：`workflow`, `agent`, `scheduler`

**背景**

PRD 要求每天 UTC 0:00 触发一次流程，并按 `collector -> analyzer -> organizer` 串行执行。

**任务**

- 定义每日 UTC 0:00 的触发入口。
- 编排 `collector`、`analyzer`、`organizer` 三个步骤。
- 确保下游只在上游成功或达到可继续条件时执行。
- 记录每次运行的开始时间、结束时间、状态和错误信息。

**验收标准**

- 可以手动触发一次完整流程。
- 可以配置或模拟 UTC 0:00 定时触发。
- 执行顺序固定为 `collector -> analyzer -> organizer`。
- 任一阶段失败时，运行状态可被追踪。

## Issue 2: 实现 collector Agent 采集与初筛

**类型**：feature

**标签**：`collector`, `github-trending`, `ai-filter`

**背景**

collector 负责抓取 GitHub Trending Top 50，过滤 AI 相关项目，并将原始结果写入 `knowledge/raw/`。

**任务**

- 抓取 GitHub Trending Top 50。
- 提取 repo 名称、URL、描述、语言、stars、forks、trending 排名等基础信息。
- 按 AI/LLM/Agent 相关规则进行初筛。
- 将采集原始数据保存到 `knowledge/raw/`。
- 保留被过滤项目的过滤原因，便于排查。

**验收标准**

- 一次运行最多处理 GitHub Trending Top 50。
- AI 相关项目被写入 `knowledge/raw/`。
- 非 AI 相关项目不会进入后续分析输入。
- 原始数据 JSON 可解析，并保留来源和采集时间。

## Issue 3: 实现 analyzer Agent 三维度标签分析

**类型**：feature

**标签**：`analyzer`, `tagging`, `llm`

**背景**

analyzer 读取 `knowledge/raw/`，对每条数据打 3 个维度的标签。

**任务**

- 读取 collector 产出的 raw 数据。
- 定义 3 个标签维度。
- 为每条 AI 相关动态生成结构化标签。
- 输出分析结果，供 organizer 消费。
- 对无法判断的条目标记原因，而不是静默丢弃。

**推荐标签维度**

- `domain`：技术领域，如 `LLM`、`Agent`、`RAG`、`Eval`、`Inference`。
- `artifact_type`：内容类型，如 `repo`、`tool`、`framework`、`paper`、`discussion`。
- `maturity`：成熟度，如 `experimental`、`usable`、`production_ready`、`unknown`。

**验收标准**

- 每条输入数据都有 3 个维度的标签。
- 标签输出为结构化 JSON。
- 标签缺失或不确定时有明确原因。
- analyzer 不直接生成最终 Markdown。

## Issue 4: 实现 organizer Agent 整理成 Markdown

**类型**：feature

**标签**：`organizer`, `markdown`, `knowledge`

**背景**

organizer 读取已标注数据，整理成 Markdown 文章或日报。

**任务**

- 读取 analyzer 输出的已标注数据。
- 按领域、类型或重要性组织内容。
- 生成 Markdown 文档。
- 保留每条动态的标题、来源链接、摘要、标签和风险提示。
- 输出到约定目录。

**验收标准**

- Markdown 可读，结构稳定。
- 每条内容都能追溯到原始来源。
- 不包含未经过 analyzer 标注的数据。
- 生成失败时不覆盖已有有效结果。

## Issue 5: 明确上游失败时的下游处理策略

**类型**：decision

**标签**：`failure-handling`, `workflow`

**背景**

PRD 开放问题：上游失败下游怎么办？

**推荐方案**

采用阶段状态机：

- `collector` 失败：停止本次流程，不执行 analyzer 和 organizer。
- `analyzer` 部分失败：允许 organizer 处理成功分析的条目，同时记录失败条目。
- `analyzer` 全部失败：停止 organizer。
- `organizer` 失败：保留 raw 和分析结果，允许重跑 organizer。

**任务**

- 定义每个阶段的成功、部分成功、失败状态。
- 定义每种状态下是否继续下游。
- 将失败原因写入运行记录。

**验收标准**

- 每个阶段失败都有明确处理分支。
- 不会因为局部失败导致已成功数据丢失。
- 下游不会消费不完整或不可解析的数据。

## Issue 6: 明确 Agent 间数据传递方式

**类型**：decision

**标签**：`data-contract`, `storage`

**背景**

PRD 开放问题：数据怎么传？文件 or 消息？

**推荐方案**

v0.1 使用文件传递，降低系统复杂度：

- collector 输出到 `knowledge/raw/`。
- analyzer 读取 raw，并输出结构化分析 JSON。
- organizer 读取分析 JSON，并生成 Markdown。

**任务**

- 定义 collector 输出 JSON schema。
- 定义 analyzer 输出 JSON schema。
- 定义 organizer 输入和输出路径。
- 确定文件命名规则。

**验收标准**

- 三个 Agent 之间没有隐式内存依赖。
- 单个阶段可以独立重跑。
- 所有中间产物都可被人工检查。

## Issue 7: 设计重跑策略

**类型**：decision

**标签**：`retry`, `rerun`, `idempotency`

**背景**

PRD 开放问题：重跑策略？

**推荐方案**

按阶段支持幂等重跑：

- 同一天 collector 重跑时覆盖或生成新版本 raw 文件，但必须保留可追溯记录。
- analyzer 可针对单条或整批 raw 重跑。
- organizer 可基于最新 analyzer 输出重新生成 Markdown。

**任务**

- 定义运行 ID 或日期分区。
- 定义重复采集同一 repo 时的去重规则。
- 定义覆盖、追加和归档策略。
- 定义失败后从哪个阶段恢复。

**验收标准**

- 重跑不会产生重复知识条目。
- 重跑不会删除原始采集证据。
- 可以只重跑 analyzer 或 organizer。

## Issue 8: 实现进度追踪与运行记录

**类型**：feature

**标签**：`observability`, `progress`, `logging`

**背景**

PRD 开放问题：进度追踪？

**任务**

- 为每次流程运行生成运行记录。
- 记录每个 Agent 的状态、输入数量、输出数量、失败数量。
- 记录错误原因和可重试信息。
- 提供简单的本地状态文件，便于人工查看。

**推荐状态字段**

- `run_id`
- `started_at`
- `finished_at`
- `status`
- `collector_status`
- `analyzer_status`
- `organizer_status`
- `input_count`
- `accepted_count`
- `failed_count`
- `error_message`

**验收标准**

- 可以看出当前流程执行到哪一步。
- 可以看出每个阶段处理了多少条数据。
- 失败时可以定位失败阶段和原因。

## Issue 9: 对齐 AGENTS.md 与 PRD 的范围差异

**类型**：chore

**标签**：`docs`, `spec`

**背景**

当前 `AGENTS.md` 描述了 GitHub Trending 和 Hacker News、多渠道分发、JSON 知识库等更完整范围；`specs/agents-prd.md` 当前只描述三 Agent 串行流程，并要求 organizer 输出 Markdown。

**任务**

- 明确 `specs/agents-prd.md` 是否要继承 `AGENTS.md` 中的 Hacker News 和 Telegram/飞书分发范围。
- 明确 organizer 的最终输出是 Markdown、JSON，还是 JSON 派生 Markdown。
- 更新 PRD，使流程目标、数据格式和分发目标一致。

**验收标准**

- `AGENTS.md` 与 `specs/agents-prd.md` 不再互相冲突。
- 后续 issue 可以基于一致的范围继续拆分。
