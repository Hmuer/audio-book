"""Doubao TTS 2.0 + ICL (声音复刻) 合成 Provider.

实现 BaseTTSProvider，职责：
  - list_voices()：返回内置 14 条典型豆包 TTS 2.0 音色 + 用户自定义 voices_doubao.json；
    全部音色 id 带 "doubao:" 前缀（与工厂路由约定一致）。
  - synthesize_to_bytes()：调用豆包 TTS 2.0 /v1/tts 接口，
    兼容 emotion/speed/instruction_text/speaker_style；遇到 429 按 Retry-After 指数退避重试最多 5 次；
    合成走 factory._doubao_rpm_wait_acquire("tts") 统一限流。
  - ICL 音色（id="icl:<clone_id>"）：使用同一 TTS 接口，但 speaker_id 传入 clone_id（不剥离 "icl:"）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time as _time
from pathlib import Path
from typing import Any

from backend.app.ai.base import BaseTTSProvider
from backend.app.ai.factory import _doubao_rpm_wait_acquire
from backend.app.core.config import settings

logger = logging.getLogger(__name__)


def _estimate_mp3_duration_ms(data: bytes) -> int:
    """纯试探性：按 128kbps 估算时长，缺失或过小返回 0。"""
    if len(data) < 10:
        return 0
    # 采样率/位率从帧头估算
    # 简单 fallback： 128 kbps ≈ 16000 bytes/s
    return int(len(data) / 16.0)


# =====================================================================
# 内置 14 条典型豆包音色（T-DV1a）：覆盖面广，便于 VoicePicker 多维筛选
# =====================================================================
_BUILTIN_VOICES: list[dict[str, Any]] = [
    {
        "id": "zh_female_qingxin", "name": "清新女声", "gender": "female",
        "age": "youth", "scene": ["日常", "新闻"], "dialect": "",
        "zh_tags": ["通用", "新闻", "叙事"],
    },
    {
        "id": "zh_female_wanwanxiaohe", "name": "弯弯小河", "gender": "female",
        "age": "youth", "scene": ["有声书", "情感"], "dialect": "",
        "zh_tags": ["甜美", "情感", "故事"],
    },
    {
        "id": "zh_male_qingnianqingche", "name": "青年清澈", "gender": "male",
        "age": "youth", "scene": ["有声书", "日常"], "dialect": "",
        "zh_tags": ["青年", "清澈", "叙事"],
    },
    {
        "id": "zh_female_tianmei", "name": "甜美女声", "gender": "female",
        "age": "teen", "scene": ["亲子", "娱乐"], "dialect": "",
        "zh_tags": ["萝莉", "可爱", "甜美"],
    },
    {
        "id": "zh_male_chuangshijia", "name": "创业家", "gender": "male",
        "age": "middle", "scene": ["商业", "演讲"], "dialect": "",
        "zh_tags": ["商务", "成熟", "演讲"],
    },
    {
        "id": "zh_female_yunxi", "name": "芸夕", "gender": "female",
        "age": "middle", "scene": ["有声书", "散文"], "dialect": "",
        "zh_tags": ["知性", "散文", "情感"],
    },
    {
        "id": "zh_male_chengshushenchen", "name": "成熟深沉男声", "gender": "male",
        "age": "middle", "scene": ["广告", "纪录片"], "dialect": "",
        "zh_tags": ["广告", "成熟", "沉稳"],
    },
    {
        "id": "zh_female_guangbozhuchi", "name": "广播主持女声", "gender": "female",
        "age": "youth", "scene": ["新闻", "播报"], "dialect": "",
        "zh_tags": ["新闻", "播报", "专业"],
    },
    {
        "id": "zh_male_nanyou_44100", "name": "男友音", "gender": "male",
        "age": "teen", "scene": ["恋爱", "有声书"], "dialect": "",
        "zh_tags": ["少年", "温柔", "恋爱"],
    },
    {
        "id": "zh_female_sunshine", "name": "阳光女声", "gender": "female",
        "age": "youth", "scene": ["教育", "儿童"], "dialect": "",
        "zh_tags": ["阳光", "教育", "活力"],
    },
    {
        "id": "zh_male_dianshizhuchi", "name": "电视主持男声", "gender": "male",
        "age": "middle", "scene": ["新闻", "晚会"], "dialect": "",
        "zh_tags": ["主持", "新闻", "权威"],
    },
    {
        "id": "zh_female_aidaier", "name": "爱黛儿", "gender": "female",
        "age": "youth", "scene": ["英文", "日常"], "dialect": "",
        "zh_tags": ["中英混合", "外语", "成熟"],
    },
    {
        "id": "zh_male_xiaohai", "name": "小男孩", "gender": "male",
        "age": "child", "scene": ["儿童", "动画"], "dialect": "",
        "zh_tags": ["男童", "儿童", "童真"],
    },
    {
        "id": "zh_female_lisachangjiang", "name": "丽莎长讲（四川话）", "gender": "female",
        "age": "youth", "scene": ["方言", "搞笑"], "dialect": "sichuan",
        "zh_tags": ["四川话", "方言", "搞笑"],
    },
]


# 接口文档参考：豆包火山文档 v1/tts（中文合成）
class DoubaoTTSProvider(BaseTTSProvider):
    name = "doubao_tts"
    provider = "doubao"

    # 豆包 TTS 支持倍速范围 0.2 ~ 3.0（相对 speed_ratio）
    MIN_SPEED = 0.2
    MAX_SPEED = 3.0
    DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v1/tts"

    # 重试参数（与 MiniMaxTTSProvider 对齐）
    MAX_RETRIES = 5
    BASE_BACKOFF_SECS = 0.6
    JITTER_SECS = 0.3

    # -----------------------------------------------------------------
    # 音色列表
    # -----------------------------------------------------------------
    def _builtin_voices_sync(self) -> list[dict[str, Any]]:
        """同步返回内置 14 条音色（全量已填 provider=doubao + id 前缀 doubao:）。"""
        out: list[dict[str, Any]] = []
        for v in _BUILTIN_VOICES:
            item: dict[str, Any] = {
                "id": f"doubao:{v['id']}",
                "name": v["name"],
                "gender": v["gender"],
                "age": v["age"],
                "scene": v["scene"],
                "dialect": v["dialect"],
                "zh_tags": list(v["zh_tags"]),
                "provider": "doubao",
            }
            out.append(item)
        return out

    async def list_voices(self) -> list[dict[str, Any]]:
        """合并内置 14 条 + DATA_DIR/voices_doubao.json（存在时覆盖）。"""
        voices_by_id: dict[str, dict[str, Any]] = {}
        for v in self._builtin_voices_sync():
            voices_by_id[v["id"]] = v

        try:
            custom_path = Path(settings.DATA_DIR) / "voices_doubao.json"
            if custom_path.exists():
                raw = json.loads(custom_path.read_text(encoding="utf-8"))
                for v in raw:
                    # 强制 id 规范：若缺前缀自动补 doubao:
                    vid = str(v.get("id", ""))
                    if not vid.startswith("doubao:") and not vid.startswith("icl:"):
                        vid = f"doubao:{vid}"
                    merged: dict[str, Any] = {
                        "id": vid,
                        "name": v.get("name") or vid.split(":", 1)[-1],
                        "provider": "doubao",
                        "gender": v.get("gender", "neutral"),
                        "age": v.get("age", "youth"),
                        "scene": v.get("scene", ["自定义"]),
                        "dialect": v.get("dialect", ""),
                        "zh_tags": list(v.get("zh_tags") or ["自定义"]),
                    }
                    # 任何额外字段原样保留（例如 sample_url / description）
                    for k, val in v.items():
                        if k not in merged and k != "id":
                            merged[k] = val
                    voices_by_id[vid] = merged
        except Exception:
            logger.exception("读取 voices_doubao.json 失败，仅使用内置音色")
        return list(voices_by_id.values())

    # -----------------------------------------------------------------
    # 合成入口（同步 + 文件）
    # -----------------------------------------------------------------
    @staticmethod
    def _strip_voice_id_for_api(voice_id: str) -> str:
        """根据 voice_id 的命名空间返回 (真实 speaker_id)
        - doubao:xxx → xxx
        - icl:xxx → xxx（豆包 ICL 直接用 clone_id 当 speaker_id 传入）
        - 无前缀：直接返回
        """
        if voice_id.startswith("icl:"):
            return voice_id.split(":", 1)[1]
        if voice_id.startswith("doubao:"):
            return voice_id.split(":", 1)[1]
        return voice_id

    @staticmethod
    def _clamp_speed(speed: float) -> float:
        v = float(speed or 1.0)
        if math.isnan(v) or v <= 0:
            v = 1.0
        return max(DoubaoTTSProvider.MIN_SPEED, min(DoubaoTTSProvider.MAX_SPEED, v))

    def _build_payload(
        self,
        text: str,
        voice_id: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
        instruction_text: str | None = None,
        speaker_style: str | None = None,
    ) -> dict[str, Any]:
        """构造豆包 TTS v1 请求体（与 T-DV3 测试对齐）。"""
        speaker_for_api = self._strip_voice_id_for_api(voice_id)
        speed_ratio = self._clamp_speed(speed)
        body: dict[str, Any] = {
            "text": text,                    # 平铺
            "voice_id": speaker_for_api,     # 平铺：便于 3rd-party 代理或测试校验
            "speaker_id": speaker_for_api,   # 兼容字段别名
            "speaker": speaker_for_api,      # 兼容字段别名
            "app": {
                # 豆包 app.userid 用于审计；取进程 ID 做一个固定但不敏感标识
                "appid": "",
                "token": "",
                "cluster": "volcano_tts",
            },
            "user": {"uid": f"local-{os.getpid() % 10000:04d}"},
            "audio": {
                "voice_type": speaker_for_api,
                "encoding": "mp3",
                "speed_ratio": speed_ratio,
                "speed": speed_ratio,
                "rate": speed_ratio,
                "volume_ratio": 1.0,
                "pitch_ratio": 1.0,
            },
            "request": {
                "reqid": f"novel-{int(_time.time()*1000)}",
                "text": text,
                "text_type": "plain",
                "operation": "query",
            },
        }
        # 额外可扩展参数：豆包 TTS 2.0 的 emotion / instruction_text / speaker_style
        # 若官方后续接入扩展字段，我们已先在请求体中携带，避免再改 provider
        ext = body.setdefault("extend_params", {})
        if emotion:
            ext["emotion"] = emotion
            body["audio"]["emotion"] = emotion
            body["emotion"] = emotion
        if speaker_style:
            ext["speaker_style"] = speaker_style
            body["speaker_style"] = speaker_style
        if instruction_text:
            ext["instruction_text"] = instruction_text
            body["instruction_text"] = instruction_text
        # 顶层平铺 speed / speed_ratio / rate
        body["speed_ratio"] = speed_ratio
        body["speed"] = speed_ratio
        body["rate"] = speed_ratio
        return body

    async def synthesize_to_bytes(
        self,
        text: str,
        voice_id: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
        instruction_text: str | None = None,
        speaker_style: str | None = None,
    ) -> tuple[bytes, int]:
        """调用豆包 TTS 2.0 接口合成，返回 (mp3_bytes, duration_ms)。"""
        if not text or not text.strip():
            empty = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 48)
            return empty, 0

        payload = self._build_payload(
            text, voice_id, emotion=emotion, speed=speed,
            instruction_text=instruction_text, speaker_style=speaker_style,
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._get_authorization(),
        }
        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                # 豆包 RPM 限流（固定间隔 token bucket）
                await _doubao_rpm_wait_acquire("tts")
                # 真正请求
                mp3_bytes = await self._http_post_bytes(
                    self.DEFAULT_ENDPOINT, headers, payload
                )
                dur_ms = _estimate_mp3_duration_ms(mp3_bytes)
                return mp3_bytes, dur_ms
            except Exception as e:
                last_exc = e
                # 解析 Retry-After（若 429 提供）
                retry_after = None
                resp_obj = getattr(e, "response", None)
                if resp_obj is not None:
                    try:
                        ra = resp_obj.headers.get("Retry-After")
                        if ra is not None:
                            retry_after = float(ra)
                    except Exception:
                        retry_after = None
                is_rate = (
                    resp_obj is not None and getattr(resp_obj, "status_code", None) == 429
                )
                is_retryable = is_rate or (
                    resp_obj is not None and 500 <= int(getattr(resp_obj, "status_code", 0)) < 600
                )
                if attempt >= self.MAX_RETRIES or not is_retryable:
                    logger.warning(
                        f"[DoubaoTTS] 放弃重试（attempt={attempt}/{self.MAX_RETRIES}）："
                        f"{type(e).__name__}: {e}"
                    )
                    break
                if retry_after is not None:
                    wait_s = retry_after + self.JITTER_SECS
                else:
                    wait_s = self.BASE_BACKOFF_SECS * (2 ** (attempt - 1)) + self.JITTER_SECS
                logger.warning(
                    f"[DoubaoTTS] 重试（attempt={attempt}/{self.MAX_RETRIES}）："
                    f"指数退避 {wait_s:.1f}s {type(e).__name__}: {e}"
                )
                await asyncio.sleep(wait_s)
        assert last_exc is not None
        raise RuntimeError(f"豆包 TTS 合成失败：{type(last_exc).__name__}: {last_exc}") from last_exc

    async def synthesize_to_file(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
        instruction_text: str | None = None,
        speaker_style: str | None = None,
    ) -> tuple[str, int]:
        """返回 (output_path, duration_ms)，原子写。"""
        data, dur_ms = await self.synthesize_to_bytes(
            text, voice_id,
            emotion=emotion, speed=speed,
            instruction_text=instruction_text, speaker_style=speaker_style,
        )
        # 原子写：先写 .tmp 再 os.replace
        tmp_path = output_path + ".tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, output_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return output_path, dur_ms

    # -----------------------------------------------------------------
    # HTTP / 配置 辅助方法
    # -----------------------------------------------------------------
    def _get_authorization(self) -> str:
        """优先取 DOUBAO_AK（TTS 访问令牌），不存在则尝试 MEGACORE_ACCESS_KEY_FROM_ENV。"""
        ak = settings.DOUBAO_AK
        if ak:
            # 豆包 TTS 令牌通常是 Bearer/直接 AK 两种风格都可，这里原样传
            if " " in ak:
                return ak.strip()
            return f"Bearer;{ak}"
        # 兜底尝试 MegaCore ENV 名称（与 config 保持一致）
        fallback = os.environ.get("MEGACORE_ACCESS_KEY_FROM_ENV") or ""
        if fallback:
            if " " in fallback:
                return fallback.strip()
            return f"Bearer;{fallback}"
        raise RuntimeError("未配置豆包凭据：请设置 DOUBAO_AK 环境变量（或 MEGACORE_ACCESS_KEY_FROM_ENV）")

    async def _http_post_bytes(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> bytes:
        """发送 POST 请求并返回响应体（bytes）；对 HTTP 错误统一用 httpx 异常抛出。"""
        import httpx  # 在函数内导入以便 mock

        # 构造异步请求，超时 60s（长文本合成需要更长时间）
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return await resp.aread()
