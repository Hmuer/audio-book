from .base import BaseLLMProvider, BaseTTSProvider
from .providers.minimax.llm import MiniMaxLLMProvider
from .providers.minimax.tts import MiniMaxTTSProvider


_llm_instance: BaseLLMProvider | None = None
_tts_instance: BaseTTSProvider | None = None


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
