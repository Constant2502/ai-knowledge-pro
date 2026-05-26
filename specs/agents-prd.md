# AI 知识库 · 三 Agent PRD v0.2

## 总流程

每天 UTC 0:00 触发一次串行流程：

```text
collector -> analyzer -> organizer
```

流程也必须支持手动触发和按阶段重跑。

## 核心决策

- JSON 是知识库唯一主产物。
- `knowledge/raw/`、`knowledge/analyzed/`、`knowledge/articles/` 三层数据都保留。
- `knowledge/articles/` 只保存最终标准化 article JSON。
- Markdown、日报、周报和分发预览都从 article JSON 派生，输出到 `knowledge/reports/`。
- article 状态只允许 `draft`、`reviewed`、`published`、`archived`。
- 正式分发只消费 `reviewed` 或 `published` 状态的 article。

## Agent 职责

| Agent | 输入 | 输出 | 职责 |
| --- | --- | --- | --- |
| collector | GitHub Trending、Hacker News 或等价来源 | `knowledge/raw/` 原始 JSON | 采集候选动态，保留原始证据和过滤原因。 |
| analyzer | `knowledge/raw/` | `knowledge/analyzed/` 中间 JSON | 判断 AI 相关性，生成标签、摘要、关键点、风险和失败原因。 |
| organizer | `knowledge/analyzed/` | `knowledge/articles/` article JSON | 去重、标准化、补齐字段、校验 JSON、维护状态和分发字段。 |

## 数据契约

### raw

`knowledge/raw/` 存放原始采集证据，保留来源、标题、链接、时间、原始描述、基础元数据和过滤原因。raw 不能直接用于正式分发。

### analyzed

`knowledge/analyzed/` 存放分析中间结果，使用独立 schema。它可以包含 `domain`、`artifact_type`、`maturity`、`score`、`score_reason`、`failed_items`、`trends` 等字段，不要求等同最终 article schema。

### articles

`knowledge/articles/` 存放最终标准化知识条目 JSON。每个 article 必须包含：

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
  "ai_relevance_reason": "说明为什么该条目属于 AI/LLM/Agent 领域。",
  "key_points": ["关键点 1", "关键点 2"],
  "risks": ["风险或不确定性 1"],
  "distribution_channels": ["telegram", "feishu"]
}
```

允许保留 `score`、`score_reason`、`metadata` 等扩展字段。

### reports

`knowledge/reports/` 存放从 article JSON 派生的 Markdown、日报、周报或分发预览。reports 不是权威数据源。

## 质量门

- article 入库前必须通过 schema 校验。
- article 入库后可以进行质量评分，用于人工审核、排序和状态升级。
- 分发前必须再次检查 JSON 合法性，并确认 `status` 为 `reviewed` 或 `published`。

## 开放问题

- 定时触发采用系统 cron、GitHub Actions 还是本地调度器？
- GitHub Trending 数据源采用页面抓取、第三方镜像还是 GitHub Search 近似替代？
- Telegram 和飞书分发模板如何设计？
