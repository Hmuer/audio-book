import asyncio
import hashlib
import time as _time
from typing import Callable, Optional

from .base import BaseLLMProvider, BaseTTSProvider
from .providers.minimax.llm import MiniMaxLLMProvider


_llm_instance: BaseLLMProvider | None = None
# C-3：provider 名 → (配置指纹, 实例)。缓存键带上配置指纹，运行中改端点/模型/
# key/DOUBAO_TTS_USE_V3 等会自然命中「指纹不同」→ 重建实例，无需重启后端。
_tts_instances: dict[str, tuple[str, BaseTTSProvider]] = {}
# 遗留单例引用：conftest._isolate_data_dir 通过它注入 mock；保持对外属性一致。
_tts_instance: BaseTTSProvider | None = None
_tts_default_instance: BaseTTSProvider | None = None

# 全局 TTS 并发限流 semaphore（单例）。
# 所有 worker（整本合成、单章合成、项目 Build）共用同一计数，
# 防止多任务分别开 4 并发 → 实际并发叠加爆 TTS 供应商 RPM 限制（429）。
_tts_sem: asyncio.Semaphore | None = None
# H-1：记录创建 sem 时的并发值；与当前 settings 不一致则重建（热更新）
_tts_sem_value: int = 0


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
def _factory_doubao() -> BaseTTSProvider:
    # 延迟导入避免循环依赖
    from ..core.config import settings
    # P1-1：settings.DOUBAO_TTS_USE_V3=True 时路由到 v3 单向流式 provider；
    # 否则沿用 v1（端点 /api/v1/tts，业务码 JSON 协议）
    if getattr(settings, "DOUBAO_TTS_USE_V3", False):
        from .providers.doubao.tts import DoubaoTTSProviderV3
        return DoubaoTTSProviderV3()
    from .providers.doubao.tts import DoubaoTTSProvider  # type: ignore
    return DoubaoTTSProvider()


def _factory_minimax() -> BaseTTSProvider:
    from .providers.minimax.tts import MiniMaxTTSProvider
    return MiniMaxTTSProvider()


TTSRegistry: dict[str, tuple[str, Callable[[], BaseTTSProvider]]] = {
    "doubao": ("doubao", _factory_doubao),
    "minimax": ("minimax", _factory_minimax),
}


def _resolve_provider_name(provider: Optional[str]) -> str:
    if provider:
        return provider.split(":", 1)[0].lower()
    from ..core.config import get_active, settings
    pid, _, _ = get_active("tts")
    return (pid or settings.TTS_PROVIDER or "doubao").lower()


def _tts_config_fingerprint() -> str:
    """C-3：TTS 实例缓存的配置指纹。

    TTS provider 实例在构造时快照了端点/模型/key/超时/v3 开关等配置。若缓存只按
    provider 名索引，运行中改这些配置后会继续复用旧实例，必须重启才生效。

    这里把「会影响 provider 构造的配置」一起哈希：所有 `DOUBAO_*` / `TTS_*` /
    `ACTIVE_TTS_*` 字段 + 结构化 `PROVIDERS_CONFIG`。只在这些字段变化时指纹才变，
    因此不会因无关配置（如日志级别）抖动而频繁重建。
    """
    from ..core.config import settings

    parts: list[str] = []
    for name in sorted(dir(settings)):
        if name.startswith(("DOUBAO_", "MINIMAX_TTS_", "LLM_API_KEY", "LLM_BASE_URL", "TTS_", "ACTIVE_TTS_")):
            parts.append(f"{name}={getattr(settings, name)}")
    parts.append(f"PROVIDERS_CONFIG={settings.PROVIDERS_CONFIG or ''}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def invalidate_tts_cache() -> None:
    """清空 TTS 实例缓存（改配置后如需立即强制重建可调用）。"""
    _tts_instances.clear()


def get_tts(provider: Optional[str] = None) -> BaseTTSProvider:
    global _tts_default_instance
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

    if pname in TTSRegistry:
        key, factory_fn = TTSRegistry[pname]
    else:
        key, factory_fn = TTSRegistry["doubao"]

    # C-3：缓存键附带配置指纹 → 配置变更后自动重建实例（不必重启后端）
    fp = _tts_config_fingerprint()
    entry = _tts_instances.get(key)
    if entry is None or entry[0] != fp:
        cached = factory_fn()
        _tts_instances[key] = (fp, cached)
    else:
        cached = entry[1]

    if provider is None:
        _tts_default_instance = cached
    return cached


def get_tts_by_voice_id(voice_id: str) -> BaseTTSProvider:
    if voice_id and ":" in voice_id:
        prefix = voice_id.split(":", 1)[0].lower()
        if prefix in TTSRegistry:
            pname, _ = TTSRegistry[prefix]
            return get_tts(pname)
    # 未识别前缀 → 走全局默认
    return get_tts(None)


def get_tts_sem() -> asyncio.Semaphore:
    """惰性初始化全局 TTS semaphore（事件循环内创建）。

    H-1 热更新：settings.TTS_MAX_CONCURRENCY 变化时立即重建（不需要重启后端）。
    旧 sem 由在飞请求的持有者自然释放；新请求立即按新并发度限流。
    """
    global _tts_sem, _tts_sem_value
    from ..core.config import settings
    n = max(1, int(settings.TTS_MAX_CONCURRENCY))
    if _tts_sem is None or _tts_sem_value != n:
        _tts_sem = asyncio.Semaphore(n)
        _tts_sem_value = n
    return _tts_sem


# =====================================================================
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


def _doubao_tts_rpm() -> int:
    from ..core.config import settings
    return max(1, int(settings.DOUBAO_TTS_RPM_LIMIT))


def _minimax_tts_rpm() -> int:
    from ..core.config import settings
    return max(1, int(settings.MINIMAX_TTS_RPM_LIMIT))


_doubao_tts_bucket = _RPMBucket(_doubao_tts_rpm)
_minimax_tts_bucket = _RPMBucket(_minimax_tts_rpm)


async def _minimax_rpm_wait_acquire() -> None:
    await _minimax_tts_bucket.acquire()


async def _doubao_rpm_wait_acquire(bucket: str = "tts") -> None:
    if bucket == "tts":
        await _doubao_tts_bucket.acquire()


def _doubao_rpm_remaining_secs(bucket: str = "tts") -> float:
    return _doubao_tts_bucket.remaining_secs() if bucket == "tts" else 0.0


def _reset_doubao_rpm_bucket_for_tests(bucket: str | None = None) -> None:
    if bucket is None or bucket == "tts":
        _doubao_tts_bucket.reset()
