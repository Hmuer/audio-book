"""豆包官方系统 TTS 2.0 模型选项。"""

from __future__ import annotations

from dataclasses import dataclass, asdict
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


# 豆包当前仅使用官方语音合成 2.0 系统音色。
TTS_RESOURCE_IDS: tuple[ModelOption, ...] = (
    ModelOption(
        id="seed-tts-2.0",
        label="豆包语音合成 2.0",
        description="官方 2.0 系统音色，支持 context_texts 语音指令。",
        doc_url="https://docs.volcengine.com/docs/6561/2528925?lang=zh",
    ),
)


def is_valid_resource_id(value: str) -> bool:
    return value == "seed-tts-2.0"


def get_all_options() -> dict[str, list[ModelOptionDict]]:
    return {"tts_resource_ids": [ModelOptionDict(**asdict(m)) for m in TTS_RESOURCE_IDS]}
