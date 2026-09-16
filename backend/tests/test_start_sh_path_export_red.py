"""回归测试：start.sh 在 source 旧 .env（可能含 DATA_DIR=./data）后，
仍然能把 $NOVEL_TTS_HOME 派生路径 export 出来给后端用。

背景（真实用户场景）:
  - 旧部署的 .env 里残留 DATA_DIR=./data / DATABASE_URL=sqlite+aiosqlite:///./data/app.db
  - start.sh 把这份 .env 复制到 ~/.novel-tts/.env 然后 source
  - bug: source 把脚本里派生的 $DATA_DIR 覆盖成 ./data，后面 export DATA_DIR="$DATA_DIR"
        反而把 ./data 导出给后端 —— 数据全部还在 ./data，没迁出去
  - fix: 用 DATA_DIR_NTH 等临时变量"冻结" NOVEL_TTS_HOME 派生值，export 时不再读 $DATA_DIR

测试策略:
  不重写 export 逻辑，而是直接 source .env 然后调用 bash 解析 start.sh 里的 export 行。
  这样如果 start.sh 又退化成 "export DATA_DIR=\"$DATA_DIR\"" 这种写法，测试会立刻红。

注意：start.sh 的"启动"段会真去 pip install / npm build，我们不跑整段，只跑
"派生路径 + source .env + export"那一段（start.sh 第 26~52 行 + 159~196 行）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
START_SH = REPO_ROOT / "start.sh"


def _extract_block(text: str, start_marker: str, end_marker: str) -> str:
    """从 start.sh 文本里取两个 marker 之间的所有行（含 marker 行）。"""
    lines = text.split("\n")
    start_i = end_i = None
    for i, line in enumerate(lines):
        if start_marker in line and start_i is None:
            start_i = i
        elif end_marker in line and start_i is not None and i > start_i:
            end_i = i
            break
    if start_i is None or end_i is None:
        raise RuntimeError(f"没找到 {start_marker!r} ... {end_marker!r} 段")
    return "\n".join(lines[start_i : end_i + 1])


def _build_subshell_script(env_file: Path, novel_home: Path) -> str:
    """从 start.sh 里取"派生路径"段 + "export"段，拼成一段可独立执行的 bash。

    把 .env 路径和 NOVEL_TTS_HOME 都通过 env var 注入，避免 hardcoded 路径。
    """
    text = START_SH.read_text()
    derive_block = _extract_block(
        text,
        start_marker="NOVEL_TTS_HOME 解析",
        end_marker="ACTION=\"daemon\"",
    )
    export_block = _extract_block(
        text,
        start_marker="环境变量配置：把运行时产物路径 export",
        end_marker="BIND_HOST=",
    )
    # 替换 NOVEL_TTS_HOME 的默认值为外部传入的（避免脚本再去 mkdir 用户家目录）
    # 真实 start.sh 里 L29-30 是 `${NOVEL_TTS_HOME:-$DEFAULT_HOME}`，
    # 这里我们直接 export 强制覆盖。
    return f"""
export NOVEL_TTS_HOME="{novel_home}"

{derive_block}

# ---------- .env 加载 ----------
{derive_block if False else ''}
set -a; source "{env_file}"; set +a

{export_block}

# 输出最终 export 出去的 4 个变量
env | grep -E '^(DATA_DIR|AUDIO_DIR|DATABASE_URL|LOG_FILE)='
"""


@pytest.fixture(scope="module")
def bash_available() -> bool:
    return shutil.which("bash") is not None


def _run(env_file: Path, novel_home: Path) -> dict:
    script = _build_subshell_script(env_file, novel_home)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.fail(
            f"bash -c 失败 (rc={result.returncode})\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n"
            f"--- script ---\n{script}"
        )
    env: dict = {}
    for line in result.stdout.strip().split("\n"):
        if "=" in line:
            k, _, v = line.partition("=")
            env[k] = v
    return env


def test_old_env_with_legacy_data_dir_does_not_override_nth(tmp_path):
    """T-PATH-3 (RED→GREEN): 项目根 .env 含 DATA_DIR=./data 时，导出值仍是 NOVEL_TTS_HOME/data。

    这是真实用户场景：升级前 .env 里有 DATA_DIR=./data + DATABASE_URL=sqlite+..././data/app.db，
    升级后 start.sh 应当仍然让 uvicorn 看到 $NOVEL_TTS_HOME 派生路径，而不是被旧 .env 拉回 ./data。
    """
    novel_home = tmp_path / "novel-tts"
    novel_home.mkdir()
    env_file = tmp_path / "env"
    env_file.write_text(
        "TTS_API_KEY=test\n"
        "DATA_DIR=./data\n"                                            # ← 旧值
        "AUDIO_DIR=./data/audio\n"
        "DATABASE_URL=sqlite+aiosqlite:///./data/app.db\n"             # ← 旧值
        "LOG_FILE=./data/logs/app.log\n"
    )

    env = _run(env_file, novel_home)

    assert env["DATA_DIR"] == str(novel_home / "data"), (
        f"DATA_DIR 应等于 NOVEL_TTS_HOME 派生路径，但被旧 .env 拉回 ./data 了: "
        f"got {env['DATA_DIR']!r}"
    )
    assert env["AUDIO_DIR"] == str(novel_home / "data" / "audio")
    assert env["DATABASE_URL"] == f"sqlite+aiosqlite:///{novel_home}/data/app.db"
    assert env["LOG_FILE"] == str(novel_home / "data" / "logs" / "app.log")


def test_no_legacy_path_fields_still_uses_nth(tmp_path):
    """T-PATH-4: .env 里没有路径字段，导出值仍是 NOVEL_TTS_HOME 派生路径。"""
    novel_home = tmp_path / "novel-tts"
    novel_home.mkdir()
    env_file = tmp_path / "env"
    env_file.write_text("TTS_API_KEY=test\n")  # 只有 API key

    env = _run(env_file, novel_home)

    assert env["DATA_DIR"] == str(novel_home / "data")
    assert env["DATABASE_URL"] == f"sqlite+aiosqlite:///{novel_home}/data/app.db"


def test_only_database_url_overridden_by_env(tmp_path):
    """T-PATH-5: .env 只覆盖 DATABASE_URL（用户最常见的微调方式）。

    即便 .env 里有 DATABASE_URL=sqlite+aiosqlite:///./data/app.db，
    我们仍然要 export NOVEL_TTS_HOME 派生版本的 DATABASE_URL 给 uvicorn。
    否则 SQLite 会指错文件、新的写入又回到 ./data。
    """
    novel_home = tmp_path / "novel-tts"
    novel_home.mkdir()
    env_file = tmp_path / "env"
    env_file.write_text(
        "TTS_API_KEY=test\n"
        "DATABASE_URL=sqlite+aiosqlite:///./data/app.db\n"
    )

    env = _run(env_file, novel_home)

    assert env["DATABASE_URL"] == f"sqlite+aiosqlite:///{novel_home}/data/app.db", (
        f"DATABASE_URL 应为 NOVEL_TTS_HOME 派生值，但被 .env 拉回 ./data 了: "
        f"got {env['DATABASE_URL']!r}"
    )
