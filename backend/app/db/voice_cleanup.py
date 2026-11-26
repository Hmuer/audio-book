"""清理历史数据里对已下线「非普通话（粤语 / zh-HK）」MiniMax 音色的引用。

背景：MiniMax 中文系统音色池已收敛为普通话（Mandarin）音色，6 个粤语
（zh-HK）音色（`Cantonese_ProfessionalHost（F)`、`Cantonese_GentleLady`、
`Cantonese_ProfessionalHost（M)`、`Cantonese_PlayfulMan`、`Cantonese_CuteGirl`、
`Cantonese_KindWoman`）被移除。旧项目数据里仍可能残留这些音色 id：

- `project_characters.assigned_voice_id`
- `builds.voice_assignments_json`（以及 build 级默认音色列）
- `projects.progress_json.voice_recs_raw[*].suggested_voice_id`
  （以及项目级默认旁白音色）

本模块由启动迁移 `session._migrate_existing_sync` 在同步 `Connection` 上调用
一次，**幂等**、**逐行容错**：任何单行 / 单字段解析失败都只记日志并跳过，
绝不向上抛异常阻断启动。

修复策略（按优先级回落，保证修复后的 id 一定是普通话音色）：

- 角色已分配音色 → `NULL`（交由用户在 UI 重新选择普通话音色）；
- 构建快照里的角色分配 → 优先该 build 自己的同性别默认音色
  （`default_female_voice_id` / `default_male_voice_id`）；该默认值缺失或本身
  仍是粤语时，用静态普通话兜底（男声 `minimax:male-qn-jingying` /
  女声 `minimax:female-shaonv`）；
- 项目 prepare 进度里的推荐音色 → 按 `project_characters` 中同名角色的性别
  选静态普通话兜底（查不到角色按男声处理）；
- build / project 级默认音色列本身是粤语 → 同性别静态普通话兜底
  （项目默认旁白音色则置 `NULL`，让项目走全局默认）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)

# 静态普通话兜底音色（MiniMax 官方系统音色，长期稳定）
MALE_FALLBACK_VOICE_ID = "minimax:male-qn-jingying"
FEMALE_FALLBACK_VOICE_ID = "minimax:female-shaonv"

# 角色性别「女」的取值（其余一切值 —— 男 / male / 未知 / 空 / None —— 按男声处理）
_FEMALE_GENDER_VALUES = frozenset({"女", "female", "女声"})

# 非普通话音色 id 的标记（大小写不敏感 + 简繁中文）
_NON_MANDARIN_ASCII_MARKERS = ("cantonese", "zh-hk")
_NON_MANDARIN_CJK_MARKERS = ("粤语", "粵語")


def is_non_mandarin_voice_id(value: Any) -> bool:
    """判断一个值是否像「已下线的粤语（zh-HK）音色 id」。

    只对非空字符串做判断（`None` / 数字 / 空串 / 纯空白一律返回 False），
    避免把任意字段内容当成音色处理；调用方也只把它用在已知的「音色 id」列上。
    """
    if not isinstance(value, str):
        return False
    raw = value.strip()
    if not raw:
        return False
    lowered = raw.lower()
    if any(marker in lowered for marker in _NON_MANDARIN_ASCII_MARKERS):
        return True
    return any(marker in raw for marker in _NON_MANDARIN_CJK_MARKERS)


def is_female_gender(gender: Any) -> bool:
    """性别归一化：`女` / `female` / `女声` 视为女声，其余（含未知 / 空）视为男声。"""
    return isinstance(gender, str) and gender.strip().lower() in _FEMALE_GENDER_VALUES


def _static_fallback(is_female: bool) -> str:
    return FEMALE_FALLBACK_VOICE_ID if is_female else MALE_FALLBACK_VOICE_ID


def _prefer_mandarin(candidate: Any, is_female: bool) -> str:
    """返回 `candidate`（要求是 MiniMax 普通话 id），否则用同性别静态普通话兜底。

    只接受 `minimax:` 前缀：历史快照里若残留跨厂商或裸 id 的默认音色，
    不能把它复制进替换后的角色分配（那会换成一个当前激活 TTS 不认识的音色）。
    """
    if isinstance(candidate, str):
        cleaned = candidate.strip()
        if cleaned.startswith("minimax:") and not is_non_mandarin_voice_id(cleaned):
            return cleaned
    return _static_fallback(is_female)


def _existing_columns(conn) -> dict[str, set[str]]:
    """返回 {表名: 列名集合}；任何反射失败都按「表不存在」处理。"""
    insp = inspect(conn)
    return {
        table: {col["name"] for col in insp.get_columns(table)}
        for table in insp.get_table_names()
    }


def _load_json_object(raw: Any, label: str) -> dict | None:
    """解析 JSON 对象；失败/非对象只记 warning 返回 None（调用方跳过该字段）。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning(
            f"[voice-cleanup] {label} JSON 解析失败，跳过该字段: {type(e).__name__}: {e}"
        )
        return None
    if not isinstance(obj, dict):
        logger.warning(f"[voice-cleanup] {label} 不是 JSON 对象，跳过该字段")
        return None
    return obj


