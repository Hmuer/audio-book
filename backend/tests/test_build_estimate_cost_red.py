"""合成预估里的费用预估 RED（2026-09-20）。

需求：豆包语音合成（字符版）按「文本字符数（含标点）」计费，单价 0.0003 元/字，
      在 `GET /projects/{id}/estimate` 的「合成预估」里给出费用预估。

计费口径（本用例锁定的三条）：
1. 计费基数 = **实际下发给 TTS 的段文本之和**（标题 + 旁白 + 对白），与逐段请求一一对应；
   ⚠️ 标题段也要发给 TTS，所以它**算钱**，会大于「正文字数」。
2. 语音指令（`context_texts`）官方明确**不计费**，写多长都不改变预估。
3. 单价由 `settings.DOUBAO_TTS_PRICE_PER_CHAR` 下发（前后端不写死），改配置即改预估。

覆盖：
  EC-1 预估项齐全且与手算一致（tts_chars=19 / cost=19×单价）
  EC-2 逐段语音指令文字不计入计费字数
  EC-3 改单价 → 预估费用随之变化，且单价原样回传
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 一章：标题「第一章 测试」(6) + 旁白「他推开门。」(5) + 对白「你是谁？」(4) + 旁白「她问道。」(4)
_TITLE = "第一章 测试"
_BODY = "他推开门。「你是谁？」她问道。"
# 手算：6 + 5 + 4 + 4 = 19（标题也下发给 TTS，故大于正文 15 字）
_EXPECT_TTS_CHARS = 19
_EXPECT_BODY_CHARS = 15


async def _seed_project(*, instruction: str = "") -> str:
    from backend.app.db.models import Project, ProjectDialogue
    from backend.app.db.session import get_session_factory, init_db

    await init_db()
    factory = get_session_factory()
    pid = uuid.uuid4().hex
    anchor = "「你是谁？」"
    start = _BODY.index(anchor)
    async with factory() as s:
        s.add(Project(
            project_id=pid,
            name="预估用例",
            status="prepared",
            owner_user_id=1,
            source_file_path="/nonexistent.txt",
            source_filename="t.txt",
            source_charset="utf-8",
            chapters_json=json.dumps(
                [{"idx": 0, "title": _TITLE, "text": _BODY}], ensure_ascii=False
            ),
        ))
        s.add(ProjectDialogue(
            project_id=pid,
            chapter_idx=0,
            segment_index=0,
            anchor_start=start,
            anchor_end=start + len(anchor),
            anchor_text=anchor,
            speaker="她",
            text="你是谁？",
            confidence=1.0,
            instruction=instruction,
        ))
        await s.commit()
    return pid


@pytest.mark.asyncio
async def test_ec1_estimate_includes_cost(monkeypatch):
    from backend.app.core import config as cfgmod
    from backend.app.services.build import estimate_project_build

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_TTS_PRICE_PER_CHAR", 0.0003)
    pid = await _seed_project()

    r = await estimate_project_build(pid)

    assert r.total_chars == _EXPECT_BODY_CHARS
    assert r.tts_chars == _EXPECT_TTS_CHARS, (
        "计费字数应为实际下发的段文本之和（标题+旁白+对白），不是正文字数"
    )
    assert r.price_per_char_cny == 0.0003
    assert r.est_tts_cost_cny == round(_EXPECT_TTS_CHARS * 0.0003, 2)
    assert r.est_tts_segments == 4  # 标题 + 旁白 + 对白 + 旁白


@pytest.mark.asyncio
async def test_ec2_voice_instruction_text_is_not_billed(monkeypatch):
    """指令只进 context_texts，官方明确不计费 → 不改变预估。"""
    from backend.app.core import config as cfgmod
    from backend.app.services.build import estimate_project_build

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_TTS_PRICE_PER_CHAR", 0.0003)
    long_instruction = "用虚弱沙哑、气息不足的语气，带着勉强支撑的疲惫感说。" * 8
    pid = await _seed_project(instruction=long_instruction)

    r = await estimate_project_build(pid)

    assert r.tts_chars == _EXPECT_TTS_CHARS
    assert r.est_tts_cost_cny == round(_EXPECT_TTS_CHARS * 0.0003, 2)


@pytest.mark.asyncio
async def test_ec3_price_comes_from_settings(monkeypatch):
    from backend.app.core import config as cfgmod
    from backend.app.services.build import estimate_project_build

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_TTS_PRICE_PER_CHAR", 0.01)
    pid = await _seed_project()

    r = await estimate_project_build(pid)

    assert r.price_per_char_cny == 0.01
    assert r.est_tts_cost_cny == round(_EXPECT_TTS_CHARS * 0.01, 2)
