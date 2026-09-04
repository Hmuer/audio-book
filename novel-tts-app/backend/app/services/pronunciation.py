"""
发音规则（pronunciation rules）—— alias / regex 替换。

从 services/project.py 拆出，保持原 API 与 import 路径兼容：
- PronunciationRule / PronunciationRuleInput（pydantic 模型）
- list_/create_/update_/delete_pronunciation_rule（CRUD）
- apply_pronunciation_rules（应用到 segment 文本）

外部调用方（routes.py / build.py）继续用：
    from .project import (
        PronunciationRule, PronunciationRuleInput,
        list_pronunciation_rules, create_pronunciation_rule,
        update_pronunciation_rule, delete_pronunciation_rule,
        apply_pronunciation_rules,
    )
project.py 在底部 re-export 这些名字，避免破坏既有 import。
"""
from __future__ import annotations

import logging
import re as _re
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select

from ..db.models import ProjectPronunciationRule
from ..db.session import get_session_factory

logger = logging.getLogger(__name__)


# =====================================================================
# Pydantic 模型
# =====================================================================

class PronunciationRule(BaseModel):
    id: int
    project_id: str
    character_id: int | None = None
    rule_type: str = "alias"   # alias | regex
    pattern: str
    replacement: str
    priority: int = 0
    enabled: bool = True
    note: str = ""


class PronunciationRuleInput(BaseModel):
    character_id: int | None = None
    rule_type: str = "alias"
    pattern: str
    replacement: str
    priority: int = 0
    enabled: bool = True
    note: str = ""


# =====================================================================
# CRUD
# =====================================================================

async def list_pronunciation_rules(project_id: str) -> list[PronunciationRule]:
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(ProjectPronunciationRule).where(
            ProjectPronunciationRule.project_id == project_id,
        ).order_by(
            ProjectPronunciationRule.priority.asc(),
            ProjectPronunciationRule.id.asc(),
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [PronunciationRule.model_validate(r, from_attributes=True) for r in rows]


async def create_pronunciation_rule(
    project_id: str, inp: PronunciationRuleInput
) -> PronunciationRule:
    factory = get_session_factory()
    async with factory() as session:
        if not inp.pattern.strip() or not inp.replacement.strip():
            raise ValueError("pattern 和 replacement 不能为空")
        if inp.rule_type not in ("alias", "regex"):
            raise ValueError(f"不支持的 rule_type: {inp.rule_type}")
        if inp.rule_type == "regex":
            try:
                _re.compile(inp.pattern)
            except _re.error as e:
                raise ValueError(f"正则 pattern 不合法: {e}")
        r = ProjectPronunciationRule(
            project_id=project_id,
            character_id=inp.character_id,
            rule_type=inp.rule_type,
            pattern=inp.pattern,
            replacement=inp.replacement,
            priority=inp.priority,
            enabled=inp.enabled,
            note=inp.note,
        )
        session.add(r)
        await session.commit()
        await session.refresh(r)
        return PronunciationRule.model_validate(r, from_attributes=True)


async def update_pronunciation_rule(
    project_id: str, rule_id: int, inp: PronunciationRuleInput
) -> PronunciationRule:
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(ProjectPronunciationRule).where(
            ProjectPronunciationRule.id == rule_id,
            ProjectPronunciationRule.project_id == project_id,
        )
        r = (await session.execute(stmt)).scalar_one_or_none()
        if not r:
            raise ValueError(f"发音规则不存在: rule_id={rule_id}")
        r.character_id = inp.character_id
        r.rule_type = inp.rule_type
        r.pattern = inp.pattern
        r.replacement = inp.replacement
        r.priority = inp.priority
        r.enabled = inp.enabled
        r.note = inp.note
        r.updated_at = datetime.now().isoformat(timespec="seconds")
        await session.commit()
        await session.refresh(r)
        return PronunciationRule.model_validate(r, from_attributes=True)


async def delete_pronunciation_rule(project_id: str, rule_id: int) -> None:
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(ProjectPronunciationRule).where(
            ProjectPronunciationRule.id == rule_id,
            ProjectPronunciationRule.project_id == project_id,
        )
        r = (await session.execute(stmt)).scalar_one_or_none()
        if not r:
            raise ValueError(f"发音规则不存在: rule_id={rule_id}")
        await session.delete(r)
        await session.commit()


def apply_pronunciation_rules(
    text: str, rules: list[PronunciationRule], *, character_id: int | None = None
) -> str:
    """应用发音规则到单个 segment 文本。

    - 只应用 enabled=True 的规则
    - character_id=None 的全局规则永远生效；指定角色专属规则仅当匹配时生效
    - 按 priority 升序 → id 升序依次应用
    """
    if not text or not rules:
        return text
    active = [
        r for r in rules
        if r.enabled
        and (r.character_id is None or r.character_id == character_id)
        and r.pattern
    ]
    active.sort(key=lambda x: (x.priority, x.id))

    result = text
    for r in active:
        try:
            if r.rule_type == "regex":
                result = _re.sub(r.pattern, r.replacement, result)
            else:
                result = result.replace(r.pattern, r.replacement)
        except Exception as e:
            logger.warning(f"[pronunciation] 规则忽略 id={r.id}: {e}")
    return result