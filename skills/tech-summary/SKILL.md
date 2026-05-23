---
name: tech-summary
description: 当需要对采集的技术内容进行深度分析总结时使用此技能
allowed-tools: Read, Grep, Glob, WebFetch
---

# 技术内容深度分析总结技能

## 使用场景

当 `knowledge/raw/` 中已有采集结果，需要对其中的技术内容进行深度分析、评分、标签建议和趋势归纳时，使用本技能。本技能适用于把原始采集数据转化为可供后续整理、分发或人工评审的结构化分析结果。

## 执行步骤

1. 读取最新采集文件：从 `knowledge/raw/` 中找到最新的采集 JSON 文件并读取内容。
2. 逐条深度分析：对每个项目生成不超过 50 字的摘要、2-3 个基于事实的技术亮点、1-10 分评分及评分理由、标签建议。
3. 趋势发现：总结本批项目中的共同主题、新概念和可能值得持续观察的方向。
4. 输出分析结果 JSON：将逐条分析和趋势发现写入结构化 JSON。

## 评分标准

- `9-10`：可能改变格局，代表明显的新方向、新范式或关键基础设施。
- `7-8`：对目标用户直接有帮助，具备较清晰的实际使用价值。
- `5-6`：值得了解，但短期内未必需要采用或跟进。
- `1-4`：可略过，相关性弱、价值不清或信息不足。

## 约束

- 每个项目的摘要必须不超过 50 字。
- 技术亮点必须用事实说话，避免空泛形容。
- 每个项目必须给出评分理由。
- 每个项目必须给出标签建议。
- 如果一批次包含 15 个项目，`9-10` 分项目不得超过 2 个。
- 不得为信息不足的项目强行打高分。
- 输出 JSON 必须可被程序解析。

## 输出格式

推荐输出文件路径：

```text
knowledge/analyzed/tech-summary-YYYY-MM-DD.json
```

JSON 结构：

```json
{
  "source_file": "knowledge/raw/github-trending-YYYY-MM-DD.json",
  "skill": "tech-summary",
  "analyzed_at": "2026-05-23T00:00:00Z",
  "items": [
    {
      "name": "owner/repo",
      "url": "https://github.com/owner/repo",
      "summary": "50字以内中文摘要",
      "technical_highlights": [
        "基于事实的技术亮点 1",
        "基于事实的技术亮点 2"
      ],
      "score": 8,
      "score_reason": "评分理由，说明为什么属于该分段。",
      "tag_suggestions": ["LLM", "Agent", "RAG"]
    }
  ],
  "trends": {
    "common_themes": ["共同主题 1", "共同主题 2"],
    "new_concepts": ["新概念 1"],
    "watch_directions": ["值得持续观察的方向 1"]
  }
}
```
