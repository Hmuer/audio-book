import asyncio
import time as _time
from typing import Callable, Optional

from .base import BaseLLMProvider, BaseTTSProvider
from .providers.minimax.llm import MiniMaxLLMProvider
from .providers.minimax.tts import MiniMaxTTSProvider


_llm_instance: BaseLLMProvider | None = None
_tts_instances: dict[str, BaseTTSProvider] = {}
# 遗留单例引用：conftest._isolate_data_dir 通过它注入 mock；保持对外属性一致。
_tts_instance: BaseTTSProvider | None = None
_tts_default_instance: BaseTTSProvider | None = None

# 全局 TTS 并发限流 semaphore（单例）。
# 所有 worker（整本合成、单章合成、项目 Build）共用同一计数，
# 防止多任务分别开 4 并发 → 实际并发叠加爆 TTS 供应商 RPM 限制（429）。
_tts_sem: asyncio.Semaphore | None = None


def _build_tts_sem() -> asyncio.Semaphore:
    from ..core.config import settings
    n = max(1, int(settings.TTS_MAX_CONCURRENCY))
    return asyncio.Semaphore(n)


def get_llm() -> BaseLLMProvider:
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = MiniMaxLLMProvider()
    return _llm_instance


# =====================================================================
# TTS 多厂商 Registry：音色 ID 前缀 → provider 工厂函数
# =====================================================================
# 延迟导入避免循环依赖；工厂函数返回 **新实例**，但 get_tts/provided 会按 provider 名缓存。
def _factory_minimax() -> BaseTTSProvider:
    return MiniMaxTTSProvider()


def _factory_doubao() -> BaseTTSProvider:
    # 延迟导入：豆包 provider 可能不存在（Task 3 尚未实现）时兜底返回最小可用对象
    try:
        from .providers.doubao.tts import DoubaoTTSProvider  # type: ignore
        return DoubaoTTSProvider()
    except Exception:
        # 当 DoubaoTTSProvider 还未创建时，返回一个「Stub」对象，它的 provider='doubao'
        # 使得 Task 2 的测试可以先通过 Registry 路由层面通过（synthesize 会抛「未实现」）。
        # 真正的 Task 3 完成后这个分支就永远不会命中。
        return _DoubaoStubProvider()


class _DoubaoStubProvider(BaseTTSProvider):
    """临时占位 Provider：Task 3 完成前避免 factory.get_tts('doubao') 因 ImportError 崩。"""
    name = "doubao_stub"
    provider = "doubao"

    async def list_voices(self):  # type: ignore[override]
        return []

    async def synthesize_to_bytes(self, text, voice_id, *, emotion="calm", speed=1.0, instruction_text=None, speaker_style=None):  # type: ignore[override]
        raise RuntimeError("DoubaoTTSProvider 尚未实现，请先完成 Task 3")

    async def synthesize_to_file(self, text, voice_id, output_path, *, emotion="calm", speed=1.0, instruction_text=None, speaker_style=None):  # type: ignore[override]
        raise RuntimeError("DoubaoTTSProvider 尚未实现，请先完成 Task 3")


# 前缀命名空间 -> (provider 标识, 工厂函数)
# icl: 前缀使用同一个 DoubaoTTSProvider（合成接口一致，provider 参数内剥离 icl:）
TTSRegistry: dict[str, tuple[str, Callable[[], BaseTTSProvider]]] = {
    "minimax": ("minimax", _factory_minimax),
    "doubao": ("doubao", _factory_doubao),
    "icl": ("doubao", _factory_doubao),
}


def _resolve_provider_name(provider: Optional[str]) -> str:
    """解析用户给出的 provider 名称。
    None → 读 settings.TTS_PROVIDER；小写化处理。
    """
    if provider:
        return provider.lower()
    from ..core.config import settings
    return (settings.TTS_PROVIDER or "minimax").lower()


def get_tts(provider: Optional[str] = None) -> BaseTTSProvider:
    """按厂商名获取 TTS 实例（按 provider 名缓存单例）。

    provider=None 时读全局 settings.TTS_PROVIDER；实例按 provider 名缓存：
    不同前缀但同 provider 标识（如 doubao:/icl: 都对应 "doubao"）共享一个实例。

    当 `_tts_instance` 被显式设置（如 conftest.monkeypatch 注入 MockTTSProvider）时：
      - provider=None → 直接返回 _tts_instance（经典行为）
      - provider=pname → 若 _tts_instance.provider 与目标 provider 标识匹配，则优先返回 mock
        （保证测试注入在显式指定 provider 时也生效）
    任何显式指定 provider（包括 minimax/doubao/icl）最终都会走 Registry 正常路由或 mock 路由。
    """
    global _tts_default_instance, _tts_instances
    # 若存在注入 mock：匹配 provider 标签时直接返回
    if _tts_instance is not None:
        if provider is None:
            _tts_default_instance = _tts_instance
            return _tts_instance
        target_name = _resolve_provider_name(provider)
        target_id = TTSRegistry.get(target_name, (None, None))[0] or target_name
        if getattr(_tts_instance, "provider", None) == target_id:
            return _tts_instance
    pname = _resolve_provider_name(provider)

    # 在 Registry 里找一个能匹配到该 provider 标识的入口（优先 minimax/doubao）
    if pname in TTSRegistry:
        key, factory_fn = TTSRegistry[pname]
    else:
        # 未知厂商名：兜底 minimax
        key, factory_fn = TTSRegistry["minimax"]

    cached = _tts_instances.get(key)
    if cached is None:
        cached = factory_fn()
        _tts_instances[key] = cached

    if provider is None:
        _tts_default_instance = cached
    return cached


