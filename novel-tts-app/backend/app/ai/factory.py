import asyncio

from .base import BaseLLMProvider, BaseTTSProvider
from .providers.minimax.llm import MiniMaxLLMProvider
from .providers.minimax.tts import MiniMaxTTSProvider


_llm_instance: BaseLLMProvider | None = None
_tts_instance: BaseTTSProvider | None = None

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


def get_tts() -> BaseTTSProvider:
    global _tts_instance
    if _tts_instance is None:
        _tts_instance = MiniMaxTTSProvider()
    return _tts_instance


def get_tts_sem() -> asyncio.Semaphore:
    """惰性初始化全局 TTS semaphore（事件循环内创建）。"""
    global _tts_sem
    if _tts_sem is None:
        _tts_sem = _build_tts_sem()
    return _tts_sem
