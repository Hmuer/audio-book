"""P0-2 RED 测试：豆包官方音色表重建 + 元数据完整性。

数据来源（与 _BUILTIN_VOICES 注释对齐）：
  - 小模型音色表：https://docs.volcengine.com/docs/6561/97465?lang=zh
  - 大模型 2.0 音色表：https://docs.volcengine.com/docs/6561/1257544?lang=zh

本测试覆盖 6 个关键约束：
  T-VOICE-1  每条 voice 都带完整元数据（gender/age/scene/dialect/zh_tags/
             provider/languages/supports_emotion/supports_subtitle/
             supports_language/free/model）
  T-VOICE-2  id 都带 doubao: 前缀且去掉前缀后是官方真实 voice_type
             （无自创的 BVxxx_stream、zh_female_xxx、zh_male_xxx 等）
  T-VOICE-3  id 不能重复
  T-VOICE-4  小模型 id 拼写统一是 *_streaming（不是 *_stream）
  T-VOICE-5  21 款免费音色白名单全部存在（FAQ 锁定列表）
  T-VOICE-6  多情感音色（supports_emotion=True）抽样存在
  T-VOICE-7  多语种音色（supports_language=True）抽样存在
  T-VOICE-8  至少覆盖：通用 / 有声阅读 / 智能助手 / 视频配音 / 特色音色 /
             广告配音 / 新闻播报 / 教育 / 多语种 / 方言 / 大模型角色
             11 大官方场景类别
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
        assert v["model"] in ("seed-tts-1.0", "seed-tts-2.0")
        # dialect 必须是字符串（可为空）
        assert isinstance(v["dialect"], str)


# ---------------------------------------------------------------------
# T-VOICE-2：id 都带 doubao: 前缀，去前缀后是官方 voice_type（无自创 id）
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
        assert not raw.startswith("zh_female_xxx"), (
            f"id {vid} 是自创占位 zh_female_xxx"
        )
        assert not raw.startswith("zh_male_xxx"), (
            f"id {vid} 是自创占位 zh_male_xxx"
        )
        # 官方 namespace 校验：BVxxx_streaming / zh_*_uranus_bigtts / ICL_uranus_*_tob
        valid = (
            raw.endswith("_streaming")
            or raw.endswith("_uranus_bigtts")
            or raw.startswith("ICL_uranus_")
        )
        assert valid, (
            f"id {vid} 不属于任何已知官方 namespace "
            f"（_streaming / _uranus_bigtts / ICL_uranus_）"
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
# T-VOICE-4：小模型 id 拼写统一是 *_streaming（不是 *_stream）
# ---------------------------------------------------------------------
def test_small_model_ids_use_streaming_suffix():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    bad = [
        v["id"] for v in voices
        if v["model"] == "seed-tts-1.0" and not v["id"].endswith("_streaming")
        and not v["id"].endswith("_uranus_bigtts")  # 防御误判
    ]
    assert not bad, f"小模型 id 拼写错误（必须 _streaming）：{bad}"


# ---------------------------------------------------------------------
# T-VOICE-5：21 款免费音色白名单全部存在（FAQ「21 款免费音色」列表）
# ---------------------------------------------------------------------
def test_free_voices_match_faq_whitelist():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    # 火山 FAQ「21 款免费音色」白名单
    expected_free = {
        # 通用 3
        "doubao:BV700_streaming",  # 灿灿
        "doubao:BV001_streaming",  # 通用女声
        "doubao:BV002_streaming",  # 通用男声
        # 有声阅读 5
        "doubao:BV701_streaming",  # 擎苍
        "doubao:BV119_streaming",  # 通用赘婿
        "doubao:BV102_streaming",  # 儒雅青年
        "doubao:BV113_streaming",  # 甜宠少御
        "doubao:BV115_streaming",  # 古风少御
        # 智能助手 / 视频配音 / 特色 / 教育 6
        "doubao:BV007_streaming",  # 亲切女声
        "doubao:BV056_streaming",  # 阳光男声
        "doubao:BV005_streaming",  # 活泼女声
        "doubao:BV051_streaming",  # 奶气萌娃
        "doubao:BV034_streaming",  # 知性姐姐-双语
        "doubao:BV033_streaming",  # 温柔小哥
        # 方言 3
        "doubao:BV021_streaming",  # 东北老铁
        "doubao:BV019_streaming",  # 重庆小伙
        "doubao:BV213_streaming",  # 广西表哥
        # 英语 2
        "doubao:BV503_streaming",  # 活力女声-Ariana
        "doubao:BV504_streaming",  # 活力男声-Jackson
        # 日语 2
        "doubao:BV522_streaming",  # 气质女声
        "doubao:BV524_streaming",  # 日语男声
    }
    # FAQ 列了 21 个，本测试覆盖 21 个全部
    assert len(expected_free) == 21, f"白名单数量不对：{len(expected_free)}"

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    ids = {v["id"] for v in voices}
    free_ids = {v["id"] for v in voices if v["free"]}

    missing = expected_free - ids
    assert not missing, f"缺少免费音色：{missing}"

    # 反向：free=True 的 id 必须出现在白名单里（防止把非免费误标 free）
    extra_free = free_ids - expected_free
    assert not extra_free, f"非白名单却被标 free：{extra_free}"


# ---------------------------------------------------------------------
# T-VOICE-6：多情感音色（supports_emotion=True）抽样存在
# ---------------------------------------------------------------------
def test_emotion_supported_voices_exist():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    emotion_voices = [v for v in voices if v["supports_emotion"]]
    # 至少要有 15 条（官方有情感列表里至少 20+ 条）
    assert len(emotion_voices) >= 15, (
        f"supports_emotion=True 的音色过少：{len(emotion_voices)}"
    )
    # 关键多情感音色必须在
    ids = {v["id"] for v in emotion_voices}
    must_have = {
        "doubao:BV700_streaming",          # 灿灿（22 种情感）
        "doubao:BV700_V2_streaming",       # 灿灿 2.0
        "doubao:BV701_streaming",          # 擎苍（10 种情感）
        "doubao:BV001_streaming",          # 通用女声（12 种情感）
        "doubao:BV123_streaming",          # 阳光青年
        "doubao:BV119_streaming",          # 通用赘婿
        "doubao:BV100_streaming",          # 质朴青年
        "doubao:BV102_streaming",          # 儒雅青年
        "doubao:zh_female_vv_uranus_bigtts",  # Vivi 2.0 大模型
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
    # 官方 supports_language=True 只有 5 条（灿灿 / 方言灿灿 / 天才少女 / Stefan / Vivi 2.0 / 小何 2.0 / 云舟 2.0 / 小天 2.0）
    assert len(lang_voices) >= 5, f"supports_language=True 的音色过少：{len(lang_voices)}"
    ids = {v["id"] for v in lang_voices}
    must_have = {
        "doubao:BV700_streaming",          # 灿灿（en/ja/ptbr/esmx/id）
        "doubao:BV704_streaming",          # 方言灿灿（多方言 + 多语种）
        "doubao:BV421_streaming",          # 天才少女（8 国）
        "doubao:BV702_streaming",          # Stefan（多国）
        "doubao:zh_female_vv_uranus_bigtts",  # Vivi 2.0（多语种 + 多方言）
    }
    missing = must_have - ids
    assert not missing, f"缺少关键多语种音色：{missing}"


# ---------------------------------------------------------------------
# T-VOICE-8：覆盖 11 大官方场景类别
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
        "智能助手",
        "视频配音",
        "特色音色",
        "广告配音",
        "新闻播报",
        "教育",
        "英文",
        "日文",
        "方言",
        "角色扮演",  # 大模型
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
# T-VOICE-10：方言字段覆盖官方 8 大方言
# ---------------------------------------------------------------------
def test_dialect_field_covers_official_dialects():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    dialects: set[str] = set()
    for v in voices:
        if v["dialect"]:
            dialects.add(v["dialect"])

    # 官方方言至少覆盖 7 种
    expected = ["dongbei", "cantonese", "sichuan", "shaanxi", "shanghai", "taipu"]
    missing = [d for d in expected if d not in dialects]
    assert not missing, f"缺少方言：{missing}（当前：{sorted(dialects)}）"


# ---------------------------------------------------------------------
# T-VOICE-11：languages 字段至少覆盖 zh / en / ja / ptbr / esmx / id
# ---------------------------------------------------------------------
def test_languages_field_covers_multiple_official_languages():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    langs: set[str] = set()
    for v in voices:
        langs.update(v["languages"])

    required = {"zh", "en", "ja", "ptbr", "esmx", "id"}
    missing = required - langs
    assert not missing, f"缺少语种：{missing}（当前：{sorted(langs)}）"
