from __future__ import annotations
import itertools
from pydantic import BaseModel

from ..ai.factory import get_llm


EXTRACT_FEW_SHOT = r"""
你是一名小说人物分析师。请从给定小说文本中提取所有出场的**独立角色**（主角、配角、只提过一次的路人都算）。
每个角色提取：姓名、性别、年龄段、性格描述。
规则：
- 性别只填「男」「女」「未知」三选一
- 只出现"少年""少女"但无姓名的，如果上下文暗示是有台词的重要角色也提取，name 填「少年A」「少女1」这类占位
- 不要提取"天""风""雨""天空""街道"等非人物
- personality 用 2-4 个关键词或一句话
- ⚠️ **严禁**将以下类型的称呼当作独立角色提取：
  · 尊称/头衔：如「师尊」「师父」「长老」「前辈」「阁主」「宗主」「师兄」「师姐」「师叔」「道友」等——这些是对已出场角色的称呼，不是新角色
  · 昵称/小名：如「燕儿」「雪儿」「灵儿」「阿明」——如果文中已有对应全名（如「周燕」「林若雪」），昵称指向同一人，不单独提取
  · 代词：「他」「她」「我」「你」「众人」「几位」等
- 判断标准：该称呼是否可以用"人名"替换而句意不变？若可以则不提取

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

【示例 3】—— 尊称/昵称处理
输入：
"师尊，您的伤不要紧吧！周燕扶起受伤的张羽。燕儿说：「师父，别担心。」"

输出：
[
  {"name": "周燕", "gender": "女", "age": "青年", "personality": "关心师长、细心"},
  {"name": "张羽", "gender": "男", "age": "中年", "personality": "受伤、虚弱"}
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
- ⚠️ 尊称/头衔/昵称的消歧：
  · 「师尊」「师父」「长老」「前辈」「阁主」「宗主」「师兄」「师姐」等——如果上下文中这些称呼明确指向某个已出场角色（如「师尊」只对应张羽一人），应判定为同一人
  · 「燕儿」「雪儿」「灵儿」等昵称——如果上下文中有对应全名（如「周燕」），应判定为同一人，canonical_name 取全名
  · 判断依据：称呼是否唯一指向某角色、且替换后句意不变

【示例 1】
上下文："林若雪推开门，妈妈在厨房喊：若雪，过来吃饭。"
名字列表：["林若雪", "若雪", "妈妈"]
输出：
[
  {"name_a": "林若雪", "name_b": "若雪", "same_person": true, "canonical_name": "林若雪"},
  {"name_a": "林若雪", "name_b": "妈妈", "same_person": false, "canonical_name": null},
  {"name_a": "若雪", "name_b": "妈妈", "same_person": false, "canonical_name": null}
]

【示例 2】—— 尊称/昵称消歧
上下文："师尊，您的伤不要紧吧！周燕扶起受伤的张羽。燕儿说：「师父，别担心。」"
名字列表：["张羽", "师尊", "周燕", "燕儿", "师父"]
输出：
[
  {"name_a": "张羽", "name_b": "师尊", "same_person": true, "canonical_name": "张羽"},
  {"name_a": "张羽", "name_b": "周燕", "same_person": false, "canonical_name": null},
  {"name_a": "张羽", "name_b": "燕儿", "same_person": false, "canonical_name": null},
  {"name_a": "张羽", "name_b": "师父", "same_person": true, "canonical_name": "张羽"},
  {"name_a": "师尊", "name_b": "周燕", "same_person": false, "canonical_name": null},
  {"name_a": "师尊", "name_b": "燕儿", "same_person": false, "canonical_name": null},
  {"name_a": "师尊", "name_b": "师父", "same_person": true, "canonical_name": "张羽"},
  {"name_a": "周燕", "name_b": "燕儿", "same_person": true, "canonical_name": "周燕"},
  {"name_a": "周燕", "name_b": "师父", "same_person": false, "canonical_name": null},
  {"name_a": "燕儿", "name_b": "师父", "same_person": false, "canonical_name": null}
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
        target = find(canonical)
        # 将 ra 和 rb 中不是 target 的一方合并到 target
        for root in (ra, rb):
            if root != target:
                name_map[root] = target
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

    # 2) 兜底：自动合并常见尊称/昵称到全名（LLM 可能遗漏时的代码层补救）
    _HONORIFICS = {
        "师尊", "师父", "师傅", "长老", "前辈", "阁主", "宗主", "帮主",
        "师兄", "师姐", "师弟", "师妹", "师叔", "师伯", "道友", "公子",
        "姑娘", "小姐", "少爷", "老爷", "夫人", "太太",
    }
    _KINSHIP = {
        "父亲", "母亲", "爸爸", "妈妈", "爹", "娘",
        "哥哥", "姐姐", "弟弟", "妹妹",
        "爷爷", "奶奶", "外公", "外婆",
        "叔叔", "阿姨", "舅舅", "姑姑",
        "儿子", "女儿", "孙子", "孙女",
        "侄子", "侄女", "外甥", "外甥女",
    }
    _ALL_HONOR = _HONORIFICS | _KINSHIP
    all_names = list(name_map.keys())

    # 2a) 昵称兜底：去后缀后匹配全名
    for name in all_names:
        root = find(name)
        if root == name and name not in _ALL_HONOR:
            for suffix in ("儿", "子"):
                if len(name) > 2 and name.endswith(suffix):
                    base = name[:-1]
                    if base in name_map:
                        union(name, base, base)
                        break

    # 2b) 尊称兜底：收集未被合并的尊称，按性别匹配到唯一候选正式角色
    all_names = list(name_map.keys())
    honorif_left = [n for n in all_names if find(n) == n and n in _ALL_HONOR]
    for hname in honorif_left:
        real_roots = set()
        for n in all_names:
            r = find(n)
            if r not in _ALL_HONOR:
                real_roots.add(r)
        if not real_roots:
            continue
        if len(real_roots) == 1:
            target = next(iter(real_roots))
            union(hname, target, target)
        else:
            h_char = next((c for c in characters if c.name == hname), None)
            candidates = []
            for rr in real_roots:
                rc = next((c for c in characters if c.name == rr), None)
                if not rc:
                    continue
                if h_char and rc.gender == h_char.gender:
                    candidates.append(rr)
            if len(candidates) == 1:
                target = candidates[0]
                union(hname, target, target)

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
