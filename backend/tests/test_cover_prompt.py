from backend.app.services.cover_prompt import build_cover_prompt


def test_cover_prompt_requires_exact_title_and_genre_scene():
    prompt, negative = build_cover_prompt(
        "元尊",
        "男频",
        "男频,玄幻,热血,升级流,逆袭,长篇",
        "圣龙气运被夺的少年，凭一卷祖龙经逆天改命。",
        "大周王朝的少年周元，从边城走向诸天战场。",
    )
    assert 'Title (must be displayed exactly, in correct Chinese characters): "元尊"' in prompt
    assert "square 1:1" in prompt
    assert "giant azure dragon" in prompt
    assert "ancient Chinese fantasy" in prompt
    assert "圣龙气运" in prompt
    assert "wrong Chinese characters" in negative
    assert "watermark" in negative


def test_cover_prompt_has_default_scene_and_escapes_empty_fields():
    prompt, negative = build_cover_prompt("")
    assert '"未命名"' in prompt
    assert "epic Chinese fantasy landscape" in prompt
    assert isinstance(negative, str) and negative
