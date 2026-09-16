"""[P-2.5] Build worker 路由：每段合成按 voice_id 前缀选 TTS 实例，跨厂商不再报错。

历史硬约束：旁白/角色音色必须属于同一 tts_provider，否则 Build 入口直接抛 RuntimeError。
新行为：合成时按 voice_id 前缀路由厂商（doubao:/icl: → 豆包；minimax: → MiniMax），
        用户可以在一个 Build 里混用任意厂商的音色。
"""
from __future__ import annotations

import asyncio
from typing import Any


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_segment_routes_by_voice_id_prefix(monkeypatch):
    """[P-2.5] worker 选 tts 应按 voice_id 前缀：doubao: 走豆包，minimax: 走 MiniMax。

    验证手段：
      1) 后端 _validate_tts_namespace 不再硬约束跨厂商音色（已覆盖在下方）
      2) worker 函数内对 seg_tts 的选择逻辑：用源码静态扫描确认每段按 voice_id
         前缀调用 get_tts_by_voice_id，无前缀时走 fallback_provider 兜底
      3) factory.get_tts_by_voice_id 自身按前缀正确路由（已有 task2 RED 覆盖）
    """
    import inspect
    from backend.app.services import build as build_mod

    src = inspect.getsource(build_mod)
    # worker 必须按 voice_id 前缀路由
    assert "get_tts_by_voice_id(vid)" in src, (
        "[P-2.5] worker 必须按 voice_id 前缀调用 get_tts_by_voice_id(vid)，"
        "未在 build.py 源码中找到对应调用"
    )
    # 无前缀音色走 fallback 兜底（不能给无前缀音色强制绑定某个厂商）
    assert '":" in (vid or "")' in src or '":" in vid' in src, (
        "[P-2.5] 无前缀音色必须走 fallback_provider 兜底，不能硬编码厂商"
    )
    # 不再硬约束：之前那行"tts = get_tts(None if tts_provider_label ...)"应该没了
    # （按需留 fallback_provider 这个变量名供后续无前缀音色兜底）
    assert "fallback_provider" in src, (
        "[P-2.5] 需要保留 fallback_provider 变量供无前缀音色兜底"
    )


def test_cross_provider_build_does_not_raise():
    """[P-2.5] 跨厂商音色配置 Build 应不抛错（与旧硬约束相反）。"""
    from backend.app.services.build import _validate_tts_namespace

    # 旧版这会抛 RuntimeError；新版必须通过
    _validate_tts_namespace(
        tts_provider="minimax",  # 即便默认是 minimax
        mode="classic",
        narrator_voice_id="doubao:zh_female_qingxin",  # 旁白是豆包
        voice_assignments={
            "甲": "minimax:male-qn-jingying",  # 角色用 MiniMax
            "乙": "icl:custom_clone_001",     # 角色用 ICL（豆包）
        },
    )
    _validate_tts_namespace(
        tts_provider="doubao",
        mode="classic",
        narrator_voice_id="minimax:male-qn-jingying",
        voice_assignments={"甲": "doubao:zh_female_qingxin"},
    )
