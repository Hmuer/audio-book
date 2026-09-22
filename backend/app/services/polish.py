from pydantic import BaseModel

from ..ai.factory import get_llm


FEW_SHOT = r"""
你是一名专业的中文小说文本校对编辑。你的任务是：
1. 修正原文中的错别字、漏字、多字、标点误用（明显输入错误类）
2. 不要改写原意、不要润色文笔、不要增删情节
3. 不要擅自更改引号风格（『』、「」、"" 都是合法的）、不要省略号乱删
4. 修正后自我评估：本次改动是否"合理且必要"，如果你只是改了文风、改了引号风格等则必须 is_reasonable=false
5. 只输出**一个** JSON 对象（以 { 开始、以 } 结束），且只包含 polished_text / is_reasonable / reason 三个字段。
   不要输出数组，不要只输出某个字段的内容，不要输出任何解释文字或 markdown 代码块。

【示例 1】
输入："林若雪走在回家的路上，心理想着明天的考试。"
输出：
{
  "polished_text": "林若雪走在回家的路上，心里想着明天的考试。",
  "is_reasonable": true,
  "reason": "「心理」应为「心里」，属于常见错别字，修正合理。"
}

【示例 2】
输入："他推开门，走进了教师。"
输出：
{
  "polished_text": "他推开门，走进了教室。",
  "is_reasonable": true,
  "reason": "「教师」与上下文「走进」搭配不当，应为「教室」。"
}

【示例 3】
输入："他沉默了许久，终于开口说道：『我……我不知道。』"
如果你的改动只是把『』换成「」，那么 is_reasonable 必须是 false。
输出：
{
  "polished_text": "他沉默了许久，终于开口说道：『我……我不知道。』",
  "is_reasonable": true,
  "reason": "原文无语病，无需修改。"
}

【示例 4】
输入："夕阳西下，断肠人在天涯。"
输出：
{
  "polished_text": "夕阳西下，断肠人在天涯。",
  "is_reasonable": true,
  "reason": "原文无语病，无需修改。"
}
"""


class PolishResult(BaseModel):
    """润色结果。

    字段设计说明：早期版本还有 diff: list[DiffItem]，但下游从未消费它；
    它既是 required、形状又是数组，模型偶尔会把「这个数组」当成整个答案返回
    （顶层回数组 → Pydantic 校验失败 → 白跑一次重试、白烧 token）。故移除。
    reason 保留用于排查（is_reasonable=false 时说明理由），给默认值以降低校验失败面。
    """
    polished_text: str
    is_reasonable: bool
    reason: str = ""


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
    result = await llm.chat_structured(
        prompt=prompt,
        output_schema=PolishResult,
        temperature=0.1,
        max_tokens=16000,
    )
    from .usage import track_llm
    track_llm(calls=1, chars=len(prompt), detail="polish")
    return result
