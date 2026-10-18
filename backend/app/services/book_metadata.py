"""从书籍正文生成展示信息，并在当前 TTS 官方音色中选旁白。"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from ..ai.factory import get_llm
from .chapter import Chapter


BOOK_CATEGORIES = {"男频", "女频", "文学", "非虚构", "其他"}


class BookMetadata(BaseModel):
    category: str
    tags: list[str]
    selling_points: str
    description: str
    narrator_voice_id: str


def _book_samples(chapters: list[Chapter]) -> str:
    """跨全书取样；每章只取开头，避免超长正文或目录挤占上下文。"""
    if not chapters:
        return ""
    indexes = sorted({round(i * (len(chapters) - 1) / 4) for i in range(5)})
    return "\n\n".join(
        f"{chapters[i].title[:100]}\n{chapters[i].text[:1200]}" for i in indexes
    )


def _normalize_gender(value: Any) -> str:
    return {"男": "male", "男声": "male", "女": "female", "女声": "female"}.get(
        str(value or ""), str(value or "").lower()
    )


def clean_metadata(raw: BookMetadata, voices: list[dict], *, category_override: str = "") -> BookMetadata:
    """截断数据库字段并验证音色 ID；模型不能自造音色或忽略分类性别。"""
    category = category_override if category_override in BOOK_CATEGORIES else raw.category.strip()
    if category not in BOOK_CATEGORIES:
        category = "其他"
    tags: list[str] = []
    for tag in raw.tags:
        tag = str(tag).strip().strip("，,；;#")[:24]
        if tag and tag not in tags and len(tags) < 8:
            tags.append(tag)
    while len(",".join(tags)) > 256:
        tags.pop()
    valid = {str(v.get("id")): v for v in voices if v.get("id")}
    voice_id = (raw.narrator_voice_id or "").strip()
    wanted_gender = "female" if category == "女频" else "male" if category == "男频" else ""
    if voice_id not in valid or (wanted_gender and _normalize_gender(valid[voice_id].get("gender")) != wanted_gender):
        voice_id = next(
            (vid for vid, v in valid.items() if _normalize_gender(v.get("gender")) == wanted_gender),
            "",
        ) if wanted_gender else ""
    if not voice_id and valid:
        voice_id = next(iter(valid))
    return BookMetadata(
        category=category,
        tags=tags,
        selling_points=raw.selling_points.strip()[:1000],
        description=raw.description.strip()[:5000],
        narrator_voice_id=voice_id,
    )


async def generate_book_metadata(
    title: str, chapters: list[Chapter], voices: list[dict], *, category_override: str = ""
) -> BookMetadata:
    """一次结构化调用生成四项文案和旁白选择。"""
    voice_options = [
        {"id": v["id"], "name": v.get("name", ""),
         "gender": v.get("gender", ""), "description": str(v.get("description", ""))[:70]}
        for v in voices if v.get("id")
    ]
    prompt = (
        "你是中文有声书编辑。根据书名和跨章节正文样本，生成真实、简洁的书籍资料。"
        "不要编造作者、奖项或样本中没有依据的情节；不透露重大结局。"
        "category 只能是男频、女频、文学、非虚构、其他之一；男频/女频只用于受众明确的网络小说。"
        "tags 输出 3 到 6 个中文短标签；selling_points 用一到两句话说明具体吸引点；"
        "description 用 150 到 300 字介绍内容，把核心信息放在前 150 字，适合详情页外显。"
        "narrator_voice_id 必须从给定的当前 TTS 官方音色里选一个适合本书气质的 ID；"
        "男频选男声、女频选女声，其余类型按作品气质决定。没有候选音色时输出空串。"
        "只返回 JSON，字段为 category、tags 数组、selling_points、description、narrator_voice_id。\n"
        f"书名：{title[:150]}\n"
        f"已有人工分类（必须沿用）：{category_override if category_override in BOOK_CATEGORIES else '无'}\n"
        f"正文样本：\n{_book_samples(chapters)}\n"
        f"官方音色：{json.dumps(voice_options, ensure_ascii=False)}"
    )
    raw = await get_llm().chat_structured(
        prompt, BookMetadata, max_tokens=1500, use_fast_model=True, max_retries=2,
    )
    return clean_metadata(raw, voices, category_override=category_override)
