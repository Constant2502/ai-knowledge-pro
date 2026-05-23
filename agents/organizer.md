# organizer Agent

来源：`specs/agents-issues.md` 的 Issue 4。

## 目标

读取 analyzer 输出的已标注数据，整理成结构稳定、可追溯的 Markdown 文章或日报。

## 输入

- `knowledge/analyzed/` 中 analyzer 生成的 JSON。

## 输出

- 建议目录：`knowledge/articles/`
- 格式：Markdown
- 内容：按领域、类型或重要性组织后的技术动态文章。

推荐文件名：

```text
knowledge/articles/{run_id}-daily-ai-trends.md
```

## Markdown 结构

```markdown
# AI 技术动态日报

## 摘要

## 按领域整理

### Agent

- [标题](来源链接)
  - 摘要：
  - 标签：
  - 关键点：
  - 风险：
```

## 任务

- 读取 analyzer 输出的已标注数据。
- 按领域、类型或重要性组织内容。
- 生成 Markdown 文档。
- 保留每条动态的标题、来源链接、摘要、标签和风险提示。
- 输出到约定目录。
- 生成失败时保留已有有效结果，不覆盖已有文件。

## 验收标准

- Markdown 可读，结构稳定。
- 每条内容都能追溯到原始来源。
- 不包含未经过 analyzer 标注的数据。
- 生成失败时不覆盖已有有效结果。

## 边界

- 不抓取 GitHub Trending。
- 不重新分析未标注数据。
- 不覆盖已有有效结果。
