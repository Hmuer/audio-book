"""P0-2 RED 测试：豆包官方音色表重建 + 元数据完整性。

数据来源（与 _BUILTIN_VOICES 注释对齐）：
  - 大模型 2.0 音色表：https://docs.volcengine.com/docs/6561/1257544?lang=zh

注：1.0 小模型音色（seed-tts-1.0 / 官方文档 97465 的 BV* 系列）已整表删除 ——
该账号未开通 1.0 资源，且 1.0 不支持语音指令与标签。内置表只保留 2.0。

本测试覆盖关键约束：
  T-VOICE-1  每条 voice 都带完整元数据（gender/age/scene/dialect/zh_tags/
             provider/languages/supports_emotion/supports_subtitle/
             supports_language/free/model）
  T-VOICE-2  id 都带 doubao: 前缀，且属于官方 2.0 namespace
             （*_uranus_bigtts / ICL_uranus_*_tob），不得出现 1.0 的 *_streaming
  T-VOICE-3  id 不能重复
  T-VOICE-4  内置表不得再出现 1.0 小模型 *_streaming id（整表已删除）
  T-VOICE-5  内置 2.0 音色无免费白名单（free 恒为 False）
  T-VOICE-6  多情感音色（supports_emotion=True）抽样存在
  T-VOICE-7  多语种音色（supports_language=True）抽样存在
  T-VOICE-8  覆盖官方 2.0 的场景类别
  T-VOICE-9  大模型 2.0 音色存在且 model=seed-tts-2.0
  T-VOICE-10 languages 字段承载中文方言码（2.0 表 dialect 字段留空）
  T-VOICE-11 languages 字段至少覆盖 zh / en / ja / id / esmx
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# T-VOICE-1：每条 voice 都带完整元数据
# ---------------------------------------------------------------------
def test_voice_has_required_metadata_fields():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    assert len(voices) >= 30, f"内置音色过少：{len(voices)}"

    required = [
        "id", "name", "gender", "age", "scene", "dialect", "languages",
        "zh_tags", "supports_emotion", "supports_subtitle", "supports_language",
        "free", "model", "provider",
    ]
    for v in voices:
        for k in required:
            assert k in v, f"音色 {v.get('id')} 缺字段 {k}"
        assert v["provider"] == "doubao"
        assert v["gender"] in ("male", "female", "neutral")
        assert v["age"] in ("child", "teen", "youth", "middle", "senior")
        assert isinstance(v["scene"], list) and v["scene"]
        assert isinstance(v["zh_tags"], list) and v["zh_tags"]
        assert isinstance(v["languages"], list) and v["languages"]
        assert isinstance(v["supports_emotion"], bool)
        assert isinstance(v["supports_subtitle"], bool)
        assert isinstance(v["supports_language"], bool)
        assert isinstance(v["free"], bool)
        assert v["model"] == "seed-tts-2.0"
        # dialect 必须是字符串（可为空）
        assert isinstance(v["dialect"], str)


# ---------------------------------------------------------------------
# T-VOICE-2：id 都带 doubao: 前缀，去前缀后是官方 2.0 voice_type
# ---------------------------------------------------------------------
def test_voice_ids_match_official_volcengine_voice_types():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    for v in voices:
        vid = v["id"]
        assert vid.startswith("doubao:"), f"id 必须 doubao: 前缀：{vid}"
        raw = vid.split(":", 1)[1]
        # 反例（旧自创拼写，必须一个都不能出现）
        assert not raw.endswith("_stream"), (
            f"id {vid} 用了自创拼写 _stream（官方是 _streaming）"
        )
        # 1.0 小模型拼写已整表下线：不得再出现 *_streaming
        assert not raw.endswith("_streaming"), (
            f"id {vid} 是已下线的 1.0 小模型音色"
        )
        assert not raw.startswith("zh_female_xxx"), (
            f"id {vid} 是自创占位 zh_female_xxx"
        )
        assert not raw.startswith("zh_male_xxx"), (
            f"id {vid} 是自创占位 zh_male_xxx"
        )
        # 官方 2.0 namespace 校验：zh_*_uranus_bigtts / en_*_uranus_bigtts /
        # ICL_uranus_*_tob
        valid = raw.endswith("_uranus_bigtts") or raw.startswith("ICL_uranus_")
        assert valid, (
            f"id {vid} 不属于任何已知 2.0 官方 namespace "
            f"（_uranus_bigtts / ICL_uranus_）"
        )


# ---------------------------------------------------------------------
# T-VOICE-3：id 不能重复
# ---------------------------------------------------------------------
def test_voice_ids_are_unique():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    ids = [v["id"] for v in voices]
    assert len(ids) == len(set(ids)), f"重复 id：{sorted(x for x in set(ids) if ids.count(x) > 1)}"


# ---------------------------------------------------------------------
# T-VOICE-4：内置表不得再出现 1.0 小模型 *_streaming id（整表已删除）
# ---------------------------------------------------------------------
def test_no_small_model_streaming_ids():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    small = [
        v["id"] for v in voices
        if v["id"].endswith("_streaming") or v["id"].endswith("_stream")
    ]
    assert not small, f"不应再内置 1.0 小模型音色（*_streaming）：{small}"


# ---------------------------------------------------------------------
# T-VOICE-5：内置 2.0 音色无免费白名单（free 恒为 False）
# ---------------------------------------------------------------------
def test_no_builtin_free_voices():
    """1.0「21 款免费音色」白名单已随音色表删除，内置 2.0 音色不应标 free=True。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    free_ids = {v["id"] for v in voices if v["free"]}
    assert not free_ids, f"内置表不应存在免费音色：{free_ids}"


