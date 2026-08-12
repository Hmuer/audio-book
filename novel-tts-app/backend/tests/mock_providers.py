"""
Mock LLM / TTS Provider，用于 pytest 离线测试。
不依赖真实 API Key。
"""
from __future__ import annotations
from typing import Any, Type, TypeVar
from pydantic import BaseModel
import json as _json
from pathlib import Path

from backend.app.ai.base import BaseLLMProvider, BaseTTSProvider
from backend.app.ai.providers.minimax.tts import make_silent_mp3, _estimate_mp3_duration_ms

T = TypeVar("T", bound=BaseModel)

VOICES_FILE = Path(__file__).resolve().parent.parent / "app" / "ai" / "providers" / "minimax" / "voices.json"


class MockLLMProvider(BaseLLMProvider):
    """
    根据被调场景的 output_schema 返回构造好的合法假数据，
    确保所有业务 service 端到端可测试。
    """

    name = "mock_llm"

    def __init__(self):
        self.calls: list[dict] = []

    async def chat_structured(
        self,
        prompt: str,
        output_schema: Type[T],
        *,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8000,
        use_fast_model: bool = False,
        max_retries: int = 3,
    ) -> T:
        self.calls.append({"prompt": prompt, "schema": output_schema.__name__})

        schema_name = output_schema.__name__

        # Polish: 默认 is_reasonable=true。测试 2 通过 "过度修改标记" 让其返回 false
        if schema_name == "PolishResult":
            if "__FORCE_UNREASONABLE__" in prompt:
                # 模拟 LLM 过度修改了文风（把『』改成「」），自我评估 is_reasonable=false
                raw = _extract_raw_from_prompt(prompt).replace("__FORCE_UNREASONABLE__", "")
                modified = raw.replace("『", "「").replace("』", "」")
                return output_schema.model_validate({
                    "polished_text": modified,
                    "diff": [{"type": "replace", "old": "『", "new": "「", "position": 0}],
                    "is_reasonable": False,
                    "reason": "擅自修改引号风格，属于过度修改。",
                })
            if "『我……我不知道。』" in prompt:
                # 原封不动回退，模拟没有错字
                return output_schema.model_validate({
                    "polished_text": _extract_raw_from_prompt(prompt),
                    "diff": [],
                    "is_reasonable": True,
                    "reason": "无需修改",
                })
            raw = _extract_raw_from_prompt(prompt)
            return output_schema.model_validate({
                "polished_text": raw,
                "diff": [],
                "is_reasonable": True,
                "reason": "无需修改",
            })

        if schema_name == "_ListWrapper" or "Character" in schema_name:
            # Character 识别：从 prompt 抓名字。测试 1 是短文本(<20字)也要返回。
            chars = [
                {"name": "林若雪", "gender": "女", "age": "少女", "personality": "内向"},
                {"name": "李明", "gender": "男", "age": "青年", "personality": "开朗"},
            ]
            # 短文本测试 ("李明说：你好。") 只返回李明
            if "李明说" in prompt and "林若雪" not in prompt:
                chars = [{"name": "李明", "gender": "男", "age": "青年", "personality": "开朗"}]
            # 3 角色测试（合成）
            if "王大爷" in prompt and "李明" in prompt and "林若雪" in prompt:
                chars = [
                    {"name": "林若雪", "gender": "女", "age": "17", "personality": "内向"},
                    {"name": "李明", "gender": "男", "age": "18", "personality": "开朗"},
                    {"name": "王大爷", "gender": "男", "age": "老年", "personality": "热情"},
                ]
            # Wrapper 顶层 data
            try:
                return output_schema.model_validate({"data": chars})
            except Exception:
                return output_schema.model_validate({"items": chars})

        if schema_name in ("DedupResult",):
            return output_schema.model_validate({"data": []})

        if schema_name == "_Wrapper" and "可用角色列表：" in prompt and ("对白标注" in prompt or "对白" in prompt):
            # 对白归属
            text = _extract_text_block(prompt, "---TEXT START---", "---TEXT END---")
            attrs = []
            import re as _re
            i = 0
            for m in _re.finditer(r"[「『]([^」』]+)[」』]", text):
                quote = m.group(0)
                content = m.group(1)
                speaker = ["李明", "林若雪", "王大爷"][i % 3]
                i += 1
                attrs.append({
                    "anchor": {"text": quote, "start": m.start(), "end": m.end()},
                    "speaker": speaker,
                    "confidence": 0.9 if i != 2 else 0.65,
                    "text": content,
                })
            return output_schema.model_validate({"data": attrs})

        if "VoiceRecommendation" in schema_name or "Voice" in schema_name:
            recs = [
                {"character_name": "林若雪", "suggested_voice_id": "female_tianmei_01", "reason": "少女匹配甜美音色"},
                {"character_name": "李明", "suggested_voice_id": "male_qingnian_01", "reason": "青年男声"},
                {"character_name": "王大爷", "suggested_voice_id": "male_cangsang_01", "reason": "老年沧桑"},
            ]
            return output_schema.model_validate({"data": recs})

        if "Chapter" in schema_name:
            text = _extract_text_block(prompt, "---LONG TEXT START---", "---LONG TEXT END---")
            return output_schema.model_validate({"data": [
                {"idx": 0, "title": "正文", "text": text}
            ]})

        # 兜底：尝试按 {data: []} 构造
        try:
            return output_schema.model_validate({"data": []})
        except Exception:
            raise NotImplementedError(
                f"MockLLM 未实现 schema={schema_name}，请补充 case。prompt preview={prompt[:200]}"
            )


def _extract_raw_from_prompt(p: str) -> str:
    return _extract_text_block(p, "---RAW TEXT START---", "---RAW TEXT END---")


def _extract_text_block(p: str, start: str, end: str) -> str:
    # 取最后一对标记的内容（因为 prompt 里 few-shot 也可能用了相同标记）
    a = p.rfind(start)
    b = p.rfind(end)
    if a < 0 or b < 0 or a >= b:
        return ""
    return p[a + len(start): b].strip()


class MockTTSProvider(BaseTTSProvider):
    """返回静音 MP3，不需要真的调 TTS。每段按长度估算时长。"""
    name = "mock_tts"

    def __init__(self):
        with open(VOICES_FILE, "r", encoding="utf-8") as f:
            self._voices = _json.load(f)

    async def list_voices(self) -> list[dict[str, Any]]:
        return self._voices

    async def synthesize_to_bytes(
        self, text: str, voice_id: str, *, emotion: str = "calm"
    ) -> bytes:
        # 每 5 个字约 1s，用静音 MP3
        dur_ms = max(200, len(text) * 200)
        return make_silent_mp3(dur_ms)

    async def synthesize_to_file(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        *,
        emotion: str = "calm",
    ) -> tuple[str, int]:
        data = await self.synthesize_to_bytes(text, voice_id, emotion=emotion)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
        return output_path, _estimate_mp3_duration_ms(data)
