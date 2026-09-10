"""P2-1：豆包 ListSpeakers 客户端 + 三层合并策略 测试。

覆盖：
  T-LS1  HMAC-SHA256 签名生成（结构 + 长度 + 一致性）
  T-LS2  单页 _fetch_one_page 调用正确性（URL / method / headers）
  T-LS3  fetch_remote_voices 多页循环：page 拉完自动停止
  T-LS4  Speaker → _BUILTIN_VOICES 结构转换（中英文、性别年龄、Emotions）
  T-LS5  save/load_remote_voices 原子写 + 容错（坏 JSON / 不存在）
  T-LS6  ensure_remote_voices_synced_once：有缓存时复用
  T-LS7  list_voices 三层合并：自定义 > 远程 > 内置
  T-LS8  refresh_remote_voices：缺失凭据时优雅返回 0
  T-LS9  refresh_remote_voices：网络错时优雅返回 0（不抛错）
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------
# T-LS1 HMAC 签名
# ---------------------------------------------------------------------
def test_p2_1_ls1_hmac_authorization_structure():
    """构造的 Authorization 头符合 ListSpeakers 规范：HMAC-SHA256 + Credential + SignedHeaders。"""
    from backend.app.services.doubao_list_speakers import _build_authorization

    auth = _build_authorization(
        ak="test-ak", sk="test-sk",
        host="open.volcengineapi.com",
        path="/",
        region="cn-north-1",
        service="speech_saas_prod",
        body="{}",
        query="Action=ListSpeakers&Version=2025-05-20",
        x_date="20260105T072305Z",
        x_content_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    # 三段：算法 + Credential=... + SignedHeaders=... + Signature=...
    assert auth.startswith("HMAC-SHA256 ")
    assert "Credential=test-ak/" in auth
    assert "SignedHeaders=content-type;host;x-content-sha256;x-date" in auth
    # Signature 是 64 hex
    sig = auth.split("Signature=", 1)[-1]
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)
    # 同样的输入产同样的签名
    auth2 = _build_authorization(
        ak="test-ak", sk="test-sk",
        host="open.volcengineapi.com",
        path="/",
        region="cn-north-1",
        service="speech_saas_prod",
        body="{}",
        query="Action=ListSpeakers&Version=2025-05-20",
        x_date="20260105T072305Z",
        x_content_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    assert auth == auth2


def test_p2_1_ls1_signing_key_derivation():
    """build_signing_key 走 SK→date→region→service→request 五层 HMAC。"""
    from backend.app.services.doubao_list_speakers import _build_signing_key

    k = _build_signing_key("test-sk", "20260105", "cn-north-1", "speech_saas_prod")
    # 派生 key 长度 32 bytes (sha256)
    assert isinstance(k, bytes)
    assert len(k) == 32


# ---------------------------------------------------------------------
# T-LS4 Speaker → _BUILTIN_VOICES 转换
# ---------------------------------------------------------------------
def test_p2_1_ls4_speaker_to_builtin_entry_basic():
    """中文 Gender/Age 翻译；Emotions 非空 → supports_emotion=True；语言截 - 前缀。"""
    from backend.app.services.doubao_list_speakers import _speaker_to_builtin_entry

    raw = {
        "VoiceType": "zh_female_cute_uranus_bigtts",
        "Name": "甜美女声",
        "Gender": "女",
        "Age": "青年",
        "Categories": [{"Categories": ["多语种", "中文"]}, {"Categories": ["情感"]}],
        "Languages": [{"Language": "zh-cn"}, {"Language": "en"}],
        "Emotions": [{"Value": "happy", "Label": "开心"}],
        "ResourceID": "seed-tts-2.0",
    }
    entry = _speaker_to_builtin_entry(raw, default_resource="seed-tts-1.0")
    assert entry["id"] == "zh_female_cute_uranus_bigtts"
    assert entry["name"] == "甜美女声"
    assert entry["gender"] == "female"
    assert entry["age"] == "youth"
    assert entry["model"] == "seed-tts-2.0"  # 用 speaker.ResourceID 而非 default
    assert entry["supports_emotion"] is True
    assert entry["languages"] == ["zh", "en"]
    assert "多语种" in entry["scene"]
    assert "情感" in entry["scene"]


def test_p2_1_ls4_speaker_to_builtin_entry_no_emotion():
    """无 Emotions → supports_emotion=False；免费字段默认 False。"""
    from backend.app.services.doubao_list_speakers import _speaker_to_builtin_entry

    raw = {
        "VoiceType": "BV001_streaming",
        "Name": "通用女声",
        "Gender": "女",
        "Age": "青年",
        "Languages": [{"Language": "zh"}],
        "ResourceID": "seed-tts-1.0",
    }
    entry = _speaker_to_builtin_entry(raw, default_resource="seed-tts-1.0")
    assert entry["supports_emotion"] is False
    assert entry["free"] is False  # 远程表不带 free 字段，按 False
    assert entry["languages"] == ["zh"]


def test_p2_1_ls4_speaker_fallback_unknown_gender_age():
    """无法识别的 Gender/Age → 兜底 neutral / youth。"""
    from backend.app.services.doubao_list_speakers import _speaker_to_builtin_entry

    raw = {
        "VoiceType": "BV_unknown",
        "Name": "x",
        "Gender": "外星人",
        "Age": "远古",
        "Languages": [],
        "ResourceID": "seed-tts-1.0",
    }
    entry = _speaker_to_builtin_entry(raw, default_resource="seed-tts-1.0")
    assert entry["gender"] == "neutral"
    assert entry["age"] == "youth"
    assert entry["languages"] == ["zh"]  # 无语言时兜底中文


# ---------------------------------------------------------------------
# T-LS5 save/load 原子写 + 容错
# ---------------------------------------------------------------------
def test_p2_1_ls5_save_and_load_remote_voices(tmp_path, monkeypatch):
    """save_remote_voices 原子写 + load_remote_voices 正确读回 + 不存在/坏 JSON 容错。"""
    from backend.app.services import doubao_list_speakers as mod

    monkeypatch.setattr(mod.settings, "DATA_DIR", tmp_path)

    sample = [
        {"id": "BV001_streaming", "name": "通用女声", "gender": "female", "model": "seed-tts-1.0"},
        {"id": "zh_male_uranus_bigtts", "name": "磁性男声", "gender": "male", "model": "seed-tts-2.0"},
    ]
    p = mod.save_remote_voices(sample)
    assert p.is_file()
    payload = json.loads(p.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert "synced_at" in payload
    assert payload["voices"] == sample

    # load_remote_voices 读回
    loaded = mod.load_remote_voices()
    assert loaded == sample


def test_p2_1_ls5_load_remote_voices_missing(tmp_path, monkeypatch):
    """文件不存在 → 返回空 list（不抛错）。"""
    from backend.app.services import doubao_list_speakers as mod

    monkeypatch.setattr(mod.settings, "DATA_DIR", tmp_path)
    assert mod.load_remote_voices() == []


def test_p2_1_ls5_load_remote_voices_corrupted(tmp_path, monkeypatch):
    """JSON 损坏 → 返回空 list（不抛错）。"""
    from backend.app.services import doubao_list_speakers as mod

    monkeypatch.setattr(mod.settings, "DATA_DIR", tmp_path)
    p = mod.remote_voices_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    assert mod.load_remote_voices() == []


# ---------------------------------------------------------------------
# T-LS6 ensure 复用缓存
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_1_ls6_ensure_uses_cache_when_present(tmp_path, monkeypatch):
    """磁盘已有 voices_doubao_remote.json → 不再调 refresh_remote_voices。"""
    from backend.app.services import doubao_list_speakers as mod

    monkeypatch.setattr(mod.settings, "DATA_DIR", tmp_path)
    mod.save_remote_voices([{"id": "cached_voice"}])

    called = {"refresh": 0}
    async def _fake_refresh():
        called["refresh"] += 1
        return 99
    monkeypatch.setattr(mod, "refresh_remote_voices", _fake_refresh)

    n = await mod.ensure_remote_voices_synced_once()
    assert n == 1
    assert called["refresh"] == 0


@pytest.mark.asyncio
async def test_p2_1_ls6_ensure_calls_refresh_when_missing(tmp_path, monkeypatch):
    """磁盘无文件 → 调 refresh_remote_voices 一次。"""
    from backend.app.services import doubao_list_speakers as mod

    monkeypatch.setattr(mod.settings, "DATA_DIR", tmp_path)
    called = {"refresh": 0}

    async def _fake_refresh():
        called["refresh"] += 1
        mod.save_remote_voices([{"id": "fetched_voice"}])
        return 1

    monkeypatch.setattr(mod, "refresh_remote_voices", _fake_refresh)
    n = await mod.ensure_remote_voices_synced_once()
    assert called["refresh"] == 1
    assert n == 1


# ---------------------------------------------------------------------
# T-LS7 list_voices 三层合并
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_1_ls7_list_voices_three_layer_merge(tmp_path, monkeypatch):
    """内置 < 远程 < 自定义：同 id 时自定义覆盖远程，远程覆盖内置。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3
    from backend.app.services import doubao_list_speakers as mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DATA_DIR", tmp_path)

    # 远程音色覆盖了内置的一个 id
    mod.save_remote_voices([
        {"id": "BV001_streaming", "name": "远程版通用女声", "model": "seed-tts-2.0"},
        {"id": "BV999_streaming", "name": "仅远程", "model": "seed-tts-1.0"},
    ])
    # 自定义覆盖了远程的一个 id
    custom = tmp_path / "voices_doubao.json"
    custom.write_text(
        json.dumps([
            {"id": "BV001_streaming", "name": "用户自定义版", "zh_tags": ["自定义"]},
        ], ensure_ascii=False),
        encoding="utf-8",
    )

    v3 = DoubaoTTSProviderV3()
    voices = await v3.list_voices()
    by_id = {v["id"]: v for v in voices}

    # 自定义顶：BV001 → 用户自定义版
    assert by_id["doubao:BV001_streaming"]["name"] == "用户自定义版"
    # 远程：BV999（仅远程有）
    assert "doubao:BV999_streaming" in by_id
    # 内置：BV002 等其他内置仍存在
    assert "doubao:BV002_streaming" in by_id