# ---------------------------------------------------------------------
# T-VOICE-6：多情感音色（supports_emotion=True）抽样存在
# ---------------------------------------------------------------------
def test_emotion_supported_voices_exist():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    emotion_voices = [v for v in voices if v["supports_emotion"]]
    # 2.0 表绝大部分音色都标注了情感，至少要有 30 条
    assert len(emotion_voices) >= 30, (
        f"supports_emotion=True 的音色过少：{len(emotion_voices)}"
    )
    # 关键多情感音色必须在
    ids = {v["id"] for v in emotion_voices}
    must_have = {
        "doubao:zh_female_vv_uranus_bigtts",     # Vivi 2.0（S2S 多语种多方言）
        "doubao:zh_female_cancan_uranus_bigtts",  # 知性灿灿 2.0（角色扮演）
        "doubao:zh_male_qingcang_uranus_bigtts",  # 擎苍 2.0
        "doubao:zh_female_peiqi_uranus_bigtts",   # 佩奇猪 2.0（视频配音）
        "doubao:zh_male_sunwukong_uranus_bigtts", # 猴哥 2.0（视频配音）
    }
    missing = must_have - ids
    assert not missing, f"缺少关键多情感音色：{missing}"


# ---------------------------------------------------------------------
# T-VOICE-7：多语种音色（supports_language=True）抽样存在
# ---------------------------------------------------------------------
def test_language_supported_voices_exist():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    lang_voices = [v for v in voices if v["supports_language"]]
    # 2.0 表里 supports_language=True 的是 S2S 系（Vivi / 小何 / 云舟 / 小天）共 4 条
    assert len(lang_voices) >= 4, f"supports_language=True 的音色过少：{len(lang_voices)}"
    ids = {v["id"] for v in lang_voices}
    must_have = {
        "doubao:zh_female_vv_uranus_bigtts",      # Vivi 2.0（多语种 + 多方言）
        "doubao:zh_female_xiaohe_uranus_bigtts",  # 小何 2.0（多方言）
        "doubao:zh_male_m191_uranus_bigtts",      # 云舟 2.0（多方言）
        "doubao:zh_male_taocheng_uranus_bigtts",  # 小天 2.0（多方言）
    }
    missing = must_have - ids
    assert not missing, f"缺少关键多语种音色：{missing}"


# ---------------------------------------------------------------------
# T-VOICE-8：覆盖官方 2.0 场景类别
# ---------------------------------------------------------------------
def test_voices_cover_all_official_scene_categories():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    all_scenes: set[str] = set()
    for v in voices:
        all_scenes.update(v["scene"])

    required_categories = [
        "通用",
        "有声阅读",
        "视频配音",
        "教育",
        "客服",
        "角色扮演",
        "角色",
        "S2S",
        "儿童",
        "情感",
        "外语音色",
    ]
    missing = [c for c in required_categories if c not in all_scenes]
    assert not missing, f"缺少官方场景类别：{missing}"


# ---------------------------------------------------------------------
# T-VOICE-9：大模型 2.0 音色存在且 model=seed-tts-2.0
# ---------------------------------------------------------------------
def test_large_model_v2_voices_exist():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    v2 = [v for v in voices if v["model"] == "seed-tts-2.0"]
    # 大模型 2.0 至少收录 80+ 条
    assert len(v2) >= 80, f"大模型 2.0 音色过少：{len(v2)}"

    # 关键命名空间：_uranus_bigtts
    for v in v2:
        assert v["id"].endswith("_uranus_bigtts"), (
            f"大模型 2.0 id 拼写错误：{v['id']}"
        )


# ---------------------------------------------------------------------
# T-VOICE-10：中文方言码由 languages 字段承载（2.0 表 dialect 字段留空）
# ---------------------------------------------------------------------
def test_languages_field_covers_chinese_dialects():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    # 2.0 表不单独给 dialect 字段，方言信息写在 languages 里
    langs: set[str] = set()
    for v in voices:
        langs.update(v["languages"])

    expected = ["dongbei", "cantonese", "sichuan", "shaanxi", "shanghai", "beijing"]
    missing = [d for d in expected if d not in langs]
    assert not missing, f"缺少方言码：{missing}（当前：{sorted(langs)}）"


# ---------------------------------------------------------------------
# T-VOICE-11：languages 字段至少覆盖 zh / en / ja / id / esmx
# ---------------------------------------------------------------------
def test_languages_field_covers_multiple_official_languages():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    langs: set[str] = set()
    for v in voices:
        langs.update(v["languages"])

    required = {"zh", "en", "ja", "id", "esmx"}
    missing = required - langs
    assert not missing, f"缺少语种：{missing}（当前：{sorted(langs)}）"
