# AGENTS.md

本文件是 AI 知识库项目的 Agent 工作规约。所有 Agent 在本仓库内工作时，都必须遵守本文档约定。

## 项目概述

本项目自动从 GitHub Trending 和 Hacker News 采集 AI、LLM、Agent 领域的技术动态，经由 AI Agent 分析、筛选、整理后，以结构化 JSON 形式沉淀到本地知识库，并支持通过 Telegram、飞书等渠道进行分发。

## 技术栈

- Python 3：主要开发语言。
- Codex：项目内编码、分析、重构和自动化协作入口。
- LangGraph：用于编排采集、分析、整理、分发等 Agent 工作流。
- OpenClaw：用于实现网页采集、内容抽取或外部数据抓取能力。

## 编码规范

- Python 代码必须遵守 PEP 8。
- 变量名、函数名、模块名使用 `snake_case`。
- 类名使用 `PascalCase`。
- 常量使用 `UPPER_SNAKE_CASE`。
- 公共函数、类、模块应使用 Google 风格 docstring。
- 禁止使用裸 `print()` 输出日志、调试信息或运行状态。
- 日志必须使用标准 `logging` 模块或项目封装的日志工具。
- 新增代码应保持小函数、清晰职责和可测试性。
- 不得把 API Key、Token、Webhook、Cookie 等敏感信息写入代码或提交到仓库。

## 项目结构

项目目录按以下职责组织：

```text
.
├── AGENTS.md
├── agents/
│   ├── collectors/
│   ├── analyzers/
│   └── organizers/
├── skills/
├── knowledge/
│   ├── raw/
│   └── articles/
└── specs/
```

目录约定：

- `agents/`：存放 Agent 角色实现、工作流节点和编排逻辑。
- `agents/collectors/`：采集 Agent，负责 GitHub Trending、Hacker News 等来源的数据采集。
- `agents/analyzers/`：分析 Agent，负责 AI/LLM/Agent 相关性判断、摘要、标签和风险分析。
- `agents/organizers/`：整理 Agent，负责去重、归档、结构化校验和分发前整理。
- `skills/`：存放项目内可复用技能、提示词模板、工具说明或 Agent 能力说明。
- `knowledge/raw/`：存放原始采集数据，保留来源、时间、原始标题、链接和未清洗内容。
- `knowledge/articles/`：存放经过分析和整理后的结构化知识条目 JSON。
- `specs/`：存放产品规格、项目愿景和需求文档。

## 知识条目 JSON 格式

经过整理后写入 `knowledge/articles/` 的知识条目必须是可解析的 JSON。推荐一个文件保存一个条目，文件名应稳定、可追溯，例如 `{date}-{source}-{slug}.json`。

知识条目必须包含以下字段：

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

字段要求：

- `id`：全局唯一，建议由日期、来源和 slug 组成。
- `title`：原始标题或整理后的标题。
- `source`：数据来源，例如 `github_trending` 或 `hacker_news`。
- `source_url`：原始链接。
- `source_type`：来源类型，例如 `repository`、`article`、`discussion`、`tool`。
- `published_at`：原内容发布时间；无法获取时可为空字符串。
- `collected_at`：采集时间，必须包含时区。
- `summary`：简明摘要，不能只复制原始描述。
- `content`：整理后的主体内容。
- `tags`：标签数组，至少包含一个 AI/LLM/Agent 相关标签。
- `status`：条目状态，只允许使用 `draft`、`reviewed`、`published`、`archived`。
- `ai_relevance`：进入 `knowledge/articles/` 的条目必须为 `true`。
- `ai_relevance_reason`：必须给出具体判断依据。
- `key_points`：关键点数组。
- `risks`：风险或不确定性数组。
- `distribution_channels`：计划分发渠道数组，可包含 `telegram`、`feishu`。

## Agent 角色概览

| 角色 | 目录 | 输入 | 输出 | 职责 |
| --- | --- | --- | --- | --- |
| 采集 Agent | `agents/collectors/` | GitHub Trending、Hacker News 链接或列表 | `knowledge/raw/` 中的原始 JSON | 抓取候选技术动态，保留来源、标题、链接、时间和原始内容。 |
| 分析 Agent | `agents/analyzers/` | `knowledge/raw/` 原始数据 | 带摘要、标签、相关性判断、关键点和风险的中间结果 | 判断是否属于 AI/LLM/Agent 领域，并生成结构化分析。 |
| 整理 Agent | `agents/organizers/` | 分析后的中间结果 | `knowledge/articles/` 中的最终知识条目 JSON | 去重、补齐字段、校验 JSON、维护状态，并准备 Telegram/飞书分发数据。 |

## 分发约定

- Telegram 和飞书只消费 `knowledge/articles/` 中状态合适的 JSON 条目。
- 分发内容必须从 JSON 字段生成，不得绕过知识库直接拼接未验证的原始采集内容。
- 分发前必须检查 `status`，只有 `reviewed` 或 `published` 状态的条目可以进入正式分发。
- 分发失败时不得丢弃知识条目，应记录失败原因并允许重试。

## 红线

以下操作绝对禁止：

- 禁止提交 API Key、Token、Webhook、Cookie、账号密码等敏感信息。
- 禁止使用裸 `print()` 作为日志、调试或状态输出。
- 禁止把未经 AI/LLM/Agent 相关性判断的内容写入 `knowledge/articles/`。
- 禁止把不可解析 JSON 写入 `knowledge/articles/`。
- 禁止绕过 JSON 知识库直接向 Telegram 或飞书分发原始采集内容。
- 禁止伪造来源、发布时间、采集时间或链接。
- 禁止删除 `knowledge/raw/` 中的原始采集记录，除非用户明确要求。
- 禁止在没有用户确认的情况下执行破坏性操作，例如批量删除数据、重写历史、清空知识库。
- 禁止引入与项目目标无关的大型框架、服务或复杂基础设施。
