from __future__ import annotations

import asyncio
from typing import Any


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_segment_routes_by_voice_id_prefix(monkeypatch):
    """[P-2.5] worker 选 tts 应按 voice_id 前缀：doubao: 走豆包。"""
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
