"""豆包仅展示官方系统 TTS 2.0 模型。"""
from backend.app.ai.providers.doubao.models import get_all_options, is_valid_resource_id


def test_only_official_system_model_is_available():
    options = get_all_options()
    assert list(options) == ["tts_resource_ids"]
    assert [item["id"] for item in options["tts_resource_ids"]] == ["seed-tts-2.0"]
    assert is_valid_resource_id("seed-tts-2.0")
    assert not is_valid_resource_id("seed-icl-2.0")
