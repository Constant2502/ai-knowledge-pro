# AI 知识库 · 项目愿景 v1.0

## 要做什么
- 每天抓取 GitHub Trending Top 10，并只保留 AI 相关项目。
- 用 Agent 分析每个入选项目，输出结构化判断。
- 以 JSON 作为知识库唯一主产物，后续可以基于 JSON 渲染 Markdown、日报、分发消息或其他展示形态。

## 数据分层

- `knowledge/raw/`：原始采集证据，保留来源、标题、链接、采集时间、原始描述和过滤依据。
- `knowledge/analyzed/`：分析中间结果，保留 AI 相关性判断、三维度标签、评分、失败原因和趋势归纳。
- `knowledge/articles/`：最终标准化 article JSON，是检索、分发和报告生成的唯一权威数据源。
- `knowledge/reports/`：从 article JSON 派生的人读 Markdown、日报、周报或分发预览。

## AI 相关判定
- repo 描述、README、topics 或项目名命中 AI、LLM、Agent、RAG、model、inference、fine-tuning、eval、embedding、vector 等关键词。
- 或者项目明显服务于 AI 开发、推理、数据处理、模型使用、Agent 工作流、MCP、评测或知识库场景。
- 不相关的前端框架、通用 CLI、数据库、游戏、系统库等，即使进入 GitHub Trending Top 10，也不进入知识库。

## Agent 分析内容
每个入选 repo 需要输出以下 6 类分析：

- 一句话定位：这个项目是什么，用来解决什么问题。
- 核心能力：它提供哪些关键功能或技术能力。
- 适用场景：谁会用它，用在什么工作流里。
- 技术关键词：如 Agent、RAG、LLM inference、MCP、embedding、eval、fine-tuning 等。
- 关注理由：为什么它今天值得进入知识库，不能只是“上了 Trending”。
- 风险/不确定性：如文档不完整、维护风险、概念炒作、依赖重、商业化不清晰等。

## 知识条目格式
每个最终知识条目输出为 JSON 对象，并写入 `knowledge/articles/`。必填字段如下：

```json
{
  "id": "2026-05-21-github-trending-owner-repo",
  "title": "项目或文章标题",
  "source": "github_trending",
  "source_url": "https://github.com/owner/repo",
  "source_type": "repository",
  "published_at": "2026-05-21",
  "collected_at": "2026-05-21T10:00:00+08:00",
  "summary": "一句话说明这个动态是什么，以及为什么值得关注。",
  "content": "整理后的正文或结构化说明。",
  "tags": ["AI", "LLM", "Agent"],
  "status": "draft",
  "ai_relevance": true,
  "ai_relevance_reason": "为什么判定为 AI 相关",
  "key_points": ["关键点 1", "关键点 2"],
  "risks": ["风险或不确定性 1"],
  "distribution_channels": ["telegram", "feishu"]
}
```

允许保留 `score`、`score_reason`、`metadata` 等扩展字段，但扩展字段不能替代标准必填字段。

## 不做什么
- v1.0 不做泛化的 GitHub Trending 摘要，只关注 AI 相关项目。
- v1.0 不把评分作为必填主字段；`score` 只能作为可选辅助质量信号，避免在样本不足时制造伪精确分数。
- v1.0 不把人读 Markdown 作为主产物；Markdown、日报、页面展示都应从 JSON 派生并写入 `knowledge/reports/`。
- v1.0 不承诺覆盖所有 AI 开源项目，只处理当天 GitHub Trending Top 10 中符合条件的项目。

## 边界 & 验收
- 每天最多处理 GitHub Trending Top 10。
- 非 AI 相关项目必须被过滤，并给出过滤依据或不进入最终输出。
- 每个入库项目必须包含完整 JSON 字段。
- Agent 输出必须包含 6 类分析，不允许只给项目简介。
- `ai_relevance_reason` 和 `summary` 必须是具体判断，不能只写“AI 相关”或“上了 Trending”。
- 输出 JSON 必须可被程序解析。

## 怎么验证
- 用某一天的 GitHub Trending Top 10 跑一次完整流程。
- 检查过滤后留下的项目是否都符合 AI 相关判定。
- 检查每个知识条目是否包含完整字段，且 JSON 可解析。
- 抽查 Agent 分析是否能回答三个问题：这个项目是什么、为什么值得关注、有什么风险。
- 确认没有非 AI 相关项目进入最终知识库。
