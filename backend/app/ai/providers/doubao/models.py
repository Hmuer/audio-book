from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ModelOption:
    """单个模型选项（前端下拉 / 后端校验共用）。"""

    id: str
    label: str
    description: str
    deprecated: bool = False
    # 文档引用：用于前端 hover 提示
    doc_url: str = ""


class ModelOptionDict(TypedDict):
    """前端 JSON 序列化形态（dataclass → dict 时用）。"""

    id: str
    label: str
    description: str
    deprecated: bool
    doc_url: str


TTS_RESOURCE_IDS: tuple[ModelOption, ...] = (
    ModelOption(
        id="seed-tts-1.0",
        label="豆包语音合成 1.0（字符版）",
        description="小模型 1.0 字符版。仅适用于 1.0 音色，免费音色白名单在此通道。",
        doc_url="https://docs.volcengine.com/docs/6561/2528925?lang=zh",
    ),
    ModelOption(
        id="seed-tts-1.0-concurr",
        label="豆包语音合成 1.0（并发版）",
        description="小模型 1.0 并发版。仅适用于 1.0 音色，并发场景。",
        doc_url="https://docs.volcengine.com/docs/6561/2528925?lang=zh",
    ),
    ModelOption(
        id="seed-tts-2.0",
        label="豆包语音合成 2.0（推荐）",
        description="大模型 2.0。仅适用于 2.0 音色，支持 context_texts 语音指令。",
        doc_url="https://docs.volcengine.com/docs/6561/2528925?lang=zh",
    ),
)


_VALID_TTS_IDS: frozenset[str] = frozenset(m.id for m in TTS_RESOURCE_IDS)


def is_valid_resource_id(value: str) -> bool:
    """检查 `X-Api-Resource-Id` 是否在官方合成白名单中。"""
    return value in _VALID_TTS_IDS


def serialize_options(options: tuple[ModelOption, ...]) -> list[ModelOptionDict]:
    """dataclass → dict 列表，供 JSON 序列化。"""
    return [
        ModelOptionDict(
            id=m.id,
            label=m.label,
            description=m.description,
            deprecated=m.deprecated,
            doc_url=m.doc_url,
        )
        for m in options
    ]


def get_all_options() -> dict[str, list[ModelOptionDict]]:
    return {"tts_resource_ids": serialize_options(TTS_RESOURCE_IDS)}
