# analyzer Agent

来源：`specs/agents-issues.md` 的 Issue 3。

## 目标

读取 collector 产出的 raw 数据，对每条 AI 相关动态生成三维度标签，并输出结构化分析结果供 organizer 消费。

## 输入

- `knowledge/raw/` 中 collector 生成的 JSON。

## 输出

- 建议目录：`knowledge/analyzed/`
- 格式：JSON
- 内容：带标签、摘要和判断原因的分析结果。

推荐文件名：

```text
knowledge/analyzed/{run_id}-github-trending.json
```

## 标签维度

- `domain`：技术领域，如 `LLM`、`Agent`、`RAG`、`Eval`、`Inference`。
- `artifact_type`：内容类型，如 `repo`、`tool`、`framework`、`paper`、`discussion`。
- `maturity`：成熟度，如 `experimental`、`usable`、`production_ready`、`unknown`。

## 输出字段

```json
{
  "run_id": "2026-05-23-000000",
  "source": "github_trending",
  "analyzed_at": "2026-05-23T00:10:00Z",
  "items": [
    {
      "repo_name": "owner/repo",
      "repo_url": "https://github.com/owner/repo",
      "title": "owner/repo",
      "summary": "一句话说明该项目是什么，以及为什么值得关注。",
      "tags": {
        "domain": "Agent",
        "artifact_type": "framework",
        "maturity": "experimental"
      },
      "key_points": ["关键点 1", "关键点 2"],
      "risks": ["风险或不确定性 1"],
      "tagging_reason": "根据 README、描述或项目定位判断其属于 Agent 框架"
    }
  ],
  "failed_items": [
    {
      "repo_name": "owner/repo",
      "repo_url": "https://github.com/owner/repo",
      "failure_reason": "缺少足够信息，无法判断标签"
    }
  ]
}
```

## 任务

- 读取 collector 产出的 raw 数据。
- 为每条 AI 相关动态生成 `domain`、`artifact_type`、`maturity` 三个维度标签。
- 输出结构化 JSON，供 organizer 消费。
- 对无法判断的条目标记原因，写入 `failed_items`。
- 不直接生成最终 Markdown。

## 验收标准

- 每条成功分析的数据都有三个维度的标签。
- 标签输出为结构化 JSON。
- 标签缺失或不确定时有明确原因。
- analyzer 不直接生成最终 Markdown。

## 边界

- 不抓取 GitHub Trending。
- 不生成最终 Markdown。
- 不分析非 collector 成功输出的数据。
