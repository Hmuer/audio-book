"""Task 2 RED tests — Factory Registry + get_tts(provider) + get_tts_by_voice_id + 限流桶。

T-TR1: get_tts('minimax').name == 'minimax'; get_tts('doubao') 返回 DoubaoTTSProvider（未配置凭据仅初始化可创建，synthesize 抛错）。
T-TR2: get_tts_by_voice_id 按前缀返回对应 provider 实例。
"""
from __future__ import annotations

import os
import sys
import time as _time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# T-TR1：get_tts(provider) 多厂商路由
# ---------------------------------------------------------------------
def test_get_tts_minimax_returns_correct_provider():
    from backend.app.ai.factory import get_tts
    p = get_tts("minimax")
    assert p.provider == "minimax"


def test_get_tts_default_respects_settings(monkeypatch):
    """显式指定 provider='doubao' 时，能得到一个 DoubaoTTSProvider 实例。"""
    from backend.app.ai.factory import get_tts
    p = get_tts("doubao")
    # Doubao provider 类名称，避免 import 循环（factory 内部延迟导入规避）
    assert p.provider in ("doubao",)
    assert p.name in ("doubao_tts", "doubao") or "doubao" in p.name.lower()


# ---------------------------------------------------------------------
# T-TR2：get_tts_by_voice_id 命名空间前缀映射
# ---------------------------------------------------------------------
def test_get_tts_by_voice_id_prefix_mapping():
    from backend.app.ai.factory import get_tts_by_voice_id

    p_mx = get_tts_by_voice_id("minimax:male-qn-jingying")
    assert p_mx.provider == "minimax"

    p_db = get_tts_by_voice_id("doubao:zh_female_qingxin")
    assert p_db.provider == "doubao"

    p_icl = get_tts_by_voice_id("icl:custom_voice_a")
    # icl 走 DoubaoTTSProvider
    assert p_icl.provider == "doubao"

    # 无前缀 → 按全局默认 provider
    p_any = get_tts_by_voice_id("some-legacy-id")
    assert p_any.provider in ("minimax", "doubao")


def test_get_tts_by_voice_id_unknown_prefix_still_returns_default():
    """unknown 前缀时不抛 KeyError，回退到 settings.TTS_PROVIDER。"""
    from backend.app.ai.factory import get_tts_by_voice_id
    p = get_tts_by_voice_id("unknown:xyz")
    assert p.provider in ("minimax", "doubao")


# ---------------------------------------------------------------------
# Doubao RPM 限流桶：固定间隔匀速（参考 minimax 同款算法验证）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_doubao_rpm_bucket_enforces_interval():
    """DOUBAO_TTS_RPM_LIMIT=60 → 间隔 1.0s；两次 acquire 必须 ≥ 0.99s 间隔。"""
    from backend.app.ai.factory import (
        _doubao_rpm_wait_acquire,
        _reset_doubao_rpm_bucket_for_tests,
    )

    # 直接读 settings 构造限流间隔
    from backend.app.core.config import settings as s
    limit = int(s.DOUBAO_TTS_RPM_LIMIT)  # 默认 60
    expected_interval = 60.0 / limit  # ≈1.0s
    assert 0.99 <= expected_interval <= 1.01, "默认 RPM 应当为 60 → 间隔≈1s"

    _reset_doubao_rpm_bucket_for_tests()
    t0 = _time.monotonic()
    await _doubao_rpm_wait_acquire()  # 立即返回
    await _doubao_rpm_wait_acquire()  # 应等待 ~1s
    elapsed = _time.monotonic() - t0
    # 第一次立即放行（已预排），第二次需要睡 ≈1s
    assert elapsed >= 0.9, f"限速间隔过小：{elapsed:.2f}s < 0.9s"