# ---------------------------------------------------------------------
# T-LS8 refresh 缺失凭据
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_1_ls8_refresh_skips_when_no_credentials(monkeypatch):
    """DOUBAO_AK / SK 留空 → refresh_remote_voices 返回 0，不抛错。"""
    from backend.app.services import doubao_list_speakers as mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "")
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_SK", "")
    assert await mod.refresh_remote_voices() == 0


# ---------------------------------------------------------------------
# T-LS9 refresh 网络错
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_1_ls9_refresh_swallows_network_errors(monkeypatch, tmp_path):
    """fetch_remote_voices 抛 RuntimeError → refresh_remote_voices 返回 0（不抛错）。"""
    from backend.app.services import doubao_list_speakers as mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "ak")
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_SK", "sk")

    async def _fake_fetch_raises(*a, **kw):
        raise RuntimeError("网络爆炸")
    monkeypatch.setattr(mod, "fetch_remote_voices", _fake_fetch_raises)

    assert await mod.refresh_remote_voices() == 0


# ---------------------------------------------------------------------
# T-LS2/3 单页 fetch + 多页循环（mock HTTP）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_1_ls3_fetch_remote_voices_paginates(monkeypatch):
    """Total=70 PageSize=30 → 拉 3 页（30+30+10）后停止。"""
    from backend.app.services import doubao_list_speakers as mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "ak")
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_SK", "sk")

    page_calls: list[int] = []

    async def _fake_one_page(client, *, ak, sk, page, resource_id):
        page_calls.append(page)
        if page == 1:
            return {
                "Result": {
                    "Total": 70,
                    "Speakers": [
                        {"VoiceType": f"v_p1_{i}", "Name": f"P1-{i}", "ResourceID": resource_id,
                         "Gender": "女", "Age": "青年", "Languages": [{"Language": "zh"}]}
                        for i in range(30)
                    ],
                }
            }
        if page == 2:
            return {
                "Result": {
                    "Total": 70,
                    "Speakers": [
                        {"VoiceType": f"v_p2_{i}", "Name": f"P2-{i}", "ResourceID": resource_id,
                         "Gender": "男", "Age": "中年", "Languages": [{"Language": "zh"}]}
                        for i in range(30)
                    ],
                }
            }
        if page == 3:
            return {
                "Result": {
                    "Total": 70,
                    "Speakers": [
                        {"VoiceType": f"v_p3_{i}", "Name": f"P3-{i}", "ResourceID": resource_id,
                         "Gender": "女", "Age": "青年", "Languages": [{"Language": "zh"}]}
                        for i in range(10)
                    ],
                }
            }
        raise AssertionError(f"不应再翻页：page={page}")

    monkeypatch.setattr(mod, "_fetch_one_page", _fake_one_page)
    voices = await mod.fetch_remote_voices(resource_ids=("seed-tts-1.0",))
    assert page_calls == [1, 2, 3]
    assert len(voices) == 70
    # 第三页数量不足时停止
    assert any(v["id"] == "v_p3_9" for v in voices)


@pytest.mark.asyncio
async def test_p2_1_ls3_fetch_raises_when_no_credentials(monkeypatch):
    """fetch_remote_voices 在 AK/SK 缺失时直接抛 RuntimeError（让调用方决定降级）。"""
    from backend.app.services import doubao_list_speakers as mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "")
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_SK", "")

    with pytest.raises(RuntimeError, match="DOUBAO_AK"):
        await mod.fetch_remote_voices()
