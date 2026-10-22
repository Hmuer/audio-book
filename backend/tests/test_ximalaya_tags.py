from backend.app.services.ximalaya_tags import (
    XimalayaTagSelection,
    load_catalog,
    normalize_selection,
)


def test_catalog_has_audio_drama_taxonomy():
    catalog = load_catalog()
    assert catalog["content_category"] == "小说分类"
    assert {"男频", "女频"} <= set(catalog["audiences"])
    assert "玄幻" in catalog["audiences"]["男频"]["genres"]
    assert "东方玄幻" in catalog["audiences"]["男频"]["genres"]["玄幻"]
    assert "情节" in catalog["audiences"]["男频"]["groups"]


def test_normalize_selection_rejects_unknown_and_limits_groups():
    raw = XimalayaTagSelection(
        content_category="小说分类",
        audience="男频",
        genre="玄幻",
        sub_genre="东方玄幻",
        plot=["升级流", "废材流", "逆袭", "模型自造标签"],
        role=["龙"],
        style=["热血", "爽文"],
        background=["异界", "古代"],
        extra=["元尊", "天蚕土豆"],
    )
    cleaned = normalize_selection(raw)
    assert cleaned.content_category == "小说分类"
    assert cleaned.audience == "男频"
    assert cleaned.genre == "玄幻"
    assert cleaned.sub_genre == "东方玄幻"
    assert cleaned.plot == ["升级流", "废材流", "逆袭"]
    assert "模型自造标签" not in cleaned.plot
    assert cleaned.role == ["龙"]
    assert cleaned.extra == ["元尊", "天蚕土豆"]


def test_normalize_selection_clears_genre_when_audience_invalid():
    cleaned = normalize_selection({
        "content_category": "小说分类",
        "audience": "不存在",
        "genre": "玄幻",
        "sub_genre": "东方玄幻",
        "plot": ["升级流"],
    })
    assert cleaned.audience == ""
    assert cleaned.genre == ""
    assert cleaned.sub_genre == ""
    assert cleaned.plot == []