def get_tts_by_voice_id(voice_id: str) -> BaseTTSProvider:
    """通过音色 ID 的命名空间前缀选择 provider；无前缀/未知前缀按全局默认。"""
    if voice_id and ":" in voice_id:
        prefix = voice_id.split(":", 1)[0].lower()
        if prefix in TTSRegistry:
            pname, _ = TTSRegistry[prefix]
            return get_tts(pname)
    # 未识别前缀 → 走全局默认
    return get_tts(None)


def get_tts_sem() -> asyncio.Semaphore:
    """惰性初始化全局 TTS semaphore（事件循环内创建）。"""
    global _tts_sem
    if _tts_sem is None:
        _tts_sem = _build_tts_sem()
    return _tts_sem


# =====================================================================
# 豆包 TTS / ICL / Seed-Audio 共享 RPM 限流桶（固定间隔 token bucket）
# 与 MiniMax 同款算法，保证任意两个请求最小间隔 60/RPM_LIMIT 秒。
# 实现一个通用工厂以避免在每个 provider 内重复代码。
# =====================================================================

class _RPMBucket:
    def __init__(self, rpm_limit_getter: Callable[[], int]):
        self._rpm_limit_getter = rpm_limit_getter
        self._next_allowed: float = 0.0
        self._lock: Optional[asyncio.Lock] = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def _interval(self) -> float:
        limit = max(1, int(self._rpm_limit_getter()))
        return 60.0 / float(limit)

    async def acquire(self) -> None:
        interval = self._interval
        async with self.lock:
            now = _time.monotonic()
            if now >= self._next_allowed:
                self._next_allowed = now + interval
                return
            wait_s = self._next_allowed - now
            self._next_allowed += interval
        # 释放锁后再 sleep，允许其他协程计算自己的 wait_time
        await asyncio.sleep(wait_s)

    def remaining_secs(self) -> float:
        now = _time.monotonic()
        return max(0.0, self._next_allowed - now)

    def reset(self) -> None:
        self._next_allowed = 0.0


# 三个独立桶（豆包三产品 RPM 限制可能独立）
def _doubao_tts_rpm() -> int:
    from ..core.config import settings
    return max(1, int(settings.DOUBAO_TTS_RPM_LIMIT))


def _doubao_seed_audio_rpm() -> int:
    from ..core.config import settings
    return max(1, int(settings.DOUBAO_SEED_AUDIO_RPM_LIMIT))


def _doubao_icl_rpm() -> int:
    # ICL 接口调用极低频，默认保守 12/min
    return 12


_doubao_tts_bucket = _RPMBucket(_doubao_tts_rpm)
_doubao_seed_audio_bucket = _RPMBucket(_doubao_seed_audio_rpm)
_doubao_icl_bucket = _RPMBucket(_doubao_icl_rpm)


async def _doubao_rpm_wait_acquire(bucket: str = "tts") -> None:
    """豆包 RPM 限流统一入口。

    bucket ∈ {'tts', 'seed_audio', 'icl'}
    """
    if bucket == "seed_audio":
        await _doubao_seed_audio_bucket.acquire()
    elif bucket == "icl":
        await _doubao_icl_bucket.acquire()
    else:
        await _doubao_tts_bucket.acquire()


def _doubao_rpm_remaining_secs(bucket: str = "tts") -> float:
    """距离下一次放行还剩多少秒（429 兜底等待用）。"""
    if bucket == "seed_audio":
        return _doubao_seed_audio_bucket.remaining_secs()
    if bucket == "icl":
        return _doubao_icl_bucket.remaining_secs()
    return _doubao_tts_bucket.remaining_secs()


def _reset_doubao_rpm_bucket_for_tests(bucket: str | None = None) -> None:
    """pytest 用：重置所有豆包 RPM 桶（避免单测串扰）。"""
    if bucket is None or bucket == "tts":
        _doubao_tts_bucket.reset()
    if bucket is None or bucket == "seed_audio":
        _doubao_seed_audio_bucket.reset()
    if bucket is None or bucket == "icl":
        _doubao_icl_bucket.reset()
