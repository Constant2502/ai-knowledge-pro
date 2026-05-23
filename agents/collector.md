# collector Agent

来源：`specs/agents-issues.md` 的 Issue 2。

## 目标

抓取 GitHub Trending Top 50，过滤 AI 相关项目，并将可供后续分析的原始结果写入 `knowledge/raw/`。

## 输入

- GitHub Trending 页面或等价数据源。
- 本次运行日期和运行 ID。

## 输出

- 目录：`knowledge/raw/`
- 格式：JSON
- 内容：AI 相关候选项目及必要元数据。

推荐文件名：

```text
knowledge/raw/{run_id}-github-trending.json
```

## 输出字段

```json
{
  "run_id": "2026-05-23-000000",
  "source": "github_trending",
  "collected_at": "2026-05-23T00:00:00Z",
  "items": [
    {
      "repo_name": "owner/repo",
      "repo_url": "https://github.com/owner/repo",
      "description": "GitHub 原始描述",
      "language": "Python",
      "stars": 12345,
      "forks": 678,
      "trending_rank": 1,
      "ai_relevance": true,
      "ai_relevance_reason": "命中 LLM、Agent 或 RAG 等 AI 相关信号"
    }
  ],
  "filtered_items": [
    {
      "repo_name": "owner/repo",
      "repo_url": "https://github.com/owner/repo",
      "trending_rank": 2,
      "filter_reason": "未发现 AI/LLM/Agent 相关信号"
    }
  ]
}
```

## 任务

- 抓取 GitHub Trending Top 50。
- 提取 repo 名称、URL、描述、语言、stars、forks、trending 排名等基础信息。
- 按 AI/LLM/Agent 相关规则进行初筛。
- 将 AI 相关项目写入 `items`。
- 将被过滤项目写入 `filtered_items` 并保留过滤原因。

## 验收标准

- 一次运行最多处理 GitHub Trending Top 50。
- AI 相关项目被写入 `knowledge/raw/`。
- 非 AI 相关项目不会进入 analyzer 的分析输入。
- 原始数据 JSON 可解析，并保留来源和采集时间。

## 边界

- 不做深度标签分析。
- 不生成 Markdown。
- 不静默丢弃无法处理的数据，必须记录原因。
