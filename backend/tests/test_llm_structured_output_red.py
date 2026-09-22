"""LLM 结构化输出健壮性回归测试（MiniMax provider + polish schema）。

覆盖：
  T-PL1  PolishResult 不再包含未被消费的 diff 字段（它是"模型把内层数组当答案"的诱因）
  T-PL2  顶层返回数组时，重试会追加【格式纠正】提示（不只是提温碰运气）
  T-PL3  schema 期望对象时，兜底提取优先选"含全部必填字段的对象"（响应外层不完整/被数组包裹也能救回）
  T-PL4  合法对象 + 尾部垃圾字符 → 单次即可救回，不触发重试
  T-PL5  全部重试失败时，FAIL 日志带上 finish_reason / tok_c / resp_chars（可诊断"是否被截断"）
"""
from __future__ import annotations

import json

import pytest


GOOD_OBJ = (
    '{"polished_text": "林若雪走在回家的路上，心里想着明天的考试。", '
    '"is_reasonable": true, "reason": "ok"}'
)
# 模型把内层"改动数组"当成整个答案返回（线上真实形态）
ARRAY_ONLY = '[{"type": "replace", "old": "心理", "new": "心里", "position": 12}]'
# 合法对象被包在一个数组里（响应外层不完整时的典型残留）
OBJ_INSIDE_ARRAY = '["说明", {"polished_text": "改后文本", "is_reasonable": true, "reason": "ok"}]'
# 合法对象 + 尾部垃圾字符
OBJ_TRAILING_JUNK = (
    '{"polished_text": "改后文本", "is_reasonable": true, "reason": "ok"}]'
)


class _FakeResponse:
    def __init__(self, content: str, status_code: int = 200):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._payload = {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """按调用次序返回预设响应，并把每次请求体记进 sink。"""

    def __init__(self, contents: list[str], sink: list[dict]):
        self._contents = contents
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):  # noqa: A002 (httpx 关键字名)
        self._sink.append(json or {})
        idx = min(len(self._sink) - 1, len(self._contents) - 1)
        return _FakeResponse(self._contents[idx])


def _patch(monkeypatch, contents: list[str]) -> list[dict]:
    import httpx
    from app.ai.providers.minimax import llm as llm_mod

    sink: list[dict] = []
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(contents, sink)
    )

    async def _noop_sleep(_s):
        return None

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _noop_sleep)
    return sink


def _provider():
    from app.ai.providers.minimax.llm import MiniMaxLLMProvider

    return MiniMaxLLMProvider(api_key="test", base_url="http://fake.local/v1")


# ---------------------------------------------------------------------
# T-PL1 schema 瘦身
# ---------------------------------------------------------------------
def test_pl1_polish_result_drops_unused_diff_field():
    from app.services.polish import PolishResult

    assert "diff" not in PolishResult.model_fields
    schema = PolishResult.model_json_schema()
    assert schema.get("type") == "object"
    assert "diff" not in schema.get("properties", {})
    # reason 给了默认值 → 不进 required，少一个校验失败面
    assert list(schema.get("required") or []) == ["polished_text", "is_reasonable"]


# ---------------------------------------------------------------------
# T-PL2 形状错误重试带纠偏提示
# ---------------------------------------------------------------------
async def test_pl2_retry_appends_format_correction_hint(monkeypatch):
    from app.services.polish import PolishResult

    sink = _patch(monkeypatch, [ARRAY_ONLY, GOOD_OBJ])
    result = await _provider().chat_structured(
        prompt="随便一段原文", output_schema=PolishResult, max_retries=3
    )

    assert result.polished_text.startswith("林若雪")
    assert len(sink) == 2, "第 1 次形状错 → 第 2 次成功"
    first_user = sink[0]["messages"][1]["content"]
    second_user = sink[1]["messages"][1]["content"]
    assert "格式纠正" not in first_user, "首次请求不应带纠偏提示"
    assert "格式纠正" in second_user, "重试必须带纠偏提示，而不是只提温"
    assert "polished_text" in second_user, "提示里要写明必须包含的字段"
    assert second_user.startswith(first_user), "只在原 prompt 后追加，不改动原 prompt"


# ---------------------------------------------------------------------
# T-PL3 期望对象时优先选对象（核心修复）
# ---------------------------------------------------------------------
async def test_pl3_prefers_object_candidate_when_schema_expects_object(monkeypatch):
    from app.services.polish import PolishResult

    sink = _patch(monkeypatch, [OBJ_INSIDE_ARRAY])
    result = await _provider().chat_structured(
        prompt="随便一段原文", output_schema=PolishResult, max_retries=3
    )

    assert result.polished_text == "改后文本"
    assert len(sink) == 1, "兜底能直接救回，不该白跑重试"


# ---------------------------------------------------------------------
# T-PL4 对象 + 尾部垃圾
# ---------------------------------------------------------------------
async def test_pl4_object_with_trailing_junk_is_salvaged(monkeypatch):
    from app.services.polish import PolishResult

    sink = _patch(monkeypatch, [OBJ_TRAILING_JUNK])
    result = await _provider().chat_structured(
        prompt="随便一段原文", output_schema=PolishResult, max_retries=3
    )

    assert result.polished_text == "改后文本"
    assert len(sink) == 1


# ---------------------------------------------------------------------
# T-PL5 FAIL 日志可诊断
# ---------------------------------------------------------------------
async def test_pl5_fail_log_includes_finish_reason_and_tokens(monkeypatch, caplog):
    from app.services.polish import PolishResult

    _patch(monkeypatch, [ARRAY_ONLY])  # 每次都回数组 → 3 次全败
    with caplog.at_level("WARNING"):
        with pytest.raises(Exception):
            await _provider().chat_structured(
                prompt="随便一段原文", output_schema=PolishResult, max_retries=2
            )

    fail_lines = [r.getMessage() for r in caplog.records if "FAIL" in r.getMessage()]
    assert fail_lines, "应有 FAIL 日志"
    assert all("finish_reason=" in m for m in fail_lines)
    assert all("tok_c=" in m for m in fail_lines)
    assert all("resp_chars=" in m for m in fail_lines)
