from abc import ABC, abstractmethod
from typing import Any, TypeVar, Type, Optional
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class BaseLLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def chat_structured(
        self,
        prompt: str,
        output_schema: Type[T],
        *,
        system_prompt: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 8000,
        use_fast_model: bool = False,
        max_retries: int = 3,
    ) -> T:
        """
        调用 LLM 并返回 Pydantic 结构化对象。
        校验失败自动重试（默认3次），温度逐次 +0.1。
        """
        ...


class BaseTTSProvider(ABC):
    name: str = "base"
    provider: str = "base"  # minimax / doubao / icl / ... 命名空间标识

    @abstractmethod
    async def list_voices(self) -> list[dict[str, Any]]:
        """返回音色列表（元数据 dict，含 id/name/gender/description）"""
        ...

    @abstractmethod
    async def synthesize_to_bytes(
        self,
        text: str,
        voice_id: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
        instruction_text: str | None = None,
        speaker_style: str | None = None,
    ) -> tuple[bytes, int]:
        """同步合成音频，返回 (MP3 bytes, duration_ms)。

        - instruction_text: 风格/情绪描述指令（豆包 TTS 2.0 / Seed-Audio 支持，MiniMax 可选）
        - speaker_style: 官方音色预置风格 id（部分豆包音色支持，如"亲切""热情"）
        """
        ...

    @abstractmethod
    async def synthesize_to_file(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
        instruction_text: str | None = None,
        speaker_style: str | None = None,
    ) -> tuple[str, int]:
        """
        合成音频并写入文件。
        返回 (output_path, duration_ms)
        """
        ...
