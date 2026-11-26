"""`backend.app.db.voice_cleanup` 的单元测试（同步 sqlite，内存库）。

覆盖：
1. 三张表的粤语音色引用都被修复（角色分配置 NULL、构建快照换同性别默认、
   项目推荐音色换静态普通话）；
2. 普通话数据完全不动；
3. build 默认音色缺失 / 本身是粤语 → 回落到静态普通话兜底；
4. 损坏 JSON 只跳过该字段、不影响其它字段和其它行，也不抛异常；
5. 幂等：第二次调用返回全 0 且不再改写数据。
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from backend.app.db.voice_cleanup import (
    FEMALE_FALLBACK_VOICE_ID,
    MALE_FALLBACK_VOICE_ID,
    cleanup_non_mandarin_voice_refs,
    is_non_mandarin_voice_id,
)

_SCHEMA = (
    """
    CREATE TABLE project_characters (
        id INTEGER PRIMARY KEY,
        project_id TEXT,
        name TEXT,
        canonical_name TEXT,
        gender TEXT,
        assigned_voice_id TEXT
    )
    """,
    """
    CREATE TABLE builds (
        build_id TEXT PRIMARY KEY,
        project_id TEXT,
        narrator_voice_id TEXT DEFAULT '',
        default_male_voice_id TEXT DEFAULT '',
        default_female_voice_id TEXT DEFAULT '',
        voice_assignments_json TEXT
    )
    """,
    """
    CREATE TABLE projects (
        project_id TEXT PRIMARY KEY,
        progress_json TEXT,
        default_narrator_voice_id TEXT
    )
    """,
)


def _make_engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        for ddl in _SCHEMA:
            conn.execute(text(ddl))
    return engine


def _insert(conn, table: str, rows: list[dict]) -> None:
    for row in rows:
        cols = ", ".join(row)
        params = ", ".join(f":{col}" for col in row)
        conn.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params})"), row)


def _seed(engine, *, characters=(), builds=(), projects=()) -> None:
    with engine.begin() as conn:
        _insert(conn, "project_characters", list(characters))
        _insert(conn, "builds", list(builds))
        _insert(conn, "projects", list(projects))


def _run_cleanup(engine) -> dict[str, int]:
    with engine.connect() as conn:
        counts = cleanup_non_mandarin_voice_refs(conn)
        conn.commit()
    return counts


def _fetch_all(engine, sql: str, params: dict | None = None) -> list:
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).fetchall()


def _fetch_one(engine, sql: str, params: dict | None = None):
    rows = _fetch_all(engine, sql, params)
    assert len(rows) == 1
    return rows[0]


def _snapshot(engine) -> dict:
    return {
        "characters": _fetch_all(
            engine,
            "SELECT id, assigned_voice_id FROM project_characters ORDER BY id",
        ),
        "builds": _fetch_all(
            engine,
            "SELECT build_id, narrator_voice_id, default_male_voice_id, "
            "default_female_voice_id, voice_assignments_json FROM builds ORDER BY build_id",
        ),
        "projects": _fetch_all(
            engine,
            "SELECT project_id, progress_json, default_narrator_voice_id "
            "FROM projects ORDER BY project_id",
        ),
    }


def test_is_non_mandarin_voice_id_detection():
    for value in (
        "minimax:Cantonese_ProfessionalHost（F)",
        "minimax:Cantonese_ProfessionalHost（M)",
        "minimax:Cantonese_GentleLady",
        "minimax:Cantonese_PlayfulMan",
        "minimax:Cantonese_CuteGirl",
        "minimax:Cantonese_KindWoman",
        "MINIMAX:CANTONESE_X",
        "minimax:zh-hk-foo",
        "minimax:粤语_男声",
        "minimax:粵語_男聲",
    ):
        assert is_non_mandarin_voice_id(value), value

    for value in (
        None,
        "",
        "   ",
        123,
        ["minimax:Cantonese_PlayfulMan"],
        "minimax:male-qn-jingying",
        "minimax:Chinese (Mandarin)_Radio_Host",
    ):
        assert not is_non_mandarin_voice_id(value), value


def test_repairs_all_three_tables():
    engine = _make_engine()
    progress = {
        "version": 1,
        "stage": "voice_recs",
        "voice_recs_narrator_id": "minimax:Cantonese_ProfessionalHost（M)",
        "voice_recs_raw": [
            {
                "character_name": "女乙",
                "suggested_voice_id": "minimax:Cantonese_GentleLady",
                "reason": "f",
            },
            {
                "character_name": "男甲",
                "suggested_voice_id": "minimax:Cantonese_PlayfulMan",
                "reason": "m",
            },
            {
                "character_name": "路人",
                "suggested_voice_id": "minimax:male-qn-badao",
                "reason": "keep",
            },
        ],
        "other": {"kept": True},
    }
    _seed(
        engine,
        characters=[
            {
                "id": 1,
                "project_id": "p1",
                "name": "女乙",
                "canonical_name": "女乙",
                "gender": "女",
                "assigned_voice_id": "minimax:Cantonese_GentleLady",
            },
            {
                "id": 2,
                "project_id": "p1",
                "name": "男甲",
                "canonical_name": "男甲",
                "gender": "男",
                "assigned_voice_id": "minimax:Cantonese_PlayfulMan",
            },
            {
                "id": 3,
                "project_id": "p1",
                "name": "路人",
                "canonical_name": "路人",
                "gender": "未知",
                "assigned_voice_id": "minimax:male-qn-badao",
            },
        ],
        builds=[
            {
                "build_id": "b1",
                "project_id": "p1",
                "narrator_voice_id": "minimax:male-qn-qingse",
                "default_male_voice_id": "minimax:male-qn-badao",
                "default_female_voice_id": "minimax:female-tianmei",
                "voice_assignments_json": json.dumps(
                    {
                        "女乙": "minimax:Cantonese_GentleLady",
                        "男甲": "minimax:Cantonese_PlayfulMan",
                        "路人": "minimax:male-qn-badao",
                    },
                    ensure_ascii=False,
                ),
            }
        ],
        projects=[
            {
                "project_id": "p1",
                "progress_json": json.dumps(progress, ensure_ascii=False),
                "default_narrator_voice_id": "minimax:Cantonese_ProfessionalHost（F)",
            }
        ],
    )

    counts = _run_cleanup(engine)

    assert counts == {"project_characters": 2, "builds": 1, "projects": 1}

    characters = dict(
        _fetch_all(engine, "SELECT id, assigned_voice_id FROM project_characters")
    )
    assert characters == {
        1: None,
        2: None,
        3: "minimax:male-qn-badao",
    }

    build = _fetch_one(
        engine,
        "SELECT narrator_voice_id, default_male_voice_id, default_female_voice_id, "
        "voice_assignments_json FROM builds WHERE build_id = 'b1'",
    )
    assert build[0] == "minimax:male-qn-qingse"
    assert build[1] == "minimax:male-qn-badao"
    assert build[2] == "minimax:female-tianmei"
    assignments = json.loads(build[3])
    assert assignments == {
        "女乙": "minimax:female-tianmei",
        "男甲": "minimax:male-qn-badao",
        "路人": "minimax:male-qn-badao",
    }

    project = _fetch_one(
        engine,
        "SELECT progress_json, default_narrator_voice_id FROM projects WHERE project_id = 'p1'",
    )
    assert project[1] is None
    repaired_progress = json.loads(project[0])
    assert repaired_progress["version"] == 1
    assert repaired_progress["stage"] == "voice_recs"
    assert repaired_progress["other"] == {"kept": True}
    # checkpoint 里记录的旁白音色也要清掉，避免残留引用并触发重新推荐
    assert repaired_progress["voice_recs_narrator_id"] == ""
    recs = {rec["character_name"]: rec for rec in repaired_progress["voice_recs_raw"]}
    assert recs["女乙"]["suggested_voice_id"] == FEMALE_FALLBACK_VOICE_ID
    assert recs["男甲"]["suggested_voice_id"] == MALE_FALLBACK_VOICE_ID
    assert recs["路人"]["suggested_voice_id"] == "minimax:male-qn-badao"
    assert all(rec["reason"] for rec in recs.values())


def test_mandarin_rows_untouched():
    engine = _make_engine()
    progress = {
        "version": 1,
        "voice_recs_raw": [
            {
                "character_name": "甲",
                "suggested_voice_id": "minimax:male-qn-jingying",
                "reason": "keep",
            }
        ],
    }
    assignments_json = json.dumps(
        {"甲": "minimax:Chinese (Mandarin)_Radio_Host"}, ensure_ascii=False
    )
    _seed(
        engine,
        characters=[
            {
                "id": 1,
                "project_id": "p1",
                "name": "甲",
                "canonical_name": "甲",
                "gender": "男",
                "assigned_voice_id": "minimax:Chinese (Mandarin)_Radio_Host",
            }
        ],
        builds=[
            {
                "build_id": "b1",
                "project_id": "p1",
                "narrator_voice_id": "minimax:male-qn-qingse",
                "default_male_voice_id": "minimax:male-qn-badao",
                "default_female_voice_id": "minimax:female-tianmei",
                "voice_assignments_json": assignments_json,
            }
        ],
        projects=[
            {
                "project_id": "p1",
                "progress_json": json.dumps(progress, ensure_ascii=False),
                "default_narrator_voice_id": "minimax:Chinese (Mandarin)_Lyrical_Voice",
            }
        ],
    )

    before = _snapshot(engine)
    counts = _run_cleanup(engine)

    assert counts == {"project_characters": 0, "builds": 0, "projects": 0}
    assert _snapshot(engine) == before


def test_build_defaults_missing_or_cantonese_use_static_fallback():
    engine = _make_engine()
    _seed(
        engine,
        characters=[
            {
                "id": 1,
                "project_id": "p1",
                "name": "男角色",
                "canonical_name": "男角色",
                "gender": "男",
                "assigned_voice_id": None,
            },
            {
                "id": 2,
                "project_id": "p1",
                "name": "女角色",
                "canonical_name": "女角色",
                "gender": "女",
                "assigned_voice_id": None,
            },
        ],
        builds=[
            {
                "build_id": "b1",
                "project_id": "p1",
                "narrator_voice_id": "minimax:Cantonese_ProfessionalHost（M)",
                "default_male_voice_id": "",
                "default_female_voice_id": "minimax:Cantonese_CuteGirl",
                "voice_assignments_json": json.dumps(
                    {
                        "男角色": "minimax:Cantonese_PlayfulMan",
                        "女角色": "minimax:Cantonese_KindWoman",
                    },
                    ensure_ascii=False,
                ),
            }
        ],
    )

    counts = _run_cleanup(engine)

    assert counts == {"project_characters": 0, "builds": 1, "projects": 0}
    build = _fetch_one(
        engine,
        "SELECT narrator_voice_id, default_male_voice_id, default_female_voice_id, "
        "voice_assignments_json FROM builds WHERE build_id = 'b1'",
    )
    # 空默认音色不主动填，但缺省回落时用静态普通话
    assert build[0] == MALE_FALLBACK_VOICE_ID
    assert build[1] == ""
    assert build[2] == FEMALE_FALLBACK_VOICE_ID
    assert json.loads(build[3]) == {
        "男角色": MALE_FALLBACK_VOICE_ID,
        "女角色": FEMALE_FALLBACK_VOICE_ID,
    }


def test_build_default_outside_minimax_namespace_is_not_copied():
    """build 默认音色若不是 MiniMax 普通话 id，不能复制进替换后的分配。"""
    engine = _make_engine()
    _seed(
        engine,
        characters=[
            {
                "id": 1,
                "project_id": "p1",
                "name": "男角色",
                "canonical_name": "男角色",
                "gender": "男",
                "assigned_voice_id": None,
            }
        ],
        builds=[
            {
                "build_id": "b1",
                "project_id": "p1",
                "narrator_voice_id": "minimax:Cantonese_ProfessionalHost（M)",
                "default_male_voice_id": "doubao:zh_male_default",
                "default_female_voice_id": "minimax:female-tianmei",
                "voice_assignments_json": json.dumps(
                    {"男角色": "minimax:Cantonese_PlayfulMan"}, ensure_ascii=False
                ),
            }
        ],
    )

    counts = _run_cleanup(engine)

    assert counts == {"project_characters": 0, "builds": 1, "projects": 0}
    build = _fetch_one(
        engine,
        "SELECT narrator_voice_id, voice_assignments_json FROM builds WHERE build_id = 'b1'",
    )
    # 非 MiniMax 前缀的默认音色不参与修复，回落到静态普通话音色
    assert build[0] == MALE_FALLBACK_VOICE_ID
    assert json.loads(build[1]) == {"男角色": MALE_FALLBACK_VOICE_ID}


def test_corrupt_json_is_skipped_without_aborting():
    engine = _make_engine()
    _seed(
        engine,
        builds=[
            {
                "build_id": "b-bad",
                "project_id": "p-bad",
                "narrator_voice_id": "minimax:Cantonese_PlayfulMan",
                "default_male_voice_id": "minimax:Cantonese_ProfessionalHost（M)",
                "default_female_voice_id": "",
                "voice_assignments_json": "{broken",
            },
            {
                "build_id": "b-good",
                "project_id": "p-good",
                "narrator_voice_id": "minimax:male-qn-qingse",
                "default_male_voice_id": "minimax:male-qn-jingying",
                "default_female_voice_id": "minimax:female-shaonv",
                "voice_assignments_json": json.dumps(
                    {"甲": "minimax:Cantonese_PlayfulMan"}, ensure_ascii=False
                ),
            },
        ],
        projects=[
            {
                "project_id": "p-bad",
                "progress_json": "{not-json",
                "default_narrator_voice_id": "minimax:Cantonese_KindWoman",
            },
            {
                "project_id": "p-good",
                "progress_json": json.dumps(
                    {
                        "voice_recs_raw": [
                            {
                                "character_name": "甲",
                                "suggested_voice_id": "minimax:Cantonese_PlayfulMan",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                "default_narrator_voice_id": None,
            },
        ],
    )

    counts = _run_cleanup(engine)

    # 损坏的 JSON 字段本身跳过，但同行的其它粤语字段 / 其它行仍被修复
    assert counts == {"project_characters": 0, "builds": 2, "projects": 2}

    bad_build = _fetch_one(
        engine,
        "SELECT narrator_voice_id, default_male_voice_id, voice_assignments_json "
        "FROM builds WHERE build_id = 'b-bad'",
    )
    assert bad_build[0] == MALE_FALLBACK_VOICE_ID
    assert bad_build[1] == MALE_FALLBACK_VOICE_ID
    assert bad_build[2] == "{broken"  # 无法解析 → 原样保留

    good_build = _fetch_one(
        engine,
        "SELECT voice_assignments_json FROM builds WHERE build_id = 'b-good'",
    )
    assert json.loads(good_build[0]) == {"甲": MALE_FALLBACK_VOICE_ID}

    bad_project = _fetch_one(
        engine,
        "SELECT progress_json, default_narrator_voice_id FROM projects WHERE project_id = 'p-bad'",
    )
    assert bad_project[0] == "{not-json"  # 无法解析 → 原样保留
    assert bad_project[1] is None

    good_project = _fetch_one(
        engine,
        "SELECT progress_json FROM projects WHERE project_id = 'p-good'",
    )
    recs = json.loads(good_project[0])["voice_recs_raw"]
    assert recs[0]["suggested_voice_id"] == MALE_FALLBACK_VOICE_ID


def test_second_run_is_idempotent():
    engine = _make_engine()
    _seed(
        engine,
        characters=[
            {
                "id": 1,
                "project_id": "p1",
                "name": "甲",
                "canonical_name": "甲",
                "gender": "unknown",
                "assigned_voice_id": "minimax:Cantonese_PlayfulMan",
            }
        ],
        builds=[
            {
                "build_id": "b1",
                "project_id": "p1",
                "narrator_voice_id": "minimax:Cantonese_ProfessionalHost（M)",
                "default_male_voice_id": "minimax:Cantonese_ProfessionalHost（M)",
                "default_female_voice_id": "minimax:female-shaonv",
                "voice_assignments_json": json.dumps(
                    {"甲": "minimax:Cantonese_PlayfulMan"}, ensure_ascii=False
                ),
            }
        ],
        projects=[
            {
                "project_id": "p1",
                "progress_json": json.dumps(
                    {
                        "voice_recs_raw": [
                            {
                                "character_name": "甲",
                                "suggested_voice_id": "minimax:Cantonese_PlayfulMan",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                "default_narrator_voice_id": "minimax:Cantonese_KindWoman",
            }
        ],
    )

    first = _run_cleanup(engine)
    assert first == {"project_characters": 1, "builds": 1, "projects": 1}

    after_first = _snapshot(engine)
    second = _run_cleanup(engine)

    assert second == {"project_characters": 0, "builds": 0, "projects": 0}
    assert _snapshot(engine) == after_first