def _load_gender_maps(
    conn, columns: dict[str, set[str]]
) -> dict[str, dict[str, bool]]:
    """加载 {project_id: {角色名(含 canonical_name): 是否女声}}，失败返回空映射。"""
    pc_cols = columns.get("project_characters") or set()
    if not {"project_id", "name"} <= pc_cols:
        return {}

    fields = ["project_id", "name"]
    if "canonical_name" in pc_cols:
        fields.append("canonical_name")
    if "gender" in pc_cols:
        fields.append("gender")
    try:
        rows = conn.execute(
            text(f"SELECT {', '.join(fields)} FROM project_characters")
        ).fetchall()
    except Exception as e:  # noqa: BLE001 - 迁移必须容错
        logger.warning(
            f"[voice-cleanup] 读取 project_characters 性别失败，按男声兜底: "
            f"{type(e).__name__}: {e}"
        )
        return {}

    maps: dict[str, dict[str, bool]] = {}
    for row in rows:
        project_id = row[0]
        if project_id is None:
            continue
        idx = 1
        name = row[idx]
        canonical_name = None
        if "canonical_name" in pc_cols:
            idx += 1
            canonical_name = row[idx]
        gender = row[idx + 1] if "gender" in pc_cols else None
        female = is_female_gender(gender)
        bucket = maps.setdefault(str(project_id), {})
        for candidate_name in (name, canonical_name):
            if isinstance(candidate_name, str) and candidate_name.strip():
                bucket[candidate_name.strip()] = female
    return maps


def _update_row(conn, table: str, key_col: str, key_value: Any, updates: dict[str, Any]) -> bool:
    """按主键更新一行；成功返回 True，失败记 warning 返回 False。"""
    set_sql = ", ".join(f"{col} = :{col}" for col in updates)
    sql = text(f"UPDATE {table} SET {set_sql} WHERE {key_col} = :_key_value")
    try:
        conn.execute(sql, {**updates, "_key_value": key_value})
    except Exception as e:  # noqa: BLE001 - 迁移必须容错
        logger.warning(
            f"[voice-cleanup] 修复 {table}.{key_col}={key_value!r} 失败: "
            f"{type(e).__name__}: {e}"
        )
        return False
    return True


def _repair_project_characters(conn, columns: dict[str, set[str]]) -> int:
    """`project_characters.assigned_voice_id` 是粤语 → NULL。返回修复行数。"""
    pc_cols = columns.get("project_characters") or set()
    if not {"id", "assigned_voice_id"} <= pc_cols:
        return 0
    try:
        rows = conn.execute(
            text(
                "SELECT id, assigned_voice_id FROM project_characters "
                "WHERE assigned_voice_id IS NOT NULL"
            )
        ).fetchall()
    except Exception as e:  # noqa: BLE001 - 迁移必须容错
        logger.warning(
            f"[voice-cleanup] 读取 project_characters 失败，跳过: {type(e).__name__}: {e}"
        )
        return 0

    fixed = 0
    for row_id, voice_id in rows:
        if not is_non_mandarin_voice_id(voice_id):
            continue
        if _update_row(
            conn, "project_characters", "id", row_id, {"assigned_voice_id": None}
        ):
            fixed += 1
    return fixed


