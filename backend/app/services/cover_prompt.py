"""AI 封面提示词：参考 /opt/novels 的“书名精确渲染 + 类型场景”写法。

与旧实现最大的不同：
- 不再笼统地说“避免文字”，而是明确要求把书名按正确汉字画在封面上；
- 根据分类/标签选择场景，避免所有书都生成同一种通用图；
- 从书名提取意象（龙、尊、天、战等），补充画面重点；
- 单独返回 negative_prompt，供支持该字段的图片接口使用。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _CoverStyle:
    scene: str
    mood: str
    lighting: str


_STYLES: tuple[tuple[tuple[str, ...], _CoverStyle], ...] = (
    (
        ("玄幻", "仙侠", "修真", "奇幻", "男频", "热血", "升级流", "逆袭"),
        _CoverStyle(
            scene=(
                "ancient Chinese fantasy world, a young cultivator in dark blue robes "
                "standing on a sacred mountain peak, a giant azure dragon coiling behind him, "
                "floating immortal palaces and misty valleys, golden qi energy swirling"
            ),
            mood="epic, heroic, aspirational",
            lighting="celestial golden light, dramatic rim light, misty atmosphere",
        ),
    ),
    (
        ("女频", "言情", "古言", "宫斗", "宅斗", "甜宠"),
        _CoverStyle(
            scene=(
                "elegant ancient Chinese palace garden, a graceful couple in elaborate Hanfu, "
                "pavilions, lotus pond, cherry blossoms, refined traditional details"
            ),
            mood="romantic, elegant, dreamy",
            lighting="soft warm daylight, gentle bokeh, atmospheric depth",
        ),
    ),
    (
        ("悬疑", "推理", "惊悚", "灵异"),
        _CoverStyle(
            scene=(
                "dark ancient Chinese mansion at night, rain and fog, a solitary detective "
                "silhouette, scattered clues, deep shadows and a single cold light source"
            ),
            mood="suspenseful, mysterious, tense",
            lighting="chiaroscuro, cold moonlight, rain reflections",
        ),
    ),
    (
        ("科幻", "星际", "末世", "未来"),
        _CoverStyle(
            scene=(
                "futuristic megacity with holographic advertisements, a starship above the skyline, "
                "neon rain, distant alien planet and nebula"
            ),
            mood="futuristic, epic, awe-inspiring",
            lighting="neon and holographic lights, cosmic color palette",
        ),
    ),
    (
        ("历史", "古代", "王朝", "宫廷"),
        _CoverStyle(
            scene=(
                "grand ancient Chinese palace courtyard, period architecture and gardens, "
                "elegant figures in historical costumes, autumn leaves and dramatic clouds"
            ),
            mood="elegant, historical, contemplative",
            lighting="soft natural daylight, warm and cool contrast",
        ),
    ),
)

_DEFAULT_STYLE = _CoverStyle(
    scene=(
        "epic Chinese fantasy landscape, ancient architecture, dramatic clouds, "
        "a lone protagonist with a strong silhouette, rich world-building details"
    ),
    mood="epic, cinematic, immersive",
    lighting="dramatic golden hour, volumetric light, deep atmospheric perspective",
)

_TITLE_HINTS: tuple[tuple[str, str], ...] = (
    ("龙", "a giant Chinese dragon, scales and whiskers, coiled around the composition"),
    ("尊", "supreme sovereign aura, majestic throne, golden imperial light"),
    ("帝", "imperial majesty, dragon throne, golden crown and ceremonial robes"),
    ("皇", "imperial majesty, dragon throne, golden crown and ceremonial robes"),
    ("王", "royal authority, ancient banner, commanding presence"),
    ("神", "divine aura, sacred light, ancient temple"),
    ("仙", "immortal aura, floating palaces, celestial mist"),
    ("魔", "demonic energy, dark red and black aura, ominous sky"),
    ("战", "battle-ready stance, weapon in hand, sparks and wind"),
    ("逆", "defiant stance, storm clouds, rising against fate"),
    ("天", "vast heavens, celestial gate, starry sky"),
    ("元", "primordial origin energy, ancient chaos, glowing core"),
    ("圣", "sacred radiance, ancient runes, holy light"),
)


def _pick_style(category: str, tags: str) -> _CoverStyle:
    haystack = f"{category},{tags}"
    for keywords, style in _STYLES:
        if any(word in haystack for word in keywords):
            return style
    return _DEFAULT_STYLE


def _title_hints(title: str) -> str:
    hints: list[str] = []
    for word, hint in _TITLE_HINTS:
        if word in title and hint not in hints:
            hints.append(hint)
    return ", ".join(hints)


def _story_cues(selling_points: str, description: str) -> str:
    text = " ".join((selling_points or "").split())
    extra = " ".join((description or "").split())
    if extra:
        text = f"{text} {extra}" if text else extra
    return text[:700]


def build_cover_prompt(
    title: str,
    category: str = "",
    tags: str = "",
    selling_points: str = "",
    description: str = "",
) -> tuple[str, str]:
    """返回 (正向 prompt, 负向 prompt)。

    正向 prompt 明确要求把书名按正确汉字画在封面上；负向 prompt 只禁止
    “多余文字/错字/水印”，不禁止书名本身。
    """
    safe_title = (title or "未命名").strip()[:120]
    style = _pick_style(category, tags)
    hints = _title_hints(safe_title)
    cues = _story_cues(selling_points, description)

    scene = style.scene + (f", {hints}" if hints else "")
    story_block = f"\nStory cues: {cues}" if cues else ""

    prompt = f"""Anime-style Chinese audiobook album cover illustration, square 1:1 format.

Title (must be displayed exactly, in correct Chinese characters): "{safe_title}"

Scene composition: {scene}

Genre style: {style.mood}
Lighting: {style.lighting}{story_block}

Composition requirements:
- Square 1:1 album cover composition, designed for Ximalaya-style podcast/album artwork
- Professional Chinese anime illustration, highly detailed characters and background
- The exact title "{safe_title}" prominently displayed at the top center in elegant Chinese calligraphy
- Every Chinese character of the title must be correct; no misspelling and no extra words
- Centered composition with balanced negative space, suitable for a square crop
- Cinematic composition, dramatic depth of field, vibrant harmonious colors
- Full-body or half-body protagonist, epic scale background
- 4K quality, masterpiece, best quality, official art

Text rules:
- Only the exact title text "{safe_title}" may appear
- No author name, no watermark, no logo, no QR code, no extra captions, no gibberish characters
"""
    negative = (
        "extra text, gibberish Chinese characters, wrong Chinese characters, "
        "misspelled title, duplicate title, author name, watermark, logo, QR code, "
        "UI elements, border, low quality, blurry, distorted hands, extra fingers"
    )
    return prompt.strip(), negative
