"""旁白音色互斥：智能推荐音色时，旁白音色必须独立、不得被任何角色复用。

覆盖：
  N-1 项目已设 default_narrator_voice_id → 该音色从角色候选池移除，prompt 显式声明旁白专用
  N-2 项目未设旁白 → 兜底擎苍 2.0（若在池中）同样被移除
  N-3 LLM 不守约束仍返回旁白音色 → 改选同性别、未被占用的其他音色
  N-4 显式 narrator_voice_id 入参优先于项目设置
  N-5 池中没有兜底旁白音色且项目未设旁白 → 不做任何剔除（不误伤）

历史：MiniMax TTS 已弃用，音色池与兜底旁白均改为豆包 2.0 口径。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DOUBAO_VOICES = [
    {"id": "doubao:zh_male_qingcang_uranus_bigtts", "name": "擎苍 2.0", "gender": "male",
     "description": "中年·沉稳·宽厚", "provider": "doubao", "model": "seed-tts-2.0"},
    {"id": "doubao:zh_female_vv_uranus_bigtts", "name": "Vivi 2.0", "gender": "female",
     "description": "少女·甜蜜", "provider": "doubao", "model": "seed-tts-2.0"},
    {"id": "doubao:zh_female_xiaohe_uranus_bigtts", "name": "小何 2.0", "gender": "female",
     "description": "少女·清脆", "provider": "doubao", "model": "seed-tts-2.0"},
    {"id": "doubao:zh_male_ruyaqingnian_uranus_bigtts", "name": "儒雅青年 2.0", "gender": "male",
     "description": "青年·儒雅", "provider": "doubao", "model": "seed-tts-2.0"},
]

# 旁白提示块的起始标志（PROMPT_BASE 的规则 4 里也会提到「【旁白音色】」四个字，
# 所以断言提示块是否存在时必须带上下文，不能只匹配这四个字）
NARRATOR_HINT_MARK = "【旁白音色】本项目旁白使用"

# 与 voice_recommender._DEFAULT_NARRATOR_IDS 保持同源
DEFAULT_NARRATOR = "doubao:zh_male_qingcang_uranus_bigtts"


class _FakeLLM:
    """记录 prompt，并按预设返回推荐结果。"""

    def __init__(self, recs: list[dict] | None = None):
        self.recs = recs or []
        self.prompts: list[str] = []

    async def chat_structured(self, *, prompt, output_schema, **_kw):
        self.prompts.append(prompt)
        return output_schema.model_validate({"data": self.recs})

    @property
    def last_prompt(self) -> str:
        return self.prompts[-1]


@pytest.fixture
def env(monkeypatch):
    from backend.app.services import voice_recommender as vr

    state: dict = {"doubao": list(DOUBAO_VOICES)}

    async def _fake_provider(p: str) -> list[dict]:
        return list(state.get(p, []))

    async def _fake_icl(_user_id):
        return []

    monkeypatch.setattr(vr, "_fetch_provider_voices", _fake_provider)
    monkeypatch.setattr(vr, "_fetch_icl_voices", _fake_icl)

    llm = _FakeLLM()
    import backend.app.ai.factory as _factory
    monkeypatch.setattr(_factory, "get_llm", lambda *a, **k: llm)
    state["llm"] = llm
    return state


async def _make_project(narrator: str | None = None) -> str:
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, update_project

    await init_db()
    pid = (await create_project("narrator-exclusive", owner_user_id=0)).project_id
    if narrator:
        await update_project(pid, default_narrator_voice_id=narrator)
    return pid


def _prompt_voice_ids(prompt: str) -> set[str]:
    """解析 prompt 末尾【音色列表】块里的全部 id。"""
    m = re.search(r"【音色列表】\s*(\[.*?\])\s*$", prompt, re.DOTALL)
    assert m, "prompt 末尾应有【音色列表】JSON 块"
    return {v["id"] for v in json.loads(m.group(1))}


# ---------------------------------------------------------------------
# N-1：项目显式设置的旁白音色不进角色候选池
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_n1_project_narrator_removed_from_character_pool(env):
    from backend.app.services.character import Character
    from backend.app.services.voice_recommender import recommend_voices_with_llm

    pid = await _make_project("doubao:zh_female_vv_uranus_bigtts")
    chars = [Character(name="林若雪", gender="女", age="少女", personality="内向")]
    await recommend_voices_with_llm(chars, project_id=pid)

    prompt = env["llm"].last_prompt
    ids = _prompt_voice_ids(prompt)
    assert "doubao:zh_female_vv_uranus_bigtts" not in ids, "旁白音色不应出现在角色候选池"
    # 项目已显式指定旁白时，不应再额外剔除兜底音色
    assert DEFAULT_NARRATOR in ids
    assert NARRATOR_HINT_MARK in prompt and "doubao:zh_female_vv_uranus_bigtts" in prompt


# ---------------------------------------------------------------------
# N-2：项目未设旁白时，按 build 的兜底口径剔除擎苍 2.0
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_n2_default_narrator_removed_when_project_has_none(env):
    from backend.app.services.character import Character
    from backend.app.services.voice_recommender import recommend_voices_with_llm

    pid = await _make_project(None)
    chars = [Character(name="李明", gender="男", age="青年", personality="开朗")]
    await recommend_voices_with_llm(chars, project_id=pid)

    prompt = env["llm"].last_prompt
    assert DEFAULT_NARRATOR not in _prompt_voice_ids(prompt)
    assert NARRATOR_HINT_MARK in prompt and DEFAULT_NARRATOR in prompt


# ---------------------------------------------------------------------
# N-3：LLM 仍返回旁白音色时，兜底改选
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_n3_llm_reusing_narrator_voice_is_reassigned(env):
    from backend.app.services.character import Character
    from backend.app.services.voice_recommender import recommend_voices_with_llm

    pid = await _make_project("doubao:zh_female_vv_uranus_bigtts")
    env["llm"].recs = [
        {"character_name": "林若雪", "suggested_voice_id": "doubao:zh_female_vv_uranus_bigtts",
         "reason": "少女匹配"},
    ]
    chars = [Character(name="林若雪", gender="女", age="少女", personality="内向")]
    recs = await recommend_voices_with_llm(chars, project_id=pid)

    assert recs[0].suggested_voice_id != "doubao:zh_female_vv_uranus_bigtts", "旁白音色不得分配给角色"
    # 同性别优先：池中剩下的唯一女声
    assert recs[0].suggested_voice_id == "doubao:zh_female_xiaohe_uranus_bigtts"
    assert "旁白音色不可复用" in recs[0].reason


# ---------------------------------------------------------------------
# N-4：显式入参优先于项目设置
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_n4_explicit_narrator_overrides_project(env):
    from backend.app.services.character import Character
    from backend.app.services.voice_recommender import recommend_voices_with_llm

    pid = await _make_project("doubao:zh_female_vv_uranus_bigtts")
    explicit = "doubao:zh_male_ruyaqingnian_uranus_bigtts"
    chars = [Character(name="李明", gender="男", age="青年", personality="开朗")]
    await recommend_voices_with_llm(chars, project_id=pid, narrator_voice_id=explicit)

    ids = _prompt_voice_ids(env["llm"].last_prompt)
    assert explicit not in ids
    # 项目旁白让位给显式入参，不再被剔除
    assert "doubao:zh_female_vv_uranus_bigtts" in ids


# ---------------------------------------------------------------------
# N-5：无法确定旁白音色时不误伤
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_n5_no_narrator_known_means_no_exclusion(env):
    from backend.app.services.character import Character
    from backend.app.services.voice_recommender import recommend_voices_with_llm

    env["doubao"] = [v for v in DOUBAO_VOICES if v["id"] != DEFAULT_NARRATOR]
    pid = await _make_project(None)
    chars = [Character(name="李明", gender="男", age="青年", personality="开朗")]
    await recommend_voices_with_llm(chars, project_id=pid)

    prompt = env["llm"].last_prompt
    expected = {v["id"] for v in env["doubao"]}
    assert _prompt_voice_ids(prompt) == expected, "无法确定旁白音色时不应剔除任何音色"
    assert NARRATOR_HINT_MARK not in prompt