def _repair_builds(
    conn, columns: dict[str, set[str]], gender_maps: dict[str, dict[str, bool]]
) -> int:
    """修复构建快照中的粤语音色引用。返回修复的 build 行数。"""
    build_cols = columns.get("builds") or set()
    if "build_id" not in build_cols:
        return 0
    wanted = (
        "project_id",
        "narrator_voice_id",
        "default_male_voice_id",
        "default_female_voice_id",
        "voice_assignments_json",
    )
    data_cols = [col for col in wanted if col in build_cols]
    if not set(data_cols) & set(wanted[1:]):
        return 0

    try:
        rows = conn.execute(
            text(f"SELECT build_id, {', '.join(data_cols)} FROM builds")
        ).fetchall()
    except Exception as e:  # noqa: BLE001 - 迁移必须容错
        logger.warning(f"[voice-cleanup] 读取 builds 失败，跳过: {type(e).__name__}: {e}")
        return 0

    fixed_rows = 0
    for row in rows:
        build_id = row[0]
        record = dict(zip(data_cols, row[1:]))
        project_id = record.get("project_id")
        updates: dict[str, Any] = {}

        # 1) 先修 build 自己的默认同性别音色（后续角色分配要作为首选回落）。
        male_voice = record.get("default_male_voice_id")
        if is_non_mandarin_voice_id(male_voice):
            male_voice = _static_fallback(is_female=False)
            updates["default_male_voice_id"] = male_voice
        female_voice = record.get("default_female_voice_id")
        if is_non_mandarin_voice_id(female_voice):
            female_voice = _static_fallback(is_female=True)
            updates["default_female_voice_id"] = female_voice

        # 2) 旁白音色本身是粤语 → 优先 build 男声默认，否则静态男声。
        narrator_voice = record.get("narrator_voice_id")
        if is_non_mandarin_voice_id(narrator_voice):
            updates["narrator_voice_id"] = _prefer_mandarin(male_voice, is_female=False)

        # 3) 角色分配快照：逐个替换为同性别普通话兜底；空兜底则删除该条目。
        raw_assignments = record.get("voice_assignments_json")
        if isinstance(raw_assignments, str) and raw_assignments.strip():
            assignments = _load_json_object(
                raw_assignments, label=f"builds.voice_assignments_json build_id={build_id}"
            )
            if assignments is not None:
                project_genders = gender_maps.get(str(project_id), {}) if project_id else {}
                changed = False
                for character_name in list(assignments):
                    voice_id = assignments[character_name]
                    if not is_non_mandarin_voice_id(voice_id):
                        continue
                    is_female = (
                        bool(project_genders.get(character_name.strip()))
                        if isinstance(character_name, str)
                        else False
                    )
                    preferred = female_voice if is_female else male_voice
                    fallback = _prefer_mandarin(preferred, is_female)
                    if fallback:
                        assignments[character_name] = fallback
                    else:
                        del assignments[character_name]
                    changed = True
                if changed:
                    updates["voice_assignments_json"] = json.dumps(
                        assignments, ensure_ascii=False
                    )

        if not updates:
            continue
        if _update_row(conn, "builds", "build_id", build_id, updates):
            fixed_rows += 1
    return fixed_rows


