from __future__ import annotations
import itertools
from pydantic import BaseModel

from ..ai.factory import get_llm


EXTRACT_FEW_SHOT = r"""
你是一名小说人物分析师。请从给定小说文本中提取所有出场的角色（主角、配角、只提过一次的路人都算）。
每个角色提取：姓名、性别、年龄段、性格描述。
规则：
- 性别只填「男」「女」「未知」三选一
- 只出现"少年""少女"但无姓名的，如果上下文暗示是有台词的重要角色也提取，name 填「少年A」「少女1」这类占位
- 不要提取"天""风""雨""天空""街道"等非人物
- personality 用 2-4 个关键词或一句话

【示例 1】
输入：
"林若雪今年 17 岁，是个内向的高二女生。她的同桌李明性格开朗，总爱逗她笑。
街角卖冰棍的王大爷认识她们俩，每次都会多给一勺。"

输出：
[
  {"name": "林若雪", "gender": "女", "age": "17岁/高中生", "personality": "内向、安静"},
  {"name": "李明", "gender": "男", "age": "高中生", "personality": "开朗、爱开玩笑"},
  {"name": "王大爷", "gender": "男", "age": "老年", "personality": "热情、友善"}
]

【示例 2】
输入：
"一个少女站在悬崖边，她穿着白衣，手里攥着一把剑。身后的少年道：「若雪，别去。」她回头笑了笑，纵身一跃。"

输出：
[
  {"name": "林若雪", "gender": "女", "age": "少女", "personality": "刚烈、决绝"},
  {"name": "少年A", "gender": "男", "age": "少年", "personality": "关心同伴、担忧"}
]
"""


DEDUP_FEW_SHOT = r"""
你是一名小说文本实体消歧专家。给定小说上下文和一个角色名字列表，判断列表中哪些名字指代的是同一个人。
两两组合全部输出。
规则：
- 「若雪」和「林若雪」在上下文支持时可能是同一人（全名/简称）
- 「王大爷」和「王师傅」默认不是同一人
- 姓相同但名完全不同（如「张伟」「张强」）默认不是同一人，除非上下文明确
- canonical_name 选最完整/出现字数最多的那个

【示例】
上下文："林若雪推开门，妈妈在厨房喊：若雪，过来吃饭。"
名字列表：["林若雪", "若雪", "妈妈"]
输出：
[
  {"name_a": "林若雪", "name_b": "若雪", "same_person": true, "canonical_name": "林若雪"},
  {"name_a": "林若雪", "name_b": "妈妈", "same_person": false, "canonical_name": null},
  {"name_a": "若雪", "name_b": "妈妈", "same_person": false, "canonical_name": null}
]
"""


class Character(BaseModel):
    name: str
    gender: str
    age: str
    personality: str


class DedupResult(BaseModel):
    name_a: str
    name_b: str
    same_person: bool
    canonical_name: str | None


async def extract_characters_with_llm(text: str) -> list[Character]:
    """从文本中提取角色。短文本也调 LLM，不做短路。"""
    prompt = (
        EXTRACT_FEW_SHOT
        + "\n\n【现在处理以下文本】\n---TEXT START---\n"
        + text
        + "\n---TEXT END---\n\n请严格输出 JSON 数组。"
    )
    llm = get_llm()

    # 用 list[Character] 无法直接作为 Pydantic model，封装一层
    class CharacterList(BaseModel):
        items: list[Character]

    # 为了提高成功率，让 LLM 包一层 {items: [...]}
    sys = "你只能输出 JSON，结构为 {items: [Character,...]}，Character={name,gender,age,personality}。gender 只允许男/女/未知。"
    # 这里直接用 list schema 的 prompt 改造一下
    prompt = sys + "\n\n" + prompt

    class _ListWrapper(BaseModel):
        data: list[Character]

    try:
        wrapped = await llm.chat_structured(
            prompt=prompt + "\n\n⚠️输出格式必须是 {\"data\": [Character, ...]}，顶层一定要有 data 字段!",
            output_schema=_ListWrapper,
            temperature=0.2,
            max_tokens=8000,
        )
        return wrapped.data
    except Exception:
        # 兜底：再试一次纯数组 prompt，手动 parse
        fallback = await llm.chat_structured(
            prompt=prompt,
            output_schema=_ListWrapper,
            temperature=0.3,
            max_tokens=8000,
        )
        return fallback.data


async def deduplicate_characters_with_llm(
    names: list[str], context: str
) -> list[DedupResult]:
    """让 LLM 判断每对名字是否同一人。"""
    if len(names) < 2:
        return []
    import json as _json

    prompt = (
        DEDUP_FEW_SHOT
        + f"\n上下文：\"\"\"\n{context}\n\"\"\""
        + f"\n名字列表：{_json.dumps(names, ensure_ascii=False)}"
        + "\n\n请输出完整的两两组合结果数组。"
        + "\n⚠️输出格式必须是 {\"data\": [DedupResult,...]}，顶层一定要有 data 字段!"
    )

    class _Wrapper(BaseModel):
        data: list[DedupResult]

    llm = get_llm()
    wrapped = await llm.chat_structured(
        prompt=prompt,
        output_schema=_Wrapper,
        temperature=0.1,
        max_tokens=4000,
    )
    return wrapped.data


def apply_dedup(
    characters: list[Character], dedup_results: list[DedupResult]
) -> tuple[list[Character], dict[str, str]]:
    """
    根据 LLM 的去重结果合并角色。
    返回 (合并后的角色列表, {旧名字 -> canonical_name} 映射)。
    """
    # 1) 构建并查集
    name_map: dict[str, str] = {c.name: c.name for c in characters}

    def find(x: str) -> str:
        while name_map[x] != x:
            name_map[x] = name_map[name_map[x]]
            x = name_map[x]
        return x

    def union(a: str, b: str, canonical: str):
        ra, rb = find(a), find(b)
        target = canonical or rb
        # 把 ra 合并到 target
        name_map[ra] = target
        name_map[target] = target

    for r in dedup_results:
        if r.same_person and r.canonical_name:
            # 先确保两个 name 都在 map 里
            if r.name_a not in name_map:
                name_map[r.name_a] = r.name_a
            if r.name_b not in name_map:
                name_map[r.name_b] = r.name_b
            if r.canonical_name not in name_map:
                name_map[r.canonical_name] = r.canonical_name
            union(r.name_a, r.name_b, r.canonical_name)

    # 压缩路径
    for n in list(name_map.keys()):
        name_map[n] = find(n)

    # 2) 合并角色元信息
    merged: dict[str, Character] = {}
    for c in characters:
        canonical = name_map.get(c.name, c.name)
        if canonical not in merged:
            merged[canonical] = Character(
                name=canonical,
                gender=c.gender if c.gender != "未知" else "未知",
                age=c.age,
                personality=c.personality,
            )
        else:
            m = merged[canonical]
            if m.gender == "未知" and c.gender != "未知":
                m.gender = c.gender
            if not m.age and c.age:
                m.age = c.age
            if c.personality and c.personality not in m.personality:
                m.personality = (m.personality + "、" + c.personality).strip("、")
    return list(merged.values()), name_map
