"""
验收标准 3 个最小测试：
1. 短文本角色识别不抛错（<20字也调LLM）
2. LLM 自我评估过度修改时回退原文 + polish_warning 非空
3. 3 角色合成时每段 dialogue 的 voice_id 一路透传到最终 segments
"""
from __future__ import annotations
import pytest

pytest_plugins = ("pytest_asyncio",)


@pytest.mark.asyncio
async def test_short_text_character_extraction_runs_llm(_isolate_data_dir, db_session):
    """
    1. 短文本（<20字）也要调 LLM，不抛错、不"短路"走启发式。
    验证点：LLM.call 数至少 1 次（角色提取），characters 非空。
    """
    from backend.app.services.chapter import prepare_chapter

    SHORT = "李明说：你好。"  # 9 个汉字，< 20
    assert len(SHORT) < 20

    resp = await prepare_chapter(db_session, SHORT, enable_polish=True)

    mock_llm = _isolate_data_dir["llm"]
    # 至少 1 次角色提取调用
    schema_calls = [c["schema"] for c in mock_llm.calls]
    character_llm_calls = [s for s in schema_calls if "Wrapper" in s or "Character" in s]
    assert len(character_llm_calls) >= 1, f"LLM 应该被调用。实际调用 schemas: {schema_calls}"

    # 必须拿到李明（mock 里保证）
    names = [c["name"] for c in resp.characters]
    assert "李明" in names, f"短文本应该能识别角色。实际 characters={names}"


@pytest.mark.asyncio
async def test_llm_self_assessment_unreasonable_falls_back(_isolate_data_dir, db_session):
    """
    2. LLM 自我评估过度修改时（is_reasonable=false）：
       - polished_text 应该回退 == raw_text
       - polish_warning 非空（展示给前端）
    """
    from backend.app.services.chapter import prepare_chapter

    RAW = "他沉默了许久，终于开口说道：『我……我不知道。』__FORCE_UNREASONABLE__"

    resp = await prepare_chapter(db_session, RAW, enable_polish=True)

    # mock 中标记了 __FORCE_UNREASONABLE__，polish 会返回 is_reasonable=false
    # → polished_text 应该是回退的 raw（去掉标记？不，polish_with_llm 原文保留但业务回退 RAW）
    # 在 chapter.py：当 is_reasonable=false 时 polished_text=raw_text
    assert resp.polished_text == RAW, (
        f"不合理修改时应回退到原文。\n"
        f"expected={RAW!r}\nactual={resp.polished_text!r}"
    )
    assert resp.polish_warning is not None and "过度" in resp.polish_warning, (
        f"polish_warning 应该包含过度修改说明。got: {resp.polish_warning!r}"
    )


@pytest.mark.asyncio
async def test_three_character_synthesize_voice_ids_propagate(_isolate_data_dir, db_session):
    """
    3. 3 角色合成：对白段的 voice_id 要等于传入的 assignments。
    Mock TTS 返回静音，但 synthesize 返回的 segments 里每段 dialogue 的 voice_id 必须
    与 voice_assignments 严格对应（如果没被 segment_overrides 覆盖）。
    """
    from backend.app.services.chapter import prepare_chapter, synthesize_chapter

    TEXT = (
        "林若雪低着头。李明拍她肩膀：「怎么了？」\n"
        "「没什么。」林若雪小声说。\n"
        "「快走吧。」王大爷手里拿着糖葫芦，在街角催促。"
    )
    prep = await prepare_chapter(db_session, TEXT, enable_polish=True)

    # 造 assignments：指定 3 个不同音色
    assignments = {
        "林若雪": "female-tianmei",
        "李明": "male-qn-qingse",
        "王大爷": "male-qn-badao",
    }
    narrator = "male-qn-jingying"

    synth = await synthesize_chapter(
        db_session,
        job_id=prep.job_id,
        voice_assignments=assignments,
        narrator_voice_id=narrator,
    )

    # 取出 dialogue 段
    dlg_segs = [s for s in synth.segments if s.kind == "dialogue"]
    # 应该有 3 段对白（mock 保证）
    assert len(dlg_segs) >= 3, f"对白段数={len(dlg_segs)}, segments 预览={[(s.kind, s.speaker, s.text[:12]) for s in synth.segments]}"

    # 每段 speaker → voice_id 要对得上 assignments（没被 override）
    mismatches = []
    for seg in dlg_segs:
        expected = assignments.get(seg.speaker)
        if expected and seg.voice_id != expected:
            mismatches.append((seg.speaker, f"expected={expected}", f"actual={seg.voice_id}"))
    assert not mismatches, f"voice_id 没有透传：{mismatches}"

    # 旁白段 voice_id 必须是 narrator
    narrator_segs = [s for s in synth.segments if s.kind == "narrator"]
    if narrator_segs:
        wrong = [s for s in narrator_segs if s.voice_id != narrator]
        assert not wrong, f"旁白 voice_id 不一致: {[(s.idx, s.voice_id) for s in wrong]}"

    # 最终 MP3 应该存在且有字节数
    from backend.app.core.config import settings
    final_path = settings.AUDIO_DIR / synth.audio_filename
    assert final_path.exists(), f"最终 MP3 不存在: {final_path}"
    assert final_path.stat().st_size > 0, f"最终 MP3 是空文件"