def _repair_projects(
    conn, columns: dict[str, set[str]], gender_maps: dict[str, dict[str, bool]]
) -> int:
    """修复 prepare 进度推荐音色 / 项目默认旁白音色中的粤语引用。返回修复行数。"""
    project_cols = columns.get("projects") or set()
    if "project_id" not in project_cols:
        return 0
    data_cols = [
        col
        for col in ("progress_json", "default_narrator_voice_id")
        if col in project_cols
    ]
    if not data_cols:
        return 0

    try:
        rows = conn.execute(
            text(f"SELECT project_id, {', '.join(data_cols)} FROM projects")
        ).fetchall()
    except Exception as e:  # noqa: BLE001 - 迁移必须容错
        logger.warning(
            f"[voice-cleanup] 读取 projects 失败，跳过: {type(e).__name__}: {e}"
        )
        return 0

    fixed_rows = 0
    for row in rows:
        project_id = row[0]
        record = dict(zip(data_cols, row[1:]))
        updates: dict[str, Any] = {}

        # 1) 项目默认旁白音色本身是粤语 → 置 NULL（回落到全局默认）。
        if is_non_mandarin_voice_id(record.get("default_narrator_voice_id")):
            updates["default_narrator_voice_id"] = None

        # 2) prepare 进度里的音色推荐（只动 voice_recs_raw / voice_recs_narrator_id，
        #    保留其余所有字段）。voice_recs_narrator_id 残留粤语时清空，让下次
        #    prepare 的 checkpoint 失配并重新推荐普通话音色。
        raw_progress = record.get("progress_json")
        if isinstance(raw_progress, str) and raw_progress.strip():
            progress = _load_json_object(
                raw_progress, label=f"projects.progress_json project_id={project_id}"
            )
            if progress is not None:
                changed = False
                if is_non_mandarin_voice_id(progress.get("voice_recs_narrator_id")):
                    progress["voice_recs_narrator_id"] = ""
                    changed = True
                recs = progress.get("voice_recs_raw")
                if isinstance(recs, list):
                    project_genders = (
                        gender_maps.get(str(project_id), {}) if project_id else {}
                    )
                    for item in recs:
                        if not isinstance(item, dict):
                            continue
                        if not is_non_mandarin_voice_id(item.get("suggested_voice_id")):
                            continue
                        character_name = item.get("character_name")
                        is_female = (
                            bool(project_genders.get(character_name.strip()))
                            if isinstance(character_name, str)
                            else False
                        )
                        item["suggested_voice_id"] = _static_fallback(is_female)
                        changed = True
                if changed:
                    updates["progress_json"] = json.dumps(progress, ensure_ascii=False)

        if not updates:
            continue
        if _update_row(conn, "projects", "project_id", project_id, updates):
            fixed_rows += 1
    return fixed_rows


def cleanup_non_mandarin_voice_refs(conn) -> dict[str, int]:
    """修复历史数据中所有对非普通话（粤语 / zh-HK）MiniMax 音色的引用。

    参数 `conn` 为**同步** SQLAlchemy `Connection`（启动迁移里由
    `AsyncConnection.run_sync` 提供）。函数只执行 SQL、不管理事务，由调用方
    的 `engine.begin()` / 显式 commit 统一提交。

    返回 {表名: 修复行数}，键固定为 `project_characters` / `builds` / `projects`
    （未修复时为 0）。第二次调用一定返回全 0 且不产生任何写入（幂等）。
    """
    counts = {"project_characters": 0, "builds": 0, "projects": 0}
    try:
        columns = _existing_columns(conn)
    except Exception as e:  # noqa: BLE001 - 迁移必须容错
        logger.warning(
            f"[voice-cleanup] 反射数据库结构失败，跳过音色清理: {type(e).__name__}: {e}"
        )
        return counts

    gender_maps = _load_gender_maps(conn, columns)

    for table, repair in (
        ("project_characters", _repair_project_characters),
        ("builds", _repair_builds),
        ("projects", _repair_projects),
    ):
        if table not in columns:
            continue
        try:
            if table == "project_characters":
                counts[table] = repair(conn, columns)
            else:
                counts[table] = repair(conn, columns, gender_maps)
        except Exception as e:  # noqa: BLE001 - 单表失败不影响其它表 / 启动
            logger.warning(
                f"[voice-cleanup] 修复 {table} 失败，跳过该表: {type(e).__name__}: {e}"
            )

    if any(counts.values()):
        logger.warning(
            "[voice-cleanup] 已清理非普通话（粤语/zh-HK）音色引用: "
            f"project_characters={counts['project_characters']} "
            f"builds={counts['builds']} "
            f"projects={counts['projects']}"
        )
    return counts
