"""批次 7 F-7（ZIP 分片）回归测试。

覆盖：
  T-ZS1  _zip_shard_ranges 分片区间（含不分片 / 空章节 / 整除与余数）
  T-ZS2  _zip_shard_filename 命名（全量覆盖沿用历史 `_all.zip`，分片带章节区间）
  T-ZS3  _build_book_zip 只打包指定区间，且章节序号保持**全书统一编号**
  T-ZS4  _parse_zip_shards 解析分片清单 + 老库单包兜底
  T-ZS5  _build_to_detail 透出 zip_shards（1-based 区间），zip_url 指向首片
  T-ZS6  路由：/media/sign 的 idx 选中对应分片，/media/stream 返回该片字节
  T-ZS7  路由：/download-all?shard=N 返回第 N 片，文件名带章节区间
"""
from __future__ import annotations

import json
import uuid
import zipfile

import pytest
import pytest_asyncio

pytest_plugins = ("pytest_asyncio",)


# =====================================================================
# T-ZS1 / T-ZS2 纯函数
# =====================================================================
def test_zs1_shard_ranges():
    from backend.app.services.build import _zip_shard_ranges

    assert _zip_shard_ranges(120, 50) == [(0, 49), (50, 99), (100, 119)]
    assert _zip_shard_ranges(100, 50) == [(0, 49), (50, 99)]
    assert _zip_shard_ranges(30, 50) == [(0, 29)]
    # <=0 表示不分片（退回单包）
    assert _zip_shard_ranges(30, 0) == [(0, 29)]
    assert _zip_shard_ranges(30, -1) == [(0, 29)]
    assert _zip_shard_ranges(0, 50) == []


def test_zs2_shard_filename():
    from backend.app.services.build import _zip_shard_filename

    # 覆盖全部章节 → 沿用历史命名（旧路径 / 旧库对得上）
    assert _zip_shard_filename("b1", 0, 29, 30) == "build_b1_all.zip"
    # 分片 → 带章节区间（1-based）
    assert _zip_shard_filename("b1", 0, 49, 120) == "build_b1_ch0001-0050.zip"
    assert _zip_shard_filename("b1", 100, 119, 120) == "build_b1_ch0101-0120.zip"


# =====================================================================
# T-ZS3 分片打包：只含指定区间 + 序号全局统一
# =====================================================================
def test_zs3_shard_zip_contains_only_its_range_with_global_numbering(tmp_path):
    from backend.app.services.build import _build_book_zip

    total = 6
    outputs: list[tuple[str | None, int | None]] = [(None, None)] * total
    titles = [f"标题{i+1}" for i in range(total)]
    lrcs = [f"[00:00.00]第{i+1}章歌词" for i in range(total)]

    zip_path = tmp_path / "shard.zip"
    _build_book_zip(
        str(zip_path),
        job_id="bid",
        job_title="我的书",
        chapter_outputs=outputs,
        chapter_titles=titles,
        chapter_lrcs=lrcs,
        start=2,
        end=4,
    )

    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())

    # 只含第 3~5 章（下标 2..4），序号仍是全书统一的 003/004/005
    assert names == [
        "我的书/第003章_标题3.lrc",
        "我的书/第003章_标题3.mp3",
        "我的书/第004章_标题4.lrc",
        "我的书/第004章_标题4.mp3",
        "我的书/第005章_标题5.lrc",
        "我的书/第005章_标题5.mp3",
    ]


def test_zs3_whole_range_default_still_packs_everything(tmp_path):
    from backend.app.services.build import _build_book_zip

    outputs: list[tuple[str | None, int | None]] = [(None, None)] * 3
    zip_path = tmp_path / "all.zip"
    _build_book_zip(
        str(zip_path),
        job_id="bid",
        job_title=None,
        chapter_outputs=outputs,
        chapter_titles=["a", "b", "c"],
    )
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
    assert len([n for n in names if n.endswith(".mp3")]) == 3
    # 无书名时目录名取 job_id（_sanitize_zip_entry 的 fallback 只在 job_id 也为空时才用）
    assert all(n.startswith("bid/") for n in names)


# =====================================================================
# T-ZS4 / T-ZS5 分片清单解析与透出
# =====================================================================
class _FakeBuild:
    def __init__(self, **kw):
        self.zip_filename = None
        self.zip_filenames_json = None
        self.total_chapters = 0
        for k, v in kw.items():
            setattr(self, k, v)


def test_zs4_parse_shards_from_json():
    from backend.app.services.build import _parse_zip_shards

    b = _FakeBuild(
        total_chapters=120,
        zip_filename="build_b_ch0001-0050.zip",
        zip_filenames_json=json.dumps([
            {"filename": "build_b_ch0001-0050.zip", "start": 0, "end": 49, "size_bytes": 10},
            {"filename": "build_b_ch0051-0100.zip", "start": 50, "end": 99, "size_bytes": 20},
            {"filename": "build_b_ch0101-0120.zip", "start": 100, "end": 119, "size_bytes": 30},
        ]),
    )
    shards = _parse_zip_shards(b)
    assert [s["filename"] for s in shards] == [
        "build_b_ch0001-0050.zip",
        "build_b_ch0051-0100.zip",
        "build_b_ch0101-0120.zip",
    ]
    assert shards[2]["start"] == 100 and shards[2]["end"] == 119


