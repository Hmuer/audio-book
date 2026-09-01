"""Review 修复回归 — 独立代码审查发现的问题（2026-09）。

T-RF1 retry-failed 必须继承源 Build 的 mode/tts_provider（原 Critical：
多播剧/豆包 Build 重试会静默退化为 classic+MiniMax）。
T-RF2 _validate_multicast_provider：multicast + 非 doubao → 抛 ValueError（禁止降级）。
T-RF3 _multicast_synth_chapter：预估超 120s 上限的章节分段生成再拼接。
T-RF4 DoubaoTTSProvider 端点读 settings.DOUBAO_TTS_BASE_URL（.env 覆写生效）。
T-RF5 ICL create reqid 每次唯一（不再用 id(self)）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# T-RF2：multicast + 非 doubao 拒绝降级
# ---------------------------------------------------------------------
def test_validate_multicast_provider_rejects_non_doubao():
    from backend.app.services.build import _validate_multicast_provider

    # 合法组合不抛
    _validate_multicast_provider("multicast", "doubao")
    _validate_multicast_provider("classic", "minimax")
    _validate_multicast_provider("classic", "doubao")

    # 非法组合必须抛 ValueError（禁止静默降级为 classic）
    with pytest.raises(ValueError, match="拒绝降级"):
        _validate_multicast_provider("multicast", "minimax")
    with pytest.raises(ValueError, match="拒绝降级"):
        _validate_multicast_provider("MULTICAST", "")


# ---------------------------------------------------------------------
# T-RF3：超长章节分段生成
# ---------------------------------------------------------------------
class _RecordingMC:
    """记录每次整章合成调用的 mock Seed-Audio provider。"""

    provider = "doubao"

    def __init__(self):
        self.calls: list[list[dict]] = []

    async def synthesize_chapter_to_file(self, segments, output_path, *, speed=1.0,
                                         chapter_title="", instruction_text=None):
        self.calls.append(list(segments))
        Path(output_path).write_bytes(b"\xff\xfb\x90\x64\x00" + (b"\x00" * 1024))
        return output_path, 1000


@pytest.mark.asyncio
async def test_multicast_long_chapter_split_into_chunks(tmp_path):
    from backend.app.services.build import _multicast_synth_chapter, _estimate_multicast_secs

    # 构造 5000 字旁白（≈125s @ speed 1.0，超过 120s 上限的 85% 阈值）
    segs = [{"kind": "narration", "speaker": "", "text": "字" * 500,
             "voice_id": "doubao:zh_female_qingxin", "silence_ms": 0} for _ in range(10)]
    assert _estimate_multicast_secs(segs, 1.0) > 102  # 前置：确实超限

    mc = _RecordingMC()
    out = str(tmp_path / "ch0000.mp3")
    _, dur_ms = await _multicast_synth_chapter(
        mc, segs, out, speed=1.0, chapter_title="第一章", max_secs=120.0,
    )

    # 必须分多段调用
    assert len(mc.calls) >= 2, f"超长章节应分段，实际调用 {len(mc.calls)} 次"
    # 每段文本量都在安全范围内
    for chunk in mc.calls:
        chunk_chars = sum(len(s.get("text") or "") for s in chunk)
        assert chunk_chars <= 100 * 4 + 500, f"单段 {chunk_chars} 字超出目标"
    # 所有 segment 都被覆盖（不丢内容）
    total_segs = sum(len(c) for c in mc.calls)
    assert total_segs == len(segs)
    # 输出文件存在且非空
    assert Path(out).stat().st_size > 0
    assert dur_ms > 0
    # 分段临时文件已清理
    leftovers = list(tmp_path.glob("*.part*"))
    assert leftovers == [], f"分段临时文件未清理: {leftovers}"


@pytest.mark.asyncio
async def test_multicast_short_chapter_single_call(tmp_path):
    from backend.app.services.build import _multicast_synth_chapter

    segs = [{"kind": "narration", "speaker": "", "text": "短章节",
             "voice_id": "doubao:zh_female_qingxin", "silence_ms": 0}]
    mc = _RecordingMC()
    out = str(tmp_path / "ch0001.mp3")
    await _multicast_synth_chapter(mc, segs, out, speed=1.0, chapter_title="短", max_secs=120.0)
    assert len(mc.calls) == 1, "短章节不应分段"


# ---------------------------------------------------------------------
# T-RF4：豆包 TTS 端点可被 settings 覆写
# ---------------------------------------------------------------------
def test_doubao_tts_endpoint_reads_settings(monkeypatch):
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    p = DoubaoTTSProvider()
    assert p._endpoint == DoubaoTTSProvider.DEFAULT_ENDPOINT  # 默认值

    # 直接覆写 settings 单例（不要走 monkeypatch 字符串路径，避免模块解析失败）
    saved = cfgmod.settings.DOUBAO_TTS_BASE_URL
    cfgmod.settings.DOUBAO_TTS_BASE_URL = "http://mock:9999/api/v1/tts"
    try:
        assert p._endpoint == "http://mock:9999/api/v1/tts"
    finally:
        cfgmod.settings.DOUBAO_TTS_BASE_URL = saved


# ---------------------------------------------------------------------
# T-RF5：ICL create reqid 唯一
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_icl_create_reqid_unique(monkeypatch):
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient

    captured: list[dict] = []

    async def _fake_post(url, payload):
        captured.append(payload)
        return {"code": 0, "data": {"task_id": f"dtid-{len(captured)}"}}

    monkeypatch.setattr(
        "backend.app.core.config.settings.DOUBAO_AK", "test-ak", raising=False
    )
    client = DoubaoICLClient()
    client._http_post_json = _fake_post  # type: ignore[method-assign]

    fake_mp3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 2048)
    await client.create_training("声线A", fake_mp3)
    await client.create_training("声线B", fake_mp3)

    reqids = [p["reqid"] for p in captured]
    assert len(reqids) == 2 and reqids[0] != reqids[1], (
        f"ICL create reqid 必须每次唯一，实际 {reqids}"
    )


# ---------------------------------------------------------------------
# T-RF1：retry-failed 继承源 Build 的 mode/tts_provider
# ---------------------------------------------------------------------
_BOOK_TXT = """第一章 初遇
今天是美好的一天。阳光透过窗户洒进房间，林小雨伸了个懒腰。

