"""喜马拉雅有声书标签体系。

数据来自 `/tmp/getTagsByCategoryId`（已保存为
`backend/app/resources/ximalaya_tags.json`）。这里只关注“可选项”和层级关系，
上传接口需要的 metadata/value id 不参与识别；原始 JSON 仍保留，后续接上传
接口时可直接复用。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_DATA_PATH = Path(__file__).resolve().parent.parent / "resources" / "ximalaya_tags.json"
_GROUP_NAMES = ("情节", "恋爱", "角色", "风格", "背景")
_GROUP_FIELDS = {
    "情节": "plot",
    "恋爱": "love",
    "角色": "role",
    "风格": "style",
    "背景": "background",
}


class XimalayaTagSelection(BaseModel):
    """LLM 从喜马拉雅标签体系中选出的结果。"""

    content_category: str = ""
    audience: str = ""
    genre: str = ""
    sub_genre: str = ""
    plot: list[str] = Field(default_factory=list)
    love: list[str] = Field(default_factory=list)
    role: list[str] = Field(default_factory=list)
    style: list[str] = Field(default_factory=list)
    background: list[str] = Field(default_factory=list)
    extra: list[str] = Field(default_factory=list)


def _find_entry(
    entries: list[dict[str, Any]], name: str, parent_id: int, parent_value: int,
) -> dict[str, Any] | None:
    for entry in entries:
        if (
            entry.get("metadataName") == name
            and entry.get("parentMetaDataId") == parent_id
            and entry.get("parentMetaDataValueId") == parent_value
        ):
            return entry
    return None


def _values(entry: dict[str, Any] | None) -> list[tuple[str, int]]:
    if not entry:
        return []
    result: list[tuple[str, int]] = []
    for item in entry.get("displayMetadataValues") or []:
        value = str(item.get("displayValue") or "").strip()
        value_id = item.get("metadataValueId")
        if value and isinstance(value_id, int):
            result.append((value, value_id))
    return result


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    """返回紧凑分类树；进程内只解析一次。"""
    raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    data = raw.get("data") or {}
    entries = data.get("metadataSummaryList") or []
    tag_config = data.get("tagConfigVO") or {}

    content_entry = next(
        (e for e in entries if e.get("metadataName") == "内容分类"), None,
    )
    if not content_entry:
        return {"content_category": "", "audiences": {}, "limits": {}}
    content_value = next(
        (
            item for item in (content_entry.get("displayMetadataValues") or [])
            if item.get("displayValue") == "小说分类"
        ),
        None,
    )
    if not content_value:
        return {"content_category": "", "audiences": {}, "limits": {}}

    novel_entry = _find_entry(
        entries,
        "小说分类",
        int(content_entry.get("metadataId") or 0),
        int(content_value.get("metadataValueId") or 0),
    )
    if not novel_entry:
        return {"content_category": "小说分类", "audiences": {}, "limits": {}}

    audiences: dict[str, Any] = {}
    for audience_name, audience_value in _values(novel_entry):
        audience_entry = _find_entry(
            entries,
            audience_name,
            int(novel_entry.get("metadataId") or 0),
            audience_value,
        )
        if not audience_entry:
            continue

        genres: dict[str, list[str]] = {}
        for genre_name, genre_value in _values(audience_entry):
            genre_entry = _find_entry(
                entries,
                genre_name,
                int(audience_entry.get("metadataId") or 0),
                genre_value,
            )
            genres[genre_name] = [name for name, _ in _values(genre_entry)]

        groups: dict[str, list[str]] = {}
        group_max = 3
        for entry in entries:
            name = str(entry.get("metadataName") or "")
            if (
                name not in _GROUP_NAMES
                or entry.get("parentMetaDataId") != novel_entry.get("metadataId")
                or entry.get("parentMetaDataValueId") != audience_value
            ):
                continue
            values = [value for value, _ in _values(entry)]
            if values:
                groups[name] = values
                group_max = max(group_max, int(entry.get("inputValueCount") or 0))

        audiences[audience_name] = {
            "genres": genres,
            "groups": groups,
            "group_max": group_max,
        }

    try:
        extra_max = max(1, int(tag_config.get("allSelect") or 10))
        extra_length = max(1, int(tag_config.get("maxLength") or 15))
    except (TypeError, ValueError):
        extra_max, extra_length = 10, 15

    return {
        "content_category": "小说分类",
        "audiences": audiences,
        "limits": {
            "extra_max": extra_max,
            "extra_tag_max_length": extra_length,
        },
    }


def catalog_for_prompt() -> dict[str, Any]:
    """给 LLM 的候选集：只要名称和层级，不带内部 id。"""
    catalog = load_catalog()
    audiences: dict[str, Any] = {}
    for name, audience in (catalog.get("audiences") or {}).items():
        audiences[name] = {
            "genres": audience.get("genres") or {},
            "groups": audience.get("groups") or {},
        }
    return {
        "content_category": catalog.get("content_category") or "",
        "audiences": audiences,
        "limits": catalog.get("limits") or {},
    }


def _clean_list(
    raw: Any, allowed: list[str], *, limit: int,
) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    allowed_set = set(allowed)
    result: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if value and value in allowed_set and value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def normalize_selection(raw: Any) -> XimalayaTagSelection:
    """只保留分类树里存在的值，并限制每组数量；防止模型自造标签。"""
    if isinstance(raw, XimalayaTagSelection):
        data = raw.model_dump()
    elif isinstance(raw, dict):
        data = raw
    else:
        return XimalayaTagSelection()

    catalog = load_catalog()
    audiences = catalog.get("audiences") or {}
    content_category = (
        str(data.get("content_category") or "").strip()
        if str(data.get("content_category") or "").strip() == catalog.get("content_category")
        else ""
    )
    audience = str(data.get("audience") or "").strip()
    audience_data = audiences.get(audience) or {}
    genres = audience_data.get("genres") or {}
    groups = audience_data.get("groups") or {}

    genre = str(data.get("genre") or "").strip()
    if genre not in genres:
        genre = ""
    sub_genre = str(data.get("sub_genre") or "").strip()
    if not genre or sub_genre not in (genres.get(genre) or []):
        sub_genre = ""

    group_max = max(1, int(audience_data.get("group_max") or 3))
    limits = catalog.get("limits") or {}
    extra_limit = max(1, int(limits.get("extra_max") or 10))
    extra_len = max(1, int(limits.get("extra_tag_max_length") or 15))

    extra: list[str] = []
    raw_extra = data.get("extra") or []
    if isinstance(raw_extra, str):
        raw_extra = [raw_extra]
    if isinstance(raw_extra, list):
        for item in raw_extra:
            value = str(item or "").strip()[:extra_len]
            if value and value not in extra:
                extra.append(value)
            if len(extra) >= extra_limit:
                break

    return XimalayaTagSelection(
        content_category=content_category,
        audience=audience if audience_data else "",
        genre=genre,
        sub_genre=sub_genre,
        plot=_clean_list(data.get("plot"), groups.get("情节") or [], limit=group_max),
        love=_clean_list(data.get("love"), groups.get("恋爱") or [], limit=group_max),
        role=_clean_list(data.get("role"), groups.get("角色") or [], limit=group_max),
        style=_clean_list(data.get("style"), groups.get("风格") or [], limit=group_max),
        background=_clean_list(
            data.get("background"), groups.get("背景") or [], limit=group_max,
        ),
        extra=extra,
    )
