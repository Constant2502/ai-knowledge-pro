---
name: github-trending
description: 当需要采集 GitHub 热门开源项目时使用此技能
allowed-tools: Read, Grep, Glob, WebFetch
---

# GitHub Trending 采集技能

## 使用场景

当需要采集 GitHub 热门开源项目，并从中筛选 AI、LLM、Agent 相关项目时，使用本技能。本技能适用于生成每日 GitHub 热门项目原始知识数据，输出结果写入 `knowledge/raw/`。

## 执行步骤

1. 搜索热门仓库：通过 GitHub API 获取当日热门开源项目候选列表。
2. 提取信息：提取每个仓库的项目名、URL、描述、Star 数、主要语言、topics 等基础信息。
3. 过滤项目：纳入 AI、LLM、Agent 相关项目，排除 Awesome 列表、资源合集、导航站和非技术项目。
4. 去重：按仓库 URL 或 `owner/repo` 去重，避免同一项目重复进入结果。
5. 撰写中文摘要：按“项目名 + 做什么 + 为什么值得关注”的公式生成简明中文摘要。
6. 排序取 Top15：按热度、相关性和信息完整度排序，保留 Top15。
7. 输出 JSON：将结果写入 `knowledge/raw/github-trending-YYYY-MM-DD.json`。

## 注意事项

- 不要把 Awesome 列表、资料合集、导航项目纳入最终 `items`。
- 不要只根据项目名判断相关性，应综合描述、topics、README 摘要或 GitHub API 返回信息。
- `summary` 必须是中文，并包含项目名、项目用途和关注理由。
- `items` 最多保留 15 条。
- 输出 JSON 必须可被程序解析。
- 不要在输出中包含 API Key、Token、Cookie 或其他敏感信息。
- 如果 GitHub API 请求失败，应记录失败原因，不要生成伪造数据。

## 输出格式

输出文件路径：

```text
knowledge/raw/github-trending-YYYY-MM-DD.json
```

JSON 结构：

```json
{
  "source": "github_trending",
  "skill": "github-trending",
  "collected_at": "2026-05-23T00:00:00Z",
  "items": [
    {
      "name": "owner/repo",
      "url": "https://github.com/owner/repo",
      "summary": "owner/repo 是一个用于构建 AI Agent 工作流的开源项目，值得关注是因为它在 GitHub 热门项目中增长明显且覆盖实际 Agent 工程场景。",
      "stars": 12345,
      "language": "Python",
      "topics": ["ai", "llm", "agent"]
    }
  ]
}
```