第二章 启程
林小雨拿起背包，走出了家门。张伟在门口等她，微笑着说："我们出发吧。"
"""

@pytest.mark.asyncio
async def test_retry_failed_inherits_mode_and_provider(_isolate_data_dir):
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import (
        start_build, get_build_status, retry_failed_build,
        _ACTIVE_BUILDS, _RUNNING_LOCK,
    )
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build
    from backend.app.core import config as cfgmod
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider

    cfgmod.settings.MULTICAST_STRICT_MODE = True

    class _FailMC:
        provider = "doubao"

        async def synthesize_chapter_to_file(self, segments, output_path, *, speed=1.0,
                                             chapter_title="", instruction_text=None):
            raise RuntimeError("模拟 Seed-Audio 失败（触发 retry 场景）")

    prev_tts, prev_llm, prev_mc = aifact._tts_instance, aifact._llm_instance, aifact._multicast_instance
    aifact._tts_instance = MockTTSProvider()
    aifact._llm_instance = MockLLMProvider()
    aifact._multicast_instance = _FailMC()
    try:
        await init_db()
        pid = (await create_project("重试继承配置测试")).project_id
        await import_file(pid, _BOOK_TXT.encode("utf-8"), "book.txt")
        await prepare_project(pid)

        resp = await start_build(
            project_id=pid, voice_assignments={},
            narrator_voice_id="doubao:zh_female_qingxin",
            tts_provider="doubao", mode="multicast",
        )
        bid = resp.build_id
        for _ in range(60):
            s = await get_build_status(bid)
            if s.status in ("success", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)

        # 源 Build 已 failed（strict + multicast）
        assert (await get_build_status(bid)).status == "failed"

        # 触发 retry-failed：新 Build 必须继承 multicast/doubao
        retry_resp = await retry_failed_build(source_build_id=bid)
        factory = get_session_factory()
        async with factory() as sess:
            nb = await sess.get(Build, retry_resp.build_id)
            assert nb is not None
            assert (nb.mode or "classic").lower() == "multicast", (
                f"retry Build 必须继承 mode=multicast，实际 {nb.mode!r}"
            )
            assert (nb.tts_provider or "minimax").lower() == "doubao", (
                f"retry Build 必须继承 tts_provider=doubao，实际 {nb.tts_provider!r}"
            )
        # 等 retry worker 结束，避免污染后续测试
        for _ in range(60):
            s = await get_build_status(retry_resp.build_id)
            if s.status in ("success", "failed", "cancelled", "partial_success"):
                break
            await asyncio.sleep(0.5)
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        aifact._multicast_instance = prev_mc
        cfgmod.settings.MULTICAST_STRICT_MODE = False
        async with _RUNNING_LOCK:
            # 清掉可能残留的 (build_id -> pid) 键，避免污染后续测试
            stale_keys = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale_keys:
                _ACTIVE_BUILDS.pop(k, None)
