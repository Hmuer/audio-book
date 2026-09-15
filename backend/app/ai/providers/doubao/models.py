"""豆包模型清单静态枚举。

供两个场景使用：
1. 训练：声音复刻训练任务 `model_type` 取值（POST /api/v3/tts/voice_clone）
2. 合成：TTS / ICL 合成 `X-Api-Resource-Id` 取值（POST /api/v3/tts/unidirectional）

来源：
- v3 HTTP 单向流式文档：https://docs.volcengine.com/docs/6561/2528925?lang=zh
- v3 大模型 API 列表：https://www.volcengine.com/docs/6561/1598757?lang=zh
- 训练接口字段：`model_type ∈ {ICL1.0, ICL2.0, DiT}`（官方训练接口字段名）

为什么是静态而非动态拉取：
- 火山引擎未暴露"模型列表"接口；只有"音色列表"（/voice/list）
- 文档更新频率不高，静态枚举更可控
- 火山加新模型时手动更新本文件即可（带版本日期）

注意：
- 复刻音色（id 以 S_ / icl_ 开头）在 v1 协议下合成必须用 cluster=volcano_icl（不在本表，路由逻辑见 tts.py）
- 复刻音色在 v3 协议下合成必须用 X-Api-Resource-Id=seed-icl-2.0（见下表 ICL_RESOURCE_IDS）
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal, TypedDict


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


# ----------------------------------------------------------------------
# 训练 model_type：3 选 1
# ----------------------------------------------------------------------
# 官方训练接口文档（/api/v3/tts/voice_clone body.model_type）：
# - ICL2.0：默认推荐，2.0 复刻算法
# - ICL1.0：旧版，1.0 复刻算法（音色合成时配 seed-icl-1.0 / seed-icl-1.0-concurr）
# - DiT：dit 算法（model_type=2 标准版 / model_type=3 还原版，合成走 seed-tts-2.0）

TRAIN_MODEL_TYPES: tuple[ModelOption, ...] = (
    ModelOption(
        id="ICL2.0",
        label="ICL 2.0（推荐）",
        description="声音复刻 2.0 算法。训练后合成走 seed-icl-2.0；多语种前端默认开启。",
        doc_url="https://www.volcengine.com/docs/6561/1598757?lang=zh",
    ),
    ModelOption(
        id="ICL1.0",
        label="ICL 1.0",
        description="声音复刻 1.0 算法。训练后合成走 seed-icl-1.0 或 seed-icl-1.0-concurr。",
        doc_url="https://www.volcengine.com/docs/6561/1598757?lang=zh",
    ),
    ModelOption(
        id="DiT",
        label="DiT",
        description="dit 算法。训练后合成走 seed-tts-2.0；model_type=2 标准版 / 3 还原版。",
        doc_url="https://www.volcengine.com/docs/6561/1598757?lang=zh",
    ),
)


# ----------------------------------------------------------------------
# 合成 X-Api-Resource-Id：6 选 1（v3 HTTP）
# ----------------------------------------------------------------------
# 官方 v3 单向流式接口 X-Api-Resource-Id 完整可选值（2026-09-14 文档）：
# - TTS 系列：seed-tts-1.0 / seed-tts-1.0-concurr / seed-tts-2.0
# - ICL 系列：seed-icl-1.0 / seed-icl-1.0-concurr / seed-icl-2.0

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
    ModelOption(
        id="seed-icl-1.0",
        label="豆包声音复刻 1.0（字符版）",
        description="ICL 1.0 训练音色专用通道。仅适用于 ICL1.0 训练的音色。",
        doc_url="https://docs.volcengine.com/docs/6561/2528925?lang=zh",
    ),
    ModelOption(
        id="seed-icl-1.0-concurr",
        label="豆包声音复刻 1.0（并发版）",
        description="ICL 1.0 训练音色专用通道，并发场景。仅适用于 ICL1.0 训练的音色。",
        doc_url="https://docs.volcengine.com/docs/6561/2528925?lang=zh",
    ),
    ModelOption(
        id="seed-icl-2.0",
        label="豆包声音复刻 2.0（推荐）",
        description="ICL 2.0 训练音色专用通道。仅适用于 ICL2.0 训练的音色。",
        doc_url="https://docs.volcengine.com/docs/6561/2528925?lang=zh",
    ),
)


# ----------------------------------------------------------------------
# 合成增强 model：仅 ICL 2.0 生效，2 选 1
# ----------------------------------------------------------------------
# v3 文档 req_params.model 字段：仅对 ICL 2.0 音色生效
# - seed-tts-2.0-standard：标准版（默认），延时更优
# - seed-tts-2.0-expressive：表现力增强版，支持语音指令 QA / 语音标签 Cot

ICL_EXPRESSIVE_MODELS: tuple[ModelOption, ...] = (
    ModelOption(
        id="seed-tts-2.0-standard",
        label="标准版（默认）",
        description="延时更优；不支持语音指令 QA / 语音标签 Cot。",
        doc_url="https://docs.volcengine.com/docs/6561/2528925?lang=zh",
    ),
    ModelOption(
        id="seed-tts-2.0-expressive",
        label="表现力增强版",
        description="表现力较强，支持语音指令 QA / 语音标签 Cot；存在效果抽卡。",
        doc_url="https://docs.volcengine.com/docs/6561/2528925?lang=zh",
    ),
)


# ----------------------------------------------------------------------
# 校验 / 序列化工具
# ----------------------------------------------------------------------

# 有效 ID 集合（用于校验前端传入值是否在白名单内）
_VALID_TRAIN_IDS: frozenset[str] = frozenset(m.id for m in TRAIN_MODEL_TYPES)
_VALID_TTS_IDS: frozenset[str] = frozenset(m.id for m in TTS_RESOURCE_IDS)
_VALID_EXPRESSIVE_IDS: frozenset[str] = frozenset(m.id for m in ICL_EXPRESSIVE_MODELS)


def is_valid_train_model_type(value: str | None) -> bool:
    """检查 `model_type` 是否在官方训练白名单中。

    None 视为合法（调用方应回退默认）。
    """
    if value is None:
        return True
    return value in _VALID_TRAIN_IDS


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
    """一次性返回三组常量，供 GET /api/doubao/models/options。"""
    return {
        "train_model_types": serialize_options(TRAIN_MODEL_TYPES),
        "tts_resource_ids": serialize_options(TTS_RESOURCE_IDS),
        "icl_expressive_models": serialize_options(ICL_EXPRESSIVE_MODELS),
    }


def find_train_default() -> str:
    """默认训练 model_type（ICL2.0 推荐）。"""
    return TRAIN_MODEL_TYPES[0].id  # ICL2.0


# 仅供 typing.Literal 反向引用，避免未使用告警
_LiteralKind = Literal["train_model_types", "tts_resource_ids", "icl_expressive_models"]