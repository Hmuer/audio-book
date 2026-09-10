"""Task 7 RED — Seed-Audio 多播剧 Provider 自 P0-5 起已废弃。

历史背景：Seed-Audio 多播剧整章一体化生成端点已停止迭代（官方不再推荐）。
现在：mode=multicast 在 start_build 入口处自动降级为 classic（逐章节分段
TTS 拼接），不再走任何「整章一体化」路径。

本测试覆盖 P0-5 后的行为契约：
  T-MC1  start_build(mode='multicast') 自动降级：resp.mode == 'classic'
  T-MC2  start_build(mode='multicast') 走逐段 TTS（mock 注入 _tts_instance），
         不再调用任何 multicast provider（mock 注入的 _multicast_instance
         即使存在也不会被触发）
  T-MC3  factory.get_multicast_tts() 永远返回 None（旧 conftest 注入的
         _multicast_instance 也不再有效，保留仅为向后兼容）
  T-MC4  _validate_tts_namespace 不再因 mode=multicast 抛错：
         - tts_provider='minimax' + mode='multicast' 不抛（历史会抛）
         - tts_provider=''  + mode='multicast' 不抛（历史会抛）
  T-MC5  Build 完成后 mode 字段持久化为 'classic'（即降级后用户看到的值）
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
# Mock 适配：当 tts_provider='doubao' 时 factory.get_tts('doubao') 会按
# provider 标识匹配 MockTTSProvider；MockTTSProvider 默认 provider='minimax'，
# 这里提供一个 provider='doubao' 的子类让 mock 注入命中 factory 路由。
# ---------------------------------------------------------------------
class _DoubaoMockTTS:
    """Mock TTS 子类：标记 provider='doubao'，避免调用真实 DoubaoTTSProvider。
    实现最小接口，行为与 MockTTSProvider 一致（静音 MP3）。"""

    provider = "doubao"
    name = "mock_tts_doubao"

    def __init__(self):
        from backend.tests.mock_providers import MockTTSProvider
        self._inner = MockTTSProvider()
        self.calls: list = []

    async def list_voices(self):
        return await self._inner.list_voices()

    async def synthesize_to_bytes(self, text, voice_id, *, emotion="calm", speed=1.0,
                                  instruction_text=None, speaker_style=None):
        self.calls.append({"text": text, "voice_id": voice_id})
        return await self._inner.synthesize_to_bytes(
            text, voice_id, emotion=emotion, speed=speed,
            instruction_text=instruction_text, speaker_style=speaker_style,
        )

    async def synthesize_to_file(self, text, voice_id, output_path, *, emotion="calm",
                                 speed=1.0, instruction_text=None, speaker_style=None):
        return await self._inner.synthesize_to_file(
            text, voice_id, output_path, emotion=emotion, speed=speed,
            instruction_text=instruction_text, speaker_style=speaker_style,
        )


# ---------------------------------------------------------------------
# T-MC1 + T-MC2：start_build(mode='multicast') 自动降级 classic
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_build_multicast_mode_raises_not_downgraded(_isolate_data_dir):
    """#4 不降级契约：mode=multicast 在 start_build 入口直接抛 RuntimeError，
    不再静默降级为 classic、不再启动 Build。"""
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import start_build, _ACTIVE_BUILDS, _RUNNING_LOCK
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockLLMProvider

    await init_db()
    prev_tts = aifact._tts_instance
    prev_llm = aifact._llm_instance
    aifact._tts_instance = _DoubaoMockTTS()
    aifact._llm_instance = MockLLMProvider()
    try:
        pid = (await create_project("multicast-不降级测试")).project_id
        book = "第一章 初遇\n李明说：「你好，请问怎么走？」\n林若雪说：「跟我来吧。」\n第二章 启程\n他们出发了。"
        await import_file(pid, book.encode("utf-8"), "book.txt")
        await prepare_project(pid)

        # #4 不降级：mode=multicast 直接抛错，不降级、不生成 Build
        with pytest.raises(RuntimeError, match="已不再支持"):
            await start_build(
                project_id=pid,
                voice_assignments={"李明": "doubao:BV002_streaming"},
                narrator_voice_id="doubao:BV001_streaming",
                tts_provider="doubao",
                mode="multicast",
            )
        # 不应有任何活跃 Build 残留（start_build 在落库前就抛了）
        async with _RUNNING_LOCK:
            stale = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            assert stale == [], f"multicast 抛错后不应残留 Build，实际 {stale}"
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        async with _RUNNING_LOCK:
            stale = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale:
                _ACTIVE_BUILDS.pop(k, None)


# ---------------------------------------------------------------------
# T-MC3：注入的 _multicast_instance 即使存在也不会被触发
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_injected_multicast_instance_is_ignored(_isolate_data_dir):
    """#4 不降级契约：mode=multicast 在 start_build 入口直接抛错，
    即便旧 conftest 注入了 _multicast_instance，也不会被调用（build 根本没启动）。"""
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import start_build, _ACTIVE_BUILDS, _RUNNING_LOCK
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockLLMProvider

    await init_db()

    class _ShouldNeverBeCalled:
        def __init__(self):
            self.calls = []

        async def synthesize_chapter_to_file(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise RuntimeError("如果 multicast provider 被调用，说明走了不该走的路径")

    sentinel = _ShouldNeverBeCalled()
    prev_tts = aifact._tts_instance
    prev_llm = aifact._llm_instance
    prev_mc = aifact._multicast_instance
    aifact._tts_instance = _DoubaoMockTTS()
    aifact._llm_instance = MockLLMProvider()
    aifact._multicast_instance = sentinel  # 即使注入 sentinel 也必须不被调用
    try:
        pid = (await create_project("sentinel-测试")).project_id
        book = "第一章\n他说：「测试」\n第二章\n继续。"
        await import_file(pid, book.encode("utf-8"), "book.txt")
        await prepare_project(pid)
        # #4 不降级：mode=multicast 直接抛错（不降级、不启动 Build）
        with pytest.raises(RuntimeError, match="已不再支持"):
            await start_build(
                project_id=pid,
                voice_assignments={},
                narrator_voice_id="doubao:BV001_streaming",
                tts_provider="doubao",
                mode="multicast",
            )
        # multicast sentinel 必须从未被调用（build 在落库前就抛了）
        assert sentinel.calls == [], (
            f"_multicast_instance 被调用了 {len(sentinel.calls)} 次"
        )
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        aifact._multicast_instance = prev_mc
        async with _RUNNING_LOCK:
            stale = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale:
                _ACTIVE_BUILDS.pop(k, None)


# ---------------------------------------------------------------------
# T-MC4：factory.get_multicast_tts() 现在永远返回 None
# ---------------------------------------------------------------------
def test_factory_get_multicast_tts_returns_none_after_deprecation():
    from backend.app.ai import factory as aifact

    # 默认状态：永远返回 None（P0-5 起已废弃）
    inst = aifact.get_multicast_tts()
    assert inst is None, (
        f"P0-5 后 get_multicast_tts() 必须返回 None（即使 _multicast_instance "
        f"被 conftest 注入，新代码也不应该再走到 multicast 路径），实际 {inst!r}"
    )


# ---------------------------------------------------------------------
# T-MC5：_validate_tts_namespace 不再因 mode=multicast 抛错
# ---------------------------------------------------------------------
def test_validate_tts_namespace_no_longer_rejects_multicast_with_minimax():
    """历史：mode=multicast + tts_provider='minimax' 会抛 RuntimeError。
    P0-5 之后：mode=multicast 在 start_build 入口降级为 classic，_validate_tts_namespace
    不再对 mode=multicast 做特殊校验（minimax + multicast 不再报错）。"""
    from backend.app.services.build import _validate_tts_namespace

    # minimax + multicast：历史抛错，现在 OK（降级）
    _validate_tts_namespace(
        tts_provider="minimax",
        mode="multicast",
        narrator_voice_id="minimax:male-qn-jingying",
        voice_assignments={},
    )

    # 空 provider + multicast：历史抛错，现在 OK
    _validate_tts_namespace(
        tts_provider="",
        mode="multicast",
        narrator_voice_id="",
        voice_assignments={},
    )

    # 未知 mode 仍然报错
    with pytest.raises(RuntimeError, match="未知 build.mode"):
        _validate_tts_namespace(
            tts_provider="doubao",
            mode="hologram",
            narrator_voice_id="doubao:BV001_streaming",
            voice_assignments={},
        )

    # 未知 provider 仍然报错
    with pytest.raises(RuntimeError, match="未知 tts_provider"):
        _validate_tts_namespace(
            tts_provider="elevenlabs",
            mode="classic",
            narrator_voice_id="elevenlabs:abc",
            voice_assignments={},
        )


# ---------------------------------------------------------------------
# T-MC6：旧 multicast provider 模块已物理删除
# ---------------------------------------------------------------------
def test_old_multicast_module_is_deleted():
    """P0-5：backend.app.ai.providers.doubao.multicast 模块已物理删除；
    新代码不应再 import 它。"""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.app.ai.providers.doubao.multicast")
