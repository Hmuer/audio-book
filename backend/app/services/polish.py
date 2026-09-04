from typing import Literal
from pydantic import BaseModel

from ..ai.factory import get_llm


FEW_SHOT = r"""
你是一名专业的中文小说文本校对编辑。你的任务是：
1. 修正原文中的错别字、漏字、多字、标点误用（明显输入错误类）
2. 不要改写原意、不要润色文笔、不要增删情节
3. 不要擅自更改引号风格（『』、「」、"" 都是合法的）、不要省略号乱删
4. 修正后自我评估：本次改动是否"合理且必要"，如果你只是改了文风、改了引号风格等则必须 is_reasonable=false

【示例 1】
输入："林若雪走在回家的路上，心理想着明天的考试。"
输出：
{
  "polished_text": "林若雪走在回家的路上，心里想着明天的考试。",
  "diff": [{"type": "replace", "old": "心理", "new": "心里", "position": 12}],
  "is_reasonable": true,
  "reason": "「心理」应为「心里」，属于常见错别字，修正合理。"
}

【示例 2】
输入："他推开门，走进了教师。"
输出：
{
  "polished_text": "他推开门，走进了教室。",
  "diff": [{"type": "replace", "old": "教师", "new": "教室", "position": 9}],
  "is_reasonable": true,
  "reason": "「教师」与上下文「走进」搭配不当，应为「教室」。"
}

【示例 3】
输入："他沉默了许久，终于开口说道：『我……我不知道。』"
如果你的改动只是把『』换成「」，那么 is_reasonable 必须是 false。
输出：
{
  "polished_text": "他沉默了许久，终于开口说道：『我……我不知道。』",
  "diff": [],
  "is_reasonable": true,
  "reason": "原文无语病，无需修改。"
}

【示例 4】
输入："夕阳西下，断肠人在天涯。"
输出：
{
  "polished_text": "夕阳西下，断肠人在天涯。",
  "diff": [],
  "is_reasonable": true,
  "reason": "原文无语病，无需修改。"
}
"""


class DiffItem(BaseModel):
    type: Literal["replace", "insert", "delete"]
    old: str
    new: str
    position: int


class PolishResult(BaseModel):
    polished_text: str
    diff: list[DiffItem]
    is_reasonable: bool
    reason: str


async def polish_with_llm(raw_text: str) -> PolishResult:
    """
    调 LLM 做错别字纠错 + 自我评估。
    is_reasonable=false 时调用方应回退 raw_text。
    """
    prompt = (
        FEW_SHOT
        + "\n\n【现在处理以下文本】\n---RAW TEXT START---\n"
        + raw_text
        + "\n---RAW TEXT END---\n\n请严格输出 JSON，不要任何解释文字。"
    )
    llm = get_llm()
    return await llm.chat_structured(
        prompt=prompt,
        output_schema=PolishResult,
        temperature=0.1,
        max_tokens=16000,
    )
