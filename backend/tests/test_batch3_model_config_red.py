"""批次 3（整体优化）— 切模型 / 配豆包 相关回归测试。

覆盖：
- C-3 TTS 实例缓存纳入配置指纹（运行中改配置即时生效，无需重启）

历史：本文件原有 C-4（MiniMax 永久错误不重试）与 C-5（MiniMax model 前缀剥离）
两组用例，均只针对 MiniMax TTS；MiniMax 语音合成弃用后，相关用例随 provider 一起删除。
"""
from __future__ import annotations


# ---------------------------------------------------------------------
# C-3 配置指纹缓存
# ---------------------------------------------------------------------
def test_c3_fingerprint_tracks_tts_settings(monkeypatch):
    from backend.app.ai import factory as aifact
    from backend.app.core.config import settings

    fp1 = aifact._tts_config_fingerprint()
    monkeypatch.setattr(
        settings, "DOUBAO_TTS_RPM_LIMIT", int(settings.DOUBAO_TTS_RPM_LIMIT) + 7,
    )
    fp2 = aifact._tts_config_fingerprint()
    assert fp1 != fp2, "改动 DOUBAO_* 配置应使指纹变化"

    # 无关配置不应影响指纹（避免无谓重建）
    fp3 = aifact._tts_config_fingerprint()
    monkeypatch.setattr(settings, "LOG_LEVEL", "DEBUG")
    assert aifact._tts_config_fingerprint() == fp3, "日志级别不应影响 TTS 实例指纹"


def test_c3_config_change_rebuilds_instance_without_manual_clear(monkeypatch):
    from backend.app.ai import factory as aifact
    from backend.app.core.config import settings
    from backend.app.ai.providers.doubao.tts import (
        DoubaoTTSProvider,
        DoubaoTTSProviderV3,
    )

    # 关掉 mock 注入，直连 Registry
    monkeypatch.setattr(aifact, "_tts_instance", None)
    aifact.invalidate_tts_cache()
    prev = settings.DOUBAO_TTS_USE_V3
    try:
        settings.DOUBAO_TTS_USE_V3 = True
        inst_v3 = aifact.get_tts("doubao")
        assert isinstance(inst_v3, DoubaoTTSProviderV3)

        # 关键：**不手工清缓存**，仅改配置 → 必须拿到新类型实例
        settings.DOUBAO_TTS_USE_V3 = False
        inst_v1 = aifact.get_tts("doubao")
        assert isinstance(inst_v1, DoubaoTTSProvider), (
            f"C-3：配置变更后应重建实例，实际仍为 {type(inst_v1).__name__}"
        )
        assert inst_v1 is not inst_v3
    finally:
        settings.DOUBAO_TTS_USE_V3 = prev
        aifact.invalidate_tts_cache()


def test_c3_same_config_reuses_same_instance(monkeypatch):
    from backend.app.ai import factory as aifact

    monkeypatch.setattr(aifact, "_tts_instance", None)
    aifact.invalidate_tts_cache()
    try:
        a = aifact.get_tts("doubao")
        b = aifact.get_tts("doubao")
        assert a is b, "配置未变时应复用同一实例"
    finally:
        aifact.invalidate_tts_cache()
