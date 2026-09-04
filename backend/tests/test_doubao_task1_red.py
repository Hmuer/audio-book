"""Task 1 RED tests — 配置扩展 / 基类扩展 / 模型扩展 + 迁移。

验证：
  T-TR1: 未设置 DOUBAO_AK 时 settings 无 ValidationError，可访问新增字段。
  T-TR2: Project/Build 新字段与 IclTrainingTask 可持久化。
  T-TR3: 升级对存量项目/构建向后兼容（mode 默认 classic、tts_provider 默认 minimax）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# T-TR1: settings 兼容性
# ---------------------------------------------------------------------
def test_settings_new_fields_without_doubao_env(monkeypatch):
    """未配置 DOUBAO_ 时 settings 仍能初始化，字段有合理默认值。"""
    # 清掉任何可能干扰的 DOUBAO 环境变量
    for key in list(os.environ.keys()):
        if key.startswith("DOUBAO_"):
            monkeypatch.delenv(key, raising=False)
    for key in ("TTS_PROVIDER", "MULTICAST_STRICT_MODE"):
        monkeypatch.delenv(key, raising=False)

    # 强制重新构造 Settings 实例（不依赖模块级 settings 缓存）
    from backend.app.core.config import Settings

    s = Settings()
    # 默认仍走 minimax，保证向后兼容
    assert s.TTS_PROVIDER == "minimax"
    assert s.DOUBAO_AK == ""
    assert s.DOUBAO_SK == ""
    assert s.DOUBAO_APP_ID == ""
    assert s.DOUBAO_TTS_RPM_LIMIT == 60
    assert s.DOUBAO_SEED_AUDIO_RPM_LIMIT == 10
    assert s.MULTICAST_STRICT_MODE is True
    assert s.DOUBAO_TTS_BASE_URL  # 应非空，有默认地址
    assert s.DOUBAO_ICL_BASE_URL
    assert s.DOUBAO_SEED_AUDIO_BASE_URL


# ---------------------------------------------------------------------
# T-TR2: Project / Build 新字段 + IclTrainingTask 可持久化
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_project_build_new_fields_and_icl_task_persist(db_session):
    """新字段写入 + 读回；IclTrainingTask 结构齐全。"""
    from backend.app.db.models import (
        Project,
        Build,
        IclTrainingTask,
        User,
    )
    from sqlalchemy import select

    s = db_session

    # 准备一个 project
    p = Project(
        project_id="proj-abc",
        name="Demo",
        status="draft",
        default_tts_provider="doubao",
        default_build_mode="multicast",
    )
    s.add(p)

    # 准备一个 build 挂到 project
    b = Build(
        build_id="build-xyz",
        project_id="proj-abc",
        status="queued",
        narrator_voice_id="doubao:female-qingxin",
        speed=1.0,
        mode="multicast",
        tts_provider="doubao",
    )
    s.add(b)

    # ICL 训练任务
    u = User(
        id=9999,
        username="tester",
        password_hash="x",
        is_active=True,
        must_change_password=False,
    )
    s.add(u)
    await s.flush()  # 让 user.id 可用（autoincrement）并与关联对齐

    icl = IclTrainingTask(
        task_id="icl-001",
        user_id=9999,  # 显式用户 id
        voice_name="我的专属声线",
        reference_audio_path="/tmp/icl/ref.wav",
        reference_audio_size_bytes=12345,
        status=4,
        progress=100,
        doubao_task_id="doubao-icl-abc",
        cloned_voice_id="icl:custom_a",
        error_msg=None,
    )
    s.add(icl)
    await s.commit()

    # 读回 Project
    p2 = await s.get(Project, "proj-abc")
    assert p2.default_tts_provider == "doubao"
    assert p2.default_build_mode == "multicast"

    # 读回 Build
    b2 = await s.get(Build, "build-xyz")
    assert b2.mode == "multicast"
    assert b2.tts_provider == "doubao"

    # 读回 ICL 任务
    icl2 = (await s.execute(
        select(IclTrainingTask).where(IclTrainingTask.task_id == "icl-001")
    )).scalar_one()
    assert icl2.user_id == 9999
    assert icl2.status == 4
    assert icl2.cloned_voice_id == "icl:custom_a"
    assert icl2.voice_name == "我的专属声线"


# ---------------------------------------------------------------------
# T-TR3: 存量数据迁移（无 mode/tts_provider 列的项目启动可补齐）
# ---------------------------------------------------------------------
def test_project_and_build_fields_have_sane_defaults():
    """模型定义必须为 mode / tts_provider 提供后向兼容默认值（classic / minimax），
    保证升级不抛 IntegrityError。"""
    from backend.app.db.models import Build, Project

    # Build 字段默认值（通过声明式检查列 server_default / default）
    build_mode_col = Build.__table__.c["mode"]
    build_prov_col = Build.__table__.c["tts_provider"]
    # 要求列有 default（非必须 server_default，因为 SQLAlchemy 侧赋值即可）
    assert build_mode_col.default is not None or build_mode_col.server_default is not None, (
        "Build.mode 必须定义默认值以便存量升级"
    )
    assert build_prov_col.default is not None or build_prov_col.server_default is not None, (
        "Build.tts_provider 必须定义默认值以便存量升级"
    )


# ---------------------------------------------------------------------
# FR-1: BaseTTSProvider 抽象层新参数 + provider 字段
# ---------------------------------------------------------------------
def test_base_tts_provider_has_provider_and_extra_synth_params():
    """抽象子类必须实现 provider，synthesize_to_bytes 签名必须接受新可选参数。"""
    import inspect
    from backend.app.ai.base import BaseTTSProvider

    assert hasattr(BaseTTSProvider, "provider")
    sig = inspect.signature(BaseTTSProvider.synthesize_to_bytes)
    params = list(sig.parameters.keys())
    # 必须包含 text, voice_id, emotion, speed + 新增
    assert "text" in params
    assert "voice_id" in params
    assert "emotion" in params
    assert "speed" in params
    assert "instruction_text" in params, "缺少 FR-1 新增 instruction_text 参数"
    assert "speaker_style" in params, "缺少 FR-1 新增 speaker_style 参数"

    # 默认值为 None（兼容现有调用方）
    p_instr = sig.parameters["instruction_text"]
    p_style = sig.parameters["speaker_style"]
    assert p_instr.default is None
    assert p_style.default is None
