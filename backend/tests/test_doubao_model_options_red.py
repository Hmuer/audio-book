"""T-OPT-MOD：豆包模型选项静态清单 + GET /api/doubao/models/options 测试。

数据来源：
- 训练 model_type：官方训练接口 body.model_type 字段（/api/v3/tts/voice_clone）
- 合成 X-Api-Resource-Id：https://docs.volcengine.com/docs/6561/2528925?lang=zh
- v3 大模型 API：https://www.volcengine.com/docs/6561/1598757?lang=zh

覆盖：
  T-OPT-1 静态常量：train_model_types 含 ICL2.0 / ICL1.0 / DiT 三项
  T-OPT-2 静态常量：tts_resource_ids 含全部 6 项官方 resource_id
  T-OPT-3 静态常量：icl_expressive_models 含 standard / expressive 两项
  T-OPT-4 白名单校验：合法值通过、非法值拒绝、None 视为合法
  T-OPT-5 find_train_default 返回 ICL2.0
  T-OPT-6 GET /api/doubao/models/options 返回结构稳定
  T-OPT-7 GET /api/doubao/models/options 必须鉴权（未登录 401）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# 直接测试静态常量（不依赖鉴权）
# ---------------------------------------------------------------------


def test_train_model_types_contains_three_official_algorithms():
    """T-OPT-1：训练 model_type 三选项完整。"""
    from backend.app.ai.providers.doubao.models import TRAIN_MODEL_TYPES

    ids = {m.id for m in TRAIN_MODEL_TYPES}
    assert ids == {"ICL2.0", "ICL1.0", "DiT"}, f"训练 model_type 应为 3 项，实际 {ids}"

    # 每项字段齐全
    for m in TRAIN_MODEL_TYPES:
        assert m.label, f"{m.id} 缺少 label"
        assert m.description, f"{m.id} 缺少 description"
        assert m.doc_url.startswith("https://"), f"{m.id} doc_url 不是 https"


def test_tts_resource_ids_contains_all_six_official_values():
    """T-OPT-2：合成 X-Api-Resource-Id 六选项完整。"""
    from backend.app.ai.providers.doubao.models import TTS_RESOURCE_IDS

    ids = {m.id for m in TTS_RESOURCE_IDS}
    expected = {
        "seed-tts-1.0",
        "seed-tts-1.0-concurr",
        "seed-tts-2.0",
        "seed-icl-1.0",
        "seed-icl-1.0-concurr",
        "seed-icl-2.0",
    }
    assert ids == expected, f"合成 resource_id 应为 6 项，实际 {ids}"

    # 推荐项首位应是 seed-tts-2.0 和 seed-icl-2.0（按官方推荐顺序）
    assert TTS_RESOURCE_IDS[2].id == "seed-tts-2.0", "seed-tts-2.0 应在第 3 位"
    assert TTS_RESOURCE_IDS[5].id == "seed-icl-2.0", "seed-icl-2.0 应在第 6 位"


def test_icl_expressive_models_has_standard_and_expressive():
    """T-OPT-3：ICL 2.0 增强 model 两选项完整。"""
    from backend.app.ai.providers.doubao.models import ICL_EXPRESSIVE_MODELS

    ids = {m.id for m in ICL_EXPRESSIVE_MODELS}
    assert ids == {"seed-tts-2.0-standard", "seed-tts-2.0-expressive"}

    # standard 必须是第一位（默认）
    assert ICL_EXPRESSIVE_MODELS[0].id == "seed-tts-2.0-standard"


def test_train_model_type_validation_whitelist():
    """T-OPT-4：白名单校验。None 视为合法（走默认）；非法值拒绝。"""
    from backend.app.ai.providers.doubao.models import is_valid_train_model_type

    # 合法
    assert is_valid_train_model_type(None) is True
    assert is_valid_train_model_type("ICL2.0") is True
    assert is_valid_train_model_type("ICL1.0") is True
    assert is_valid_train_model_type("DiT") is True

    # 非法
    assert is_valid_train_model_type("icl2.0") is False, "大小写敏感"
    assert is_valid_train_model_type("seed-icl-2.0") is False, "seed-icl-2.0 是合成 resource_id 不是训练 model_type"
    assert is_valid_train_model_type("ICL3.0") is False
    assert is_valid_train_model_type("") is False
    assert is_valid_train_model_type("自定义") is False


def test_find_train_default_returns_icl2():
    """T-OPT-5：默认训练 model_type 是 ICL2.0。"""
    from backend.app.ai.providers.doubao.models import find_train_default

    assert find_train_default() == "ICL2.0"


def test_serialize_options_returns_dicts():
    """OPT-辅助：序列化函数返回 dict 列表，字段一致。"""
    from backend.app.ai.providers.doubao.models import (
        serialize_options,
        TRAIN_MODEL_TYPES,
    )

    out = serialize_options(TRAIN_MODEL_TYPES)
    assert isinstance(out, list)
    assert len(out) == len(TRAIN_MODEL_TYPES)
    for d in out:
        assert set(d.keys()) >= {"id", "label", "description", "deprecated", "doc_url"}
        assert isinstance(d["id"], str)
        assert isinstance(d["label"], str)
        assert isinstance(d["description"], str)
        assert isinstance(d["deprecated"], bool)


# ---------------------------------------------------------------------
# HTTP 端点测试（需鉴权）
# ---------------------------------------------------------------------


def test_get_doubao_models_options_requires_auth():
    """T-OPT-7：未登录访问应 401。"""
    from fastapi.testclient import TestClient
    from backend.app.main import app

    with TestClient(app) as client:
        r = client.get("/api/doubao/models/options")
    assert r.status_code == 401, f"未鉴权应 401，实际 {r.status_code}"


def test_get_doubao_models_options_returns_three_groups(admin_token):
    """T-OPT-6：已登录返回三组数据，结构稳定。"""
    from fastapi.testclient import TestClient
    from backend.app.main import app

    with TestClient(app) as client:
        r = client.get(
            "/api/doubao/models/options",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 200
    body = r.json()

    # 三组 key 必须存在
    assert set(body.keys()) >= {
        "train_model_types",
        "tts_resource_ids",
        "icl_expressive_models",
    }

    # 训练 3 项
    train_ids = [m["id"] for m in body["train_model_types"]]
    assert sorted(train_ids) == ["DiT", "ICL1.0", "ICL2.0"]

    # 合成 6 项
    tts_ids = [m["id"] for m in body["tts_resource_ids"]]
    assert sorted(tts_ids) == sorted([
        "seed-tts-1.0",
        "seed-tts-1.0-concurr",
        "seed-tts-2.0",
        "seed-icl-1.0",
        "seed-icl-1.0-concurr",
        "seed-icl-2.0",
    ])

    # ICL 增强 2 项
    exp_ids = [m["id"] for m in body["icl_expressive_models"]]
    assert sorted(exp_ids) == ["seed-tts-2.0-expressive", "seed-tts-2.0-standard"]

    # 每项字段完整
    for grp in body.values():
        for item in grp:
            assert "id" in item and "label" in item and "description" in item
            assert item["label"], f"{item['id']} label 不应为空"
            assert item["description"], f"{item['id']} description 不应为空"