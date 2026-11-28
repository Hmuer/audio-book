"""残留失败自动补跑（看门狗）单元测试。

背景：对白批 / 角色切片重试耗尽后，prepare 以 partial_success 收尾；旧行为
必须用户手动点「重跑识别」才会补跑。现在 watchdog 自动扫描并重新拉起 prepare。
"""
from __future__ import annotations


def test_residual_failed_n_counts_batches_and_slices():
    from backend.app.services.project import _residual_failed_n

    assert _residual_failed_n({}) == 0
    assert _residual_failed_n({"dialogue_failed_batches": {"1": {"last_err": "x"}}}) == 1
    assert _residual_failed_n({"char_failed_slices": {"0": {"last_err": "y"}}}) == 1
    assert _residual_failed_n({
        "dialogue_failed_batches": {"1": {}, "2": {}},
        "char_failed_slices": {"0": {}},
    }) == 3
    # 非 dict（历史脏数据）不应炸
    assert _residual_failed_n({"dialogue_failed_batches": None}) == 0


def test_auto_retry_defaults_are_sane():
    from backend.app.services import project as p

    assert p._auto_retry_failed_enabled() is True
    assert p._auto_retry_failed_minutes() >= 0.5
    assert p._auto_retry_failed_max() >= 1
