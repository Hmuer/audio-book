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


def test_tts_resource_ids_contains_only_tts_values():
    from backend.app.ai.providers.doubao.models import TTS_RESOURCE_IDS

    ids = {m.id for m in TTS_RESOURCE_IDS}
    expected = {
        "seed-tts-1.0",
        "seed-tts-1.0-concurr",
        "seed-tts-2.0",
    }
    assert ids == expected

    assert TTS_RESOURCE_IDS[2].id == "seed-tts-2.0", "seed-tts-2.0 应在第 3 位"


def test_serialize_options_returns_dicts():
    """OPT-辅助：序列化函数返回 dict 列表，字段一致。"""
    from backend.app.ai.providers.doubao.models import (
        serialize_options,
        TTS_RESOURCE_IDS,
    )

    out = serialize_options(TTS_RESOURCE_IDS)
    assert isinstance(out, list)
    assert len(out) == len(TTS_RESOURCE_IDS)
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


def test_get_doubao_models_options_returns_only_tts_group(admin_token):
    from fastapi.testclient import TestClient
    from backend.app.main import app

    with TestClient(app) as client:
        r = client.get(
            "/api/doubao/models/options",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 200
    body = r.json()

    assert set(body) == {"tts_resource_ids"}
    tts_ids = {m["id"] for m in body["tts_resource_ids"]}
    assert tts_ids == {"seed-tts-1.0", "seed-tts-1.0-concurr", "seed-tts-2.0"}

    # 每项字段完整
    for grp in [body["tts_resource_ids"]]:
        for item in grp:
            assert "id" in item and "label" in item and "description" in item
            assert item["label"], f"{item['id']} label 不应为空"
            assert item["description"], f"{item['id']} description 不应为空"

def test_removed_training_options_and_resource_validation():
    from backend.app.ai.providers.doubao import models

    for name in ("TRAIN_MODEL_TYPES", "ICL_EXPRESSIVE_MODELS", "is_valid_train_model_type", "find_train_default"):
        assert not hasattr(models, name)
    for option in models.TTS_RESOURCE_IDS:
        assert models.is_valid_resource_id(option.id)
    for value in ("seed-icl-1.0", "seed-icl-1.0-concurr", "seed-icl-2.0", "invalid"):
        assert not models.is_valid_resource_id(value)
