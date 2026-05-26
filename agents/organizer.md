# organizer Agent

来源：`specs/agents-issues.md` 的 Issue 4。

## 目标

读取 analyzer 输出的已标注数据，整理成结构稳定、可追溯、可校验的最终 article JSON。

JSON 是知识库唯一主产物。Markdown、日报、周报和分发预览必须从 article JSON 派生，输出到 `knowledge/reports/`。

## 输入

- `knowledge/analyzed/` 中 analyzer 生成的 JSON。

## 输出

- 目录：`knowledge/articles/`
- 格式：JSON
- 内容：最终标准化知识条目。

推荐文件名：

```text
knowledge/articles/{date}-{source}-{slug}.json
```

## Article JSON 结构

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

## 任务

- 读取 analyzer 输出的已标注数据。
- 按 `source_url`、稳定 ID 或来源唯一键去重。
- 补齐 article 必填字段。
- 将新条目状态初始化为 `draft`。
- 设置计划分发渠道，但不直接执行正式分发。
- 入库前运行 schema 校验。
- 输出到 `knowledge/articles/`。
- 生成失败时保留已有有效结果，不覆盖已有文件。

## 验收标准

- 每个输出文件都是可解析 JSON object。
- 每个 article 都符合最终 article schema。
- 每条内容都能追溯到原始来源。
- 不包含未经过 analyzer 标注的数据。
- 生成失败时不覆盖已有有效结果。

## 边界

- 不抓取 GitHub Trending。
- 不重新分析未标注数据。
- 不直接生成 Markdown、日报或分发内容。
- 不覆盖已有有效结果。