def test_zs4_legacy_single_zip_falls_back_to_one_shard():
    from backend.app.services.build import _parse_zip_shards

    # 老库：只有 zip_filename，没有 json → 合成一条覆盖全书的记录
    b = _FakeBuild(total_chapters=30, zip_filename="build_b_all.zip")
    assert _parse_zip_shards(b) == [
        {"filename": "build_b_all.zip", "start": 0, "end": 29, "size_bytes": None}
    ]
    # 完全没有产出
    assert _parse_zip_shards(_FakeBuild(total_chapters=30)) == []


def test_zs5_build_to_detail_exposes_shards_1_based():
    from backend.app.services.build import _build_to_detail

    b = _FakeBuild(
        build_id="b",
        project_id="p",
        status="success",
        progress_msg=None,
        total_chapters=60,
        completed_chapters=60,
        narrator_voice_id="v",
        speed=1.0,
        mode="classic",
        tts_provider="doubao",
        zip_filename="build_b_ch0001-0050.zip",
        zip_filenames_json=json.dumps([
            {"filename": "build_b_ch0001-0050.zip", "start": 0, "end": 49, "size_bytes": 2048},
            {"filename": "build_b_ch0051-0060.zip", "start": 50, "end": 59, "size_bytes": 1024},
        ]),
        total_size_bytes=3072,
        total_duration_ms=1000,
        started_at=None,
        completed_at=None,
        created_at=None,
        failed_chapters_json=None,
        is_retry=False,
        tts_calls=0,
        tts_chars=0,
    )
    detail = _build_to_detail(b, [])
    assert detail.zip_url == "/media/build_b_ch0001-0050.zip"
    assert [(s.idx, s.start_chapter, s.end_chapter) for s in detail.zip_shards] == [
        (0, 1, 50),
        (1, 51, 60),
    ]
    assert detail.zip_shards[0].size_kb == 2
    assert detail.zip_shards[1].url == "/media/build_b_ch0051-0060.zip"


# =====================================================================
# T-ZS6 / T-ZS7 路由：分片选择
# =====================================================================
@pytest_asyncio.fixture
async def sharded_build(_isolate_data_dir, monkeypatch):
    """一个含 2 个 ZIP 分片的 build（分片内容不同，便于断言取到的是哪一片）。"""
    from sqlalchemy import select
    from backend.app.db.models import Project, Build, User
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.core import config as cfg
    from backend.app.api import routes as _routes

    # ⚠️ patch 路由模块自己 import 的那个 settings 对象：全量跑时
    # test_path_env_override_red 会 importlib.reload(core.config)，`core.config.settings`
    # 会变成另一个对象，只 patch 那里的话路由侧读到的 AUDIO_DIR 仍是旧值 → 404（E-1 同一根因）。
    audio_dir = cfg.settings.AUDIO_DIR
    monkeypatch.setattr(_routes.settings, "AUDIO_DIR", audio_dir)

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    pid = uuid.uuid4().hex
    bid = uuid.uuid4().hex
    shard0 = f"build_{bid}_ch0001-0050.zip"
    shard1 = f"build_{bid}_ch0051-0060.zip"
    async with factory() as s:
        admin = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
        s.add(Project(project_id=pid, name="Test", status="done", owner_user_id=admin.id))
        s.add(Build(
            build_id=bid,
            project_id=pid,
            status="success",
            total_chapters=60,
            completed_chapters=60,
            narrator_voice_id="",
            speed=1.0,
            mode="classic",
            tts_provider="doubao",
            zip_filename=shard0,
            zip_filenames_json=json.dumps([
                {"filename": shard0, "start": 0, "end": 49, "size_bytes": 4},
                {"filename": shard1, "start": 50, "end": 59, "size_bytes": 4},
            ]),
        ))
        await s.commit()

    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / shard0).write_bytes(b"PK\x03\x04SHARD0")
    (audio_dir / shard1).write_bytes(b"PK\x03\x04SHARD1")

    token, _ = create_access_token("admin")
    return pid, bid, shard0, shard1, token


@pytest.mark.asyncio
async def test_zs6_sign_idx_selects_shard_and_stream_returns_it(sharded_build):
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app

    pid, bid, _shard0, shard1, token = sharded_build
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        # 取第 1 片（idx=1）
        r = await client.get(
            "/api/media/sign",
            params={"build_id": bid, "kind": "all_zip", "idx": 1},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        url = r.json()["url"]

        r2 = await client.get(url)
        assert r2.status_code == 200, r2.text
        assert r2.content == b"PK\x03\x04SHARD1"

        # 不传 idx → 第 0 片
        r3 = await client.get(
            "/api/media/sign",
            params={"build_id": bid, "kind": "all_zip"},
            headers=headers,
        )
        assert r3.status_code == 200, r3.text
        r4 = await client.get(r3.json()["url"])
        assert r4.status_code == 200
        assert r4.content == b"PK\x03\x04SHARD0"


@pytest.mark.asyncio
async def test_zs7_download_all_shard_param_and_filename(sharded_build):
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app

    pid, bid, _shard0, _shard1, token = sharded_build
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.get(
            f"/api/projects/{pid}/builds/{bid}/download-all",
            params={"shard": 1},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.content == b"PK\x03\x04SHARD1", "必须返回第 1 片而不是首片"
        # 分片下载文件名应带章节区间，避免一堆同名 zip（header 里是 percent-encoding）
        import urllib.parse

        dispo = urllib.parse.unquote(r.headers.get("content-disposition", ""))
        assert "第051-060章" in dispo, dispo
