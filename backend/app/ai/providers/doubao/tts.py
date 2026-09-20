"""Doubao TTS 2.0 + ICL (声音复刻) 合成 Provider.

实现 BaseTTSProvider，职责：
  - list_voices()：返回内置 100+ 条豆包官方音色（覆盖通用 / 方言 / 外语 / 古风 /
    配音 / 童声 / 新闻等）+ 用户自定义 voices_doubao.json；
    全部音色 id 带 "doubao:" 前缀（与工厂路由约定一致）。
  - synthesize_to_bytes()：调用豆包 TTS 2.0 /v1/tts 接口，
    兼容 emotion/speed/instruction_text/speaker_style；遇到 429 按 Retry-After 指数退避重试最多 5 次；
    合成走 factory._doubao_rpm_wait_acquire("tts") 统一限流。
  - ICL 音色（id="icl:<clone_id>"）：使用同一 TTS 接口，但 speaker_id 传入 clone_id（不剥离 "icl:"）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import tempfile
import time as _time
import uuid
from pathlib import Path
from typing import Any

from backend.app.ai.base import BaseTTSProvider
from backend.app.ai.factory import _doubao_rpm_wait_acquire
from backend.app.core.config import settings

logger = logging.getLogger(__name__)

# =====================================================================
# 5xx fastfail 重试策略（v1 / v3 共用，避免两个类各写一份导致分叉）
# ---------------------------------------------------------------------
# 5xx 是上游服务问题，重试 5 次要白等 10s+；上游 500 多为短暂瞬时。
# _MAX_5XX_RETRIES=2 表示最多尝试 2 次（即 1 次重试），把用户感知从 13s 降到 < 3s。
# 业务码可重试（3001/3002/3003/3010/3011）仍走 MAX_RETRIES —— 那是客户端配额相关，
# 与 5xx 不是一类问题，因此单独定常量。
# =====================================================================
_MAX_5XX_RETRIES = 2
_HTTP_5XX_BACKOFF_SECS = 0.3


def _estimate_mp3_duration_ms(data: bytes) -> int:
    """解析 MPEG audio frame header 估算 MP3 时长（毫秒）。

    协议背景：
      官方豆包 v1 TTS 接口（`/api/v1/tts`）返回 JSON：
        {"code": 3000, "message": "Success", "data": "<base64 mp3>"}
      - 业务码 3000 = 成功；其他 = 失败（可重试 vs 不可重试见 _classify_v1_code）
      - data 是 base64 编码的 MP3 字节流；HTTP 状态码始终 200，
        不能用 status_code 判定业务成功与否——必须看业务码。

    这里用首帧 MPEG frame header 解出 bitrate/sample_rate，
    估算 total_ms = (file_bytes * 8) / bitrate * 1000。
    精度 ±50ms，足以 SRT 对齐 / 章节拼接。
    """
    if not data or len(data) < 10:
        return 0
    # MPEG audio frame header: 11 bits all set (0xFFE / 0xFFF)
    # 在 data 里扫描第一个有效帧头（跳过 ID3v2 tag）
    pos = 0
    if data[:3] == b"ID3":
        # ID3v2 header: 10 bytes；size 是 syncsafe integer 在 byte 6-9
        try:
            sz = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
            pos = 10 + sz
        except Exception:
            pos = 0
    while pos < len(data) - 4:
        b1, b2 = data[pos], data[pos + 1]
        if b1 == 0xFF and (b2 & 0xE0) == 0xE0:
            # 解析 MPEG header byte 2-3
            version = (b2 >> 3) & 0x3           # 0=MPEG2.5, 2=MPEG2, 3=MPEG1
            layer = (b2 >> 1) & 0x3             # 1=Layer3
            br_idx = (data[pos + 2] >> 4) & 0xF
            sr_idx = (data[pos + 2] >> 2) & 0x3
            # bitrate 表 (kbps)，Layer III：MPEG1 / MPEG2 / MPEG2.5
            # MPEG1:   [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,-]
            # MPEG2/2.5:[0,8,16,24,32,40,48,56,64,80,96,112,128,144,160,-]
            bitrate_table_m1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
            bitrate_table_m2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
            # sample_rate table (Hz)：MPEG1: 44100/22050/11025；MPEG2: 22050/...
            sr_table_m1 = [44100, 48000, 32000, 0]
            sr_table_m2 = [22050, 24000, 16000, 0]
            sr_table_m25 = [11025, 12000, 8000, 0]
            if layer != 1:
                pos += 1
                continue
            if version == 3:
                bitrate = bitrate_table_m1[br_idx] * 1000
                sample_rate = sr_table_m1[sr_idx]
            elif version == 2:
                bitrate = bitrate_table_m2[br_idx] * 1000
                sample_rate = sr_table_m2[sr_idx]
            else:  # version == 0 (MPEG2.5)
                bitrate = bitrate_table_m2[br_idx] * 1000
                sample_rate = sr_table_m25[sr_idx]
            if bitrate <= 0 or sample_rate <= 0:
                pos += 1
                continue
            # 时长 = bytes / (bitrate / 8)，转换为 ms
            duration_ms = int(len(data) * 8 / bitrate * 1000)
            return duration_ms
        pos += 1
    # 兜底：所有解析失败，退化为 128kbps 估算
    return int(len(data) / 16.0)


# 官方 v1 业务码分类（仅与本 Provider 的响应解析相关）
# - 成功：3000
# - 客户端可重试：3001, 3002, 3003, 3010, 3011（参数/限流/网络抖动）
# - 客户端不可重试：3004..3009, 3012+（鉴权/余额/不存在资源等）
_V1_RETRYABLE_CODES = {3001, 3002, 3003, 3010, 3011}


class DoubaoTTSResponseError(RuntimeError):
    """豆包 v1 TTS 业务码非 3000。

    携带业务码与 message，方便上层做业务级重试判定 + 错误聚类。
    response 不一定有（POST 请求被底层拦了），所以是 Optional。
    """

    def __init__(
        self,
        message: str,
        *,
        code: int,
        logid: str | None = None,
        response: Any | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.logid = logid
        self.response = response

    @property
    def is_retryable(self) -> bool:
        return self.code in _V1_RETRYABLE_CODES


async def _post_json_for_v1(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    *,
    timeout_read_s: float = 60.0,
) -> tuple[dict[str, Any], str | None]:
    """发 POST，期望响应是 JSON；返回 (parsed_json_dict, x_tt_logid)。

    不抛 HTTPStatusError：HTTP 错误由调用方根据业务码决定下一步。
    但 httpx 连接异常 / 超时仍会抛（这是真网络错，应重试）。
    """
    import httpx  # 函数内导入便于 mock

    timeout = httpx.Timeout(connect=10.0, read=timeout_read_s, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
        logid = resp.headers.get("X-Tt-Logid") or resp.headers.get("x-tt-logid")
        # 响应体读完再判定（业务码非 3000 时 HTTP 仍可能 200）
        raw = await resp.aread()
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # 非 2xx 时上游会在 body 里给出真正的原因（业务码 / message），而
            # raise_for_status() 只抛一句 "Server error '500 ...'"，把 body 丢掉会让
            # 排查只剩一个 logid（实测踩到：v1 稳定 500 却看不到原因）。
            # 保留原异常上的 request/response（重试判定依赖 .response.status_code），
            # 仅把上游响应体补进 message。
            # 注：httpx 的 `.request` 是 property，未设置时会抛 RuntimeError，
            # 所以取底层 `_request` 而不是 `e.request`。
            body = raw.decode("utf-8", "replace").strip()
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {body[:500] or '(empty body)'}",
                request=getattr(e, "_request", None),
                response=getattr(e, "response", None) or resp,
            ) from None
        try:
            data = json.loads(raw)
        except Exception as e:
            raise DoubaoTTSResponseError(
                f"响应不是合法 JSON：{type(e).__name__}: {e}",
                code=-1,
                logid=logid,
            ) from None
        if not isinstance(data, dict):
            raise DoubaoTTSResponseError(
                f"响应 JSON 不是对象：{type(data).__name__}",
                code=-1,
                logid=logid,
            )
        return data, logid


# =====================================================================
# 豆包官方内置音色清单（大模型 2.0 uranus 系列）。
# 数据来源（按官方文档核对）：
#   - 大模型 2.0 音色表：https://docs.volcengine.com/docs/6561/1257544?lang=zh
#
# 命名空间约定（调用方通过 _strip_voice_id_for_api 剥前缀后直接传给豆包 API）：
#   - doubao:zh_*_uranus_bigtts   → 大模型 2.0 通用 / 角色配音（seed-tts-2.0）
#   - doubao:ICL_uranus_*_tob     → 大模型 2.0 角色 ICL 系（seed-tts-2.0）
#
# 说明：1.0 小模型音色（seed-tts-1.0，官方文档 97465 的 BV* 系列）已整表删除 ——
#   该账号未开通 1.0 资源，v3 端点请求 1.0 会 403 "resource not granted"；
#   且 1.0 音色不支持语音指令与标签。音色体系只保留 2.0 + 声音复刻（ICL 2.0）。
#
# 每条音色提供：
#   - id          官方真实 voice_type（直接透传给豆包 API）
#   - name        中文展示名（取自官方音色名）
#   - gender      female / male / neutral
#   - age         child / teen / youth / middle / senior
#   - scene       适用场景标签列表（中文，便于前端多维筛选）
#   - dialect     方言（cantonese / sichuan / dongbei / shaanxi / shanghai /
#                 minnan / changsha / tianjin / shandong / henan / xinjiang 等；""=普通话）
#   - languages   支持语种列表（zh / en / ja / ko / ptbr / esmx / id / thth / vivn 等）
#   - supports_emotion    是否支持 emotion 多情感（基于官方「支持情感/风格类型」列）
#   - supports_subtitle   是否支持 enable_subtitle 字级别时间戳（基于官方「时间戳 ✔」列）
#   - supports_language   是否支持 language 多语种参数（基于官方「支持语种」列）
#   - free        是否免费音色（内置 2.0 音色均为 False；远程/自定义音色可自行声明）
#   - model       推荐使用的豆包模型（内置条目恒为 "seed-tts-2.0"；复刻音色走 "seed-icl-2.0"）
#   - provider    固定为 "doubao"（list_voices 输出时再补）
#
# 维护要点：
#   - 豆包会不定期新增音色；如有新 ID 需求，可在 voices_doubao.json 里追加覆盖
#     （内置条目作为 id 默认存在时，自定义条目按 id 覆盖）。
#   - 内置表只收录官方 2.0 文档里真实存在的 *_uranus_bigtts / ICL_uranus_*_tob id；
#     1.0 小模型 BV* 命名（*_streaming / *_V2_streaming / *_24k_streaming）已整表移除。
# =====================================================================
_BUILTIN_VOICES: list[dict[str, Any]] = [
    # =================================================================
    # 大模型 2.0 音色（官方 1257544 文档 · 通用场景 + 角色配音 + 视频配音）
    # 仅收录"豆包语音合成模型 2.0 / S2S-O2.0 / S2S-全双工"音色（model=seed-tts-2.0）
    # =================================================================
    # ===== 通用场景 =====
    {"id": "zh_female_vv_uranus_bigtts", "name": "Vivi 2.0", "gender": "female", "age": "youth",
     "scene": ["通用", "S2S"], "dialect": "", "languages": ["zh", "ja", "id", "esmx", "cantonese", "shanghai", "henan", "beijing", "tianjin", "sichuan", "shaanxi", "dongbei"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": True,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "S2S", "多语种", "多方言"]},
    {"id": "zh_female_xiaohe_uranus_bigtts", "name": "小何 2.0", "gender": "female", "age": "youth",
     "scene": ["通用", "S2S"], "dialect": "", "languages": ["zh", "cantonese", "shanghai", "henan", "beijing", "tianjin", "sichuan", "shaanxi", "dongbei"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": True,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "S2S", "多方言"]},
    {"id": "zh_male_m191_uranus_bigtts", "name": "云舟 2.0", "gender": "male", "age": "middle",
     "scene": ["通用", "S2S"], "dialect": "", "languages": ["zh", "cantonese", "shanghai", "henan", "beijing", "tianjin", "sichuan", "shaanxi", "dongbei"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": True,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "S2S", "男声", "多方言"]},
    {"id": "zh_male_taocheng_uranus_bigtts", "name": "小天 2.0", "gender": "male", "age": "youth",
     "scene": ["通用", "S2S"], "dialect": "", "languages": ["zh", "cantonese", "shanghai", "henan", "beijing", "tianjin", "sichuan", "shaanxi", "dongbei"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": True,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "S2S", "男声", "多方言"]},
    {"id": "zh_male_liufei_uranus_bigtts", "name": "刘飞 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "男声"]},
    {"id": "zh_female_sophie_uranus_bigtts", "name": "魅力苏菲 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "女声"]},
    {"id": "zh_female_qingxinnvsheng_uranus_bigtts", "name": "清新女声 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "女声", "清新"]},
    # ===== 角色扮演 =====
    {"id": "zh_female_cancan_uranus_bigtts", "name": "知性灿灿 2.0", "gender": "female", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "灿灿"]},
    {"id": "zh_female_sajiaoxuemei_uranus_bigtts", "name": "撒娇学妹 2.0", "gender": "female", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "少女"]},
    {"id": "zh_female_tianmeixiaoyuan_uranus_bigtts", "name": "甜美小源 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "甜美"]},
    {"id": "zh_female_tianmeitaozi_uranus_bigtts", "name": "甜美桃子 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "甜美"]},
    {"id": "zh_female_shuangkuaisisi_uranus_bigtts", "name": "爽快思思 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "爽快"]},
    {"id": "zh_female_peiqi_uranus_bigtts", "name": "佩奇猪 2.0", "gender": "female", "age": "child",
     "scene": ["视频配音", "角色"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["视频", "大模型", "动画"]},
    {"id": "zh_female_linjianvhai_uranus_bigtts", "name": "邻家女孩 2.0", "gender": "female", "age": "teen",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "少女"]},
    {"id": "zh_male_shaonianzixin_uranus_bigtts", "name": "少年梓辛 2.0", "gender": "male", "age": "teen",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "少年"]},
    {"id": "zh_male_sunwukong_uranus_bigtts", "name": "猴哥 2.0", "gender": "male", "age": "middle",
     "scene": ["视频配音", "角色"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["视频", "大模型", "角色"]},
    {"id": "zh_female_yingyujiaoxue_uranus_bigtts", "name": "Tina 老师 2.0", "gender": "female", "age": "youth",
     "scene": ["教育"], "dialect": "", "languages": ["zh", "en"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["教育", "大模型", "双语"]},
    {"id": "zh_female_kefunvsheng_uranus_bigtts", "name": "暖阳女声 2.0", "gender": "female", "age": "youth",
     "scene": ["客服"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["客服", "大模型"]},
    {"id": "zh_female_xiaoxue_uranus_bigtts", "name": "儿童绘本 2.0", "gender": "female", "age": "child",
     "scene": ["有声阅读", "儿童"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["有声书", "大模型", "儿童"]},
    {"id": "zh_male_dayi_uranus_bigtts", "name": "大壹 2.0", "gender": "male", "age": "middle",
     "scene": ["视频配音"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["视频", "大模型", "男声"]},
    {"id": "zh_female_mizai_uranus_bigtts", "name": "黑猫侦探社咪仔 2.0", "gender": "female", "age": "child",
     "scene": ["视频配音"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["视频", "大模型", "童声"]},
    {"id": "zh_female_jitangnv_uranus_bigtts", "name": "鸡汤女 2.0", "gender": "female", "age": "youth",
     "scene": ["视频配音"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["视频", "大模型"]},
    {"id": "zh_female_meilinvyou_uranus_bigtts", "name": "魅力女友 2.0", "gender": "female", "age": "youth",
     "scene": ["通用", "情感"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "情感"]},
    {"id": "zh_female_liuchangnv_uranus_bigtts", "name": "流畅女声 2.0", "gender": "female", "age": "youth",
     "scene": ["视频配音"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["视频", "大模型"]},
    {"id": "zh_male_ruyayichen_uranus_bigtts", "name": "儒雅逸辰 2.0", "gender": "male", "age": "middle",
     "scene": ["视频配音"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["视频", "大模型", "儒雅"]},
    # ===== 外语音色（美式英语） =====
    {"id": "en_male_tim_uranus_bigtts", "name": "Tim", "gender": "male", "age": "youth",
     "scene": ["外语音色"], "dialect": "", "languages": ["en"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["英文", "美式", "大模型"]},
    {"id": "en_female_dacey_uranus_bigtts", "name": "Dacey", "gender": "female", "age": "youth",
     "scene": ["外语音色"], "dialect": "", "languages": ["en"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["英文", "美式", "大模型"]},
    {"id": "en_female_stokie_uranus_bigtts", "name": "Stokie", "gender": "female", "age": "youth",
     "scene": ["外语音色"], "dialect": "", "languages": ["en"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["英文", "美式", "大模型"]},
    # ===== 通用场景（补充热门） =====
    {"id": "zh_female_wenroumama_uranus_bigtts", "name": "温柔妈妈 2.0", "gender": "female", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_jieshuoxiaoming_uranus_bigtts", "name": "解说小明 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "解说"]},
    {"id": "zh_female_tvbnv_uranus_bigtts", "name": "TVB 女声 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "粤语", "TVB"]},
    {"id": "zh_male_yizhipiannan_uranus_bigtts", "name": "译制片男 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "译制片"]},
    {"id": "zh_female_qiaopinv_uranus_bigtts", "name": "俏皮女声 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "俏皮"]},
    {"id": "zh_female_zhishuaiyingzi_uranus_bigtts", "name": "直率英子 2.0", "gender": "female", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型"]},
    {"id": "zh_male_linjiananhai_uranus_bigtts", "name": "邻家男孩 2.0", "gender": "male", "age": "teen",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "少年"]},
    {"id": "zh_male_silang_uranus_bigtts", "name": "四郎 2.0", "gender": "male", "age": "middle",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "古风"]},
    {"id": "zh_male_ruyaqingnian_uranus_bigtts", "name": "儒雅青年 2.0", "gender": "male", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "儒雅"]},
    {"id": "zh_male_qingcang_uranus_bigtts", "name": "擎苍 2.0（大模型）", "gender": "male", "age": "middle",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "擎苍"]},
    {"id": "zh_male_xionger_uranus_bigtts", "name": "熊二 2.0", "gender": "male", "age": "child",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "动画"]},
    {"id": "zh_female_yingtaowanzi_uranus_bigtts", "name": "樱桃丸子 2.0", "gender": "female", "age": "child",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "童声"]},
    {"id": "zh_male_wennuanahu_uranus_bigtts", "name": "温暖阿虎 2.0", "gender": "male", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_naiqimengwa_uranus_bigtts", "name": "奶气萌娃 2.0", "gender": "male", "age": "child",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "萌娃"]},
    {"id": "zh_female_popo_uranus_bigtts", "name": "婆婆 2.0", "gender": "female", "age": "senior",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "长辈"]},
    {"id": "zh_female_gaolengyujie_uranus_bigtts", "name": "高冷御姐 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "御姐"]},
    {"id": "zh_male_aojiaobazong_uranus_bigtts", "name": "傲娇霸总 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "傲娇"]},
    {"id": "zh_male_lanyinmianbao_uranus_bigtts", "name": "懒音绵宝 2.0", "gender": "male", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型"]},
    {"id": "zh_male_fanjuanqingnian_uranus_bigtts", "name": "反卷青年 2.0", "gender": "male", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_female_wenroushunv_uranus_bigtts", "name": "温柔淑女 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "温柔"]},
    {"id": "zh_female_gufengshaoyu_uranus_bigtts", "name": "古风少御 2.0", "gender": "female", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "古风"]},
    {"id": "zh_male_huolixiaoge_uranus_bigtts", "name": "活力小哥 2.0", "gender": "male", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "活力"]},
    {"id": "zh_male_baqiqingshu_uranus_bigtts", "name": "霸气青叔 2.0", "gender": "male", "age": "middle",
     "scene": ["有声阅读"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["有声书", "大模型"]},
    {"id": "zh_male_xuanyijieshuo_uranus_bigtts", "name": "悬疑解说 2.0", "gender": "male", "age": "middle",
     "scene": ["有声阅读"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["有声书", "大模型", "解说"]},
    {"id": "zh_female_mengyatou_uranus_bigtts", "name": "萌丫头 2.0", "gender": "female", "age": "child",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "萌娃"]},
    {"id": "zh_female_tiexinnvsheng_uranus_bigtts", "name": "贴心女声 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_female_jitangmei_uranus_bigtts", "name": "鸡汤妹妹 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_cixingjieshuonan_uranus_bigtts", "name": "磁性解说男声 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "解说"]},
    {"id": "zh_male_liangsangmengzai_uranus_bigtts", "name": "亮嗓萌仔 2.0", "gender": "male", "age": "child",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "萌娃"]},
    {"id": "zh_female_kailangjiejie_uranus_bigtts", "name": "开朗姐姐 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_gaolengchenwen_uranus_bigtts", "name": "高冷沉稳 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_shenyeboke_uranus_bigtts", "name": "深夜播客 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_lubanqihao_uranus_bigtts", "name": "鲁班七号 2.0", "gender": "male", "age": "middle",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "动画"]},
    {"id": "zh_female_jiaochuannv_uranus_bigtts", "name": "娇喘女声 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_female_linxiao_uranus_bigtts", "name": "林潇 2.0", "gender": "female", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型"]},
    {"id": "zh_female_lingling_uranus_bigtts", "name": "玲玲姐姐 2.0", "gender": "female", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型"]},
    {"id": "zh_female_chunribu_uranus_bigtts", "name": "春日部姐姐 2.0", "gender": "female", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型"]},
    {"id": "zh_male_tangseng_uranus_bigtts", "name": "唐僧 2.0", "gender": "male", "age": "middle",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "古风"]},
    {"id": "zh_male_zhuangzhou_uranus_bigtts", "name": "庄周 2.0", "gender": "male", "age": "middle",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "古风"]},
    {"id": "zh_male_kailangdidi_uranus_bigtts", "name": "开朗弟弟 2.0", "gender": "male", "age": "teen",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "少年"]},
    {"id": "zh_male_zhubajie_uranus_bigtts", "name": "猪八戒 2.0", "gender": "male", "age": "middle",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型"]},
    {"id": "zh_female_ganmaodianyin_uranus_bigtts", "name": "感冒电音姐姐 2.0", "gender": "female", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型"]},
    {"id": "zh_female_chanmeinv_uranus_bigtts", "name": "谄媚女声 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_female_nvleishen_uranus_bigtts", "name": "女雷神 2.0", "gender": "female", "age": "youth",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型"]},
    {"id": "zh_female_qinqienv_uranus_bigtts", "name": "亲切女声 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_kuailexiaodong_uranus_bigtts", "name": "快乐小东 2.0", "gender": "male", "age": "teen",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_kailangxuezhang_uranus_bigtts", "name": "开朗学长 2.0", "gender": "male", "age": "teen",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_youyoujunzi_uranus_bigtts", "name": "悠悠君子 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_female_wenjingmaomao_uranus_bigtts", "name": "文静毛毛 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_female_zhixingnv_uranus_bigtts", "name": "知性女声 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_qingshuangnanda_uranus_bigtts", "name": "清爽男大 2.0", "gender": "male", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_yuanboxiaoshu_uranus_bigtts", "name": "渊博小叔 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_yangguangqingnian_uranus_bigtts", "name": "阳光青年 2.0", "gender": "male", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "阳光"]},
    {"id": "zh_female_qingchezizi_uranus_bigtts", "name": "清澈梓梓 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_female_tianmeiyueyue_uranus_bigtts", "name": "甜美悦悦 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "甜美"]},
    {"id": "zh_female_xinlingjitang_uranus_bigtts", "name": "心灵鸡汤 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_wenrouxiaoge_uranus_bigtts", "name": "温柔小哥 2.0", "gender": "male", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "温柔"]},
    {"id": "zh_female_roumeinvyou_uranus_bigtts", "name": "柔美女友 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_dongfanghaoran_uranus_bigtts", "name": "东方浩然 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_female_wenrouxiaoya_uranus_bigtts", "name": "温柔小雅 2.0", "gender": "female", "age": "youth",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型"]},
    {"id": "zh_male_tiancaitongsheng_uranus_bigtts", "name": "天才童声 2.0", "gender": "male", "age": "child",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "童声"]},
    {"id": "zh_female_wuzetian_uranus_bigtts", "name": "武则天 2.0", "gender": "female", "age": "middle",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型", "古风"]},
    {"id": "zh_female_gujie_uranus_bigtts", "name": "顾姐 2.0", "gender": "female", "age": "middle",
     "scene": ["角色扮演"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["角色", "大模型"]},
    {"id": "zh_male_guanggaojieshuo_uranus_bigtts", "name": "广告解说 2.0", "gender": "male", "age": "middle",
     "scene": ["通用"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["通用", "大模型", "广告"]},
    {"id": "zh_female_shaoergushi_uranus_bigtts", "name": "少儿故事 2.0", "gender": "female", "age": "youth",
     "scene": ["有声阅读"], "dialect": "", "languages": ["zh"],
     "supports_emotion": True, "supports_subtitle": True, "supports_language": False,
     "free": False, "model": "seed-tts-2.0",
     "zh_tags": ["有声书", "大模型", "儿童"]},
]


def _voice_supports_emotion(speaker_for_api: str) -> bool:
    """P1-2：模块级 helper — 查 _BUILTIN_VOICES 元数据返回当前音色是否支持 emotion。

    v1 / v3 两条 provider 路径都要用，所以提到模块级避免重复定义。
    ICL 复刻音色（speaker 以 `icl_`/`S_` 开头）走 ICL 2.0 协议，按文档不接收 emotion；
    元数据里找不到也兜底 False（默认"未支持"更安全，避免下发失败）。
    """
    if not speaker_for_api:
        return False
    if speaker_for_api.startswith("icl_") or speaker_for_api.startswith("S_"):
        return False
    for v in _BUILTIN_VOICES:
        if v["id"] == speaker_for_api:
            return bool(v.get("supports_emotion", False))
    return False


# 接口文档参考：豆包火山文档 v1/tts（中文合成）
class DoubaoTTSProvider(BaseTTSProvider):
    name = "doubao_tts"
    provider = "doubao"

    # 豆包 TTS 支持倍速范围 0.2 ~ 3.0（相对 speed_ratio）
    MIN_SPEED = 0.2
    MAX_SPEED = 3.0
    DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v1/tts"

    @property
    def _endpoint(self) -> str:
        """合成端点：优先读 doubao_field("tts_endpoint")（页面配置优先，回退 settings/env）。"""
        try:
            from backend.app.core.config import doubao_field
            return doubao_field("tts_endpoint") or self.DEFAULT_ENDPOINT
        except Exception:
            return self.DEFAULT_ENDPOINT

    # 重试参数（与 MiniMaxTTSProvider 对齐）
    MAX_RETRIES = 5
    BASE_BACKOFF_SECS = 0.6
    JITTER_SECS = 0.3

    # [P-fastfail] 5xx 走快失败策略 —— 常量定义在模块级 `_MAX_5XX_RETRIES`
    # / `_HTTP_5XX_BACKOFF_SECS`，v1 与 v3 共用，避免两处各写一份后分叉。
    MAX_5XX_RETRIES = _MAX_5XX_RETRIES
    HTTP_5XX_BACKOFF_SECS = _HTTP_5XX_BACKOFF_SECS

    # -----------------------------------------------------------------
    # 音色列表
    # -----------------------------------------------------------------
    def _builtin_voices_sync(self) -> list[dict[str, Any]]:
        """同步返回内置全部豆包音色（全量已填 provider=doubao + id 前缀 doubao:）。

        输出字段：
          - id          doubao:<官方真实 voice_type>
          - name        中文展示名
          - gender      female / male / neutral
          - age         child / teen / youth / middle / senior
          - scene       适用场景列表
          - dialect     方言 / ""=普通话
          - languages   支持语种列表（zh / en / ja / ptbr / esmx / id 等）
          - zh_tags     中文标签（前端筛选用）
          - supports_emotion / supports_subtitle / supports_language
                          是否支持 emotion / enable_subtitle / language 三个高级参数
                          （基于官方音色表逐条核对，见 _BUILTIN_VOICES 注释）
          - free        是否免费音色（火山 FAQ「21 款免费音色」白名单）
          - model       推荐使用的豆包模型（内置条目恒为 "seed-tts-2.0"）
          - provider    固定为 "doubao"
        """
        out: list[dict[str, Any]] = []
        for v in _BUILTIN_VOICES:
            item: dict[str, Any] = {
                "id": f"doubao:{v['id']}",
                "name": v["name"],
                "gender": v["gender"],
                "age": v["age"],
                "scene": list(v["scene"]),
                "dialect": v["dialect"],
                "languages": list(v.get("languages") or ["zh"]),
                "zh_tags": list(v["zh_tags"]),
                "supports_emotion": bool(v.get("supports_emotion", False)),
                "supports_subtitle": bool(v.get("supports_subtitle", True)),
                "supports_language": bool(v.get("supports_language", False)),
                "free": bool(v.get("free", False)),
                "model": v.get("model", "seed-tts-2.0"),
                "provider": "doubao",
            }
            out.append(item)
        return out

    async def list_voices(self) -> list[dict[str, Any]]:
        """合并三层音色（优先级 自定义 > 远程 > 内置）：
        1. 内置 `_BUILTIN_VOICES`（底）
        2. `DATA_DIR/voices_doubao_remote.json`（P2-1 启动时同步，写盘后只在重启再同步）
        3. `DATA_DIR/voices_doubao.json`（运营/用户自定义，顶）
        """
        voices_by_id: dict[str, dict[str, Any]] = {}
        for v in self._builtin_voices_sync():
            voices_by_id[v["id"]] = v

        # P2-1：远程音色（启动时由 doubao_list_speakers 同步写入磁盘）
        try:
            from backend.app.services.doubao_list_speakers import load_remote_voices
            for v in load_remote_voices():
                vid = str(v.get("id") or "").strip()
                if not vid:
                    continue
                if not vid.startswith("doubao:") and not vid.startswith("icl:"):
                    vid = f"doubao:{vid}"
                merged = {
                    "id": vid,
                    "name": v.get("name") or vid.split(":", 1)[-1],
                    "provider": "doubao",
                    "gender": v.get("gender", "neutral"),
                    "age": v.get("age", "youth"),
                    "scene": v.get("scene", ["远程"]),
                    "dialect": v.get("dialect", ""),
                    "languages": v.get("languages", ["zh"]),
                    "supports_emotion": bool(v.get("supports_emotion", False)),
                    "supports_subtitle": bool(v.get("supports_subtitle", True)),
                    "supports_language": bool(v.get("supports_language", False)),
                    "free": bool(v.get("free", False)),
                    "model": v.get("model", "seed-tts-2.0"),
                    "zh_tags": v.get("zh_tags", ["远程"]),
                }
                # 额外字段（如 description / avatar / categories）原样保留
                for k, val in v.items():
                    if k not in merged and k != "id":
                        merged[k] = val
                voices_by_id[vid] = merged
        except Exception:
            logger.exception("读取 voices_doubao_remote.json 失败，仅使用内置音色")

        # 自定义覆盖（用户/运营手工编辑的 json，最高优先级）
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
        """构造豆包 TTS v1 请求体（与 T-DV3 测试对齐）。

        关键：cluster 按 speaker 路由
          - 普通 TTS 音色（zh_xxx_uranus_bigtts 等）→ cluster=volcano_tts
          - 复刻音色（S_/icl_ 前缀）→ cluster=volcano_icl
        官方文档：https://www.volcengine.com/docs/6561/1305191
        """
        speaker_for_api = self._strip_voice_id_for_api(voice_id)
        speed_ratio = self._clamp_speed(speed)
        # 复刻音色必须用 volcano_icl cluster，否则官方按标准 TTS 路由找不到 speaker
        if speaker_for_api.startswith("S_") or speaker_for_api.startswith("icl_"):
            cluster = "volcano_icl"
        else:
            cluster = "volcano_tts"
        body: dict[str, Any] = {
            "text": text,                    # 平铺
            "voice_id": speaker_for_api,     # 平铺：便于 3rd-party 代理或测试校验
            "speaker_id": speaker_for_api,   # 兼容字段别名
            "speaker": speaker_for_api,      # 兼容字段别名
            "app": {
                # 豆包 app.userid 用于审计；取进程 ID 做一个固定但不敏感标识
                "appid": "",
                "token": "",
                "cluster": cluster,
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
        # P1-2：emotion 只在 extend_params.emotion 一处塞（之前在三处冗余，是 bug）。
        # 同时按音色元数据 supports_emotion 诊断：当前音色不支持 emotion 时降级并打 warning。
        ext = body.setdefault("extend_params", {})
        if emotion:
            if not _voice_supports_emotion(speaker_for_api):
                logger.warning(
                    f"[DoubaoTTS-v1] 音色 {speaker_for_api} 不支持 emotion，"
                    f"忽略 emotion={emotion!r}（降级为音色默认情绪）"
                )
            else:
                ext["emotion"] = emotion
        if speaker_style:
            ext["speaker_style"] = speaker_style
        if instruction_text:
            ext["instruction_text"] = instruction_text
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
                # 真正请求：返回 JSON 业务码 + logid（不再用 _http_post_bytes 那种「把响应原样当 MP3」的反模式）
                resp_json, logid = await _post_json_for_v1(
                    self._endpoint, headers, payload
                )
                code = int(resp_json.get("code", -1) or -1)
                msg = str(resp_json.get("message") or "")
                # 业务码 3000 才算成功；其余一律按业务错处理
                if code != 3000:
                    raise DoubaoTTSResponseError(
                        f"code={code} {msg}",
                        code=code,
                        logid=logid,
                    )
                data_b64 = resp_json.get("data") or ""
                if not data_b64:
                    raise DoubaoTTSResponseError(
                        "code=3000 但响应 data 字段为空",
                        code=code,
                        logid=logid,
                    )
                import base64 as _b64
                try:
                    mp3_bytes = _b64.b64decode(data_b64, validate=False)
                except Exception as e:
                    raise DoubaoTTSResponseError(
                        f"data 不是合法 base64：{type(e).__name__}: {e}",
                        code=code,
                        logid=logid,
                    ) from None
                dur_ms = _estimate_mp3_duration_ms(mp3_bytes)
                if logid:
                    logger.debug(f"[DoubaoTTS] 成功 logid={logid} dur_ms={dur_ms}")
                return mp3_bytes, dur_ms
            except DoubaoTTSResponseError as e:
                last_exc = e
                # 业务码可重试：3001/3002/3003/3010/3011；其余直接放弃
                is_retryable = e.is_retryable
                if attempt >= self.MAX_RETRIES or not is_retryable:
                    logger.warning(
                        f"[DoubaoTTS] 放弃重试（attempt={attempt}/{self.MAX_RETRIES}）："
                        f"业务码 code={e.code} logid={e.logid} msg={e}"
                    )
                    break
                wait_s = self.BASE_BACKOFF_SECS * (2 ** (attempt - 1)) + self.JITTER_SECS
                logger.warning(
                    f"[DoubaoTTS] 重试（attempt={attempt}/{self.MAX_RETRIES}）："
                    f"业务码 code={e.code} logid={e.logid} 指数退避 {wait_s:.1f}s"
                )
                await asyncio.sleep(wait_s)
            except Exception as e:
                # 真网络错 / JSON 解析错 / base64 解码错（-1）/ httpx 异常
                last_exc = e
                resp_obj = getattr(e, "response", None)
                # 透传 logid（P1-6）：从异常对象的 response.headers 取 X-Tt-Logid，
                # 让 routes.py 能在 5xx 响应里附给前端，方便定位问题
                try:
                    if resp_obj is not None and getattr(last_exc, "logid", None) is None:
                        logid_hdr = (
                            resp_obj.headers.get("X-Tt-Logid")
                            or resp_obj.headers.get("x-tt-logid")
                        )
                        if logid_hdr:
                            setattr(last_exc, "logid", logid_hdr)
                except Exception:
                    pass
                # 解析 Retry-After（若 429 提供）
                retry_after = None
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
                is_server = (
                    resp_obj is not None and 500 <= int(getattr(resp_obj, "status_code", 0)) < 600
                )
                # 网络层可重试；JSON 解析失败（code=-1）不重试
                code = getattr(e, "code", None)
                if code == -1:
                    is_retryable = False
                elif is_server:
                    # 5xx 走 fastfail：最多 MAX_5XX_RETRIES 次（默认 1）。
                    # 上游服务问题，5 次重试毫无意义，反而拖到 13s+ 让用户以为卡死。
                    is_retryable = attempt < self.MAX_5XX_RETRIES
                else:
                    is_retryable = is_rate or is_server or resp_obj is None
                if attempt >= self.MAX_RETRIES or not is_retryable:
                    logger.warning(
                        f"[DoubaoTTS] 放弃重试（attempt={attempt}/{self.MAX_RETRIES}）："
                        f"{type(e).__name__}: {e}"
                    )
                    break
                if retry_after is not None:
                    wait_s = retry_after + self.JITTER_SECS
                elif is_server:
                    # 5xx 退避（默认 0.3s），不再指数
                    wait_s = self.HTTP_5XX_BACKOFF_SECS
                else:
                    wait_s = self.BASE_BACKOFF_SECS * (2 ** (attempt - 1)) + self.JITTER_SECS
                logger.warning(
                    f"[DoubaoTTS] 重试（attempt={attempt}/{self.MAX_RETRIES}）："
                    f"退避 {wait_s:.1f}s {type(e).__name__}: {e}"
                )
                await asyncio.sleep(wait_s)
        assert last_exc is not None
        # P1-6：final 错也带 logid（如能从 last_exc 继承），便于 routes.py 透传
        logid_final = getattr(last_exc, "logid", None)
        msg = f"豆包 TTS 合成失败：{type(last_exc).__name__}: {last_exc}"
        if logid_final:
            msg = f"{msg} logid={logid_final}"
        err = RuntimeError(msg)
        try:
            err.logid = logid_final  # type: ignore[attr-defined]
        except Exception:
            pass
        raise err from last_exc

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
        """取豆包访问令牌。

        优先级（与 MiniMax 厂商结构对齐）：
          1) PROVIDERS_CONFIG 中 id="doubao" 厂商的 api_key
             （设置页「火山引擎豆包语音」启用后填入并保存，会持久化到这里）
          2) settings.DOUBAO_AK（.env 扁平字段，兼容老部署）
          3) 进程环境变量 MEGACORE_ACCESS_KEY_FROM_ENV（容器/部署平台注入）
        任一来源非空都视为有效凭据；返回时按 "是否含空格" 判断走 Bearer 分号风格。
        """
        # 1) 多厂商配置（页面配置）
        try:
            from ....core.config import doubao_field
            ak = doubao_field("api_key") or ""
        except Exception:
            ak = ""
        if ak:
            if " " in ak:
                return ak.strip()
            return f"Bearer;{ak}"
        # 2) doubao_field 已 fallback settings.DOUBAO_AK；如果还为空，走进程 ENV 兜底
        fallback = os.environ.get("MEGACORE_ACCESS_KEY_FROM_ENV") or ""
        if fallback:
            if " " in fallback:
                return fallback.strip()
            return f"Bearer;{fallback}"
        raise RuntimeError(
            "未配置豆包凭据：请在「设置 → 模型厂商 → 火山引擎豆包语音」"
            "启用并填入 API Key 后保存，或设置 DOUBAO_AK 环境变量。"
        )

    # 旧的 _http_post_bytes 已删除：它的实现把响应原样当 MP3 写盘（HTTP 200 但
    # 业务码非 3000 时会写入 JSON 伪装 .mp3）。当前路径走 _post_json_for_v1 +
    # DoubaoTTSResponseError 做业务码判定与重试。


# =====================================================================
# 豆包 v3 单向流式 HTTP TTS Provider（P1-1）
#
# 协议：https://openspeech.bytedance.com/api/v3/tts/unidirectional
#       （HTTP Chunked 单向流式）
#
# 鉴权（新版控制台推荐）：
#   X-Api-Key: <API Key>
#   X-Api-Resource-Id: seed-tts-2.0  # 2.0 音色；声音复刻走 seed-icl-2.0
#   X-Api-App-Key: aGjiRDfUWi        # 固定值（官方要求）
#   X-Api-Request-Id: <uuid>
#
# 鉴权（旧版控制台兼容）：
#   X-Api-App-Id: <APP ID>           # 纯数字
#   X-Api-Access-Key: <Access Token>
#   X-Api-Resource-Id: seed-tts-2.0
#
# 请求体（v3 嵌套结构）：
#   {
#     "user": {"uid": "..."},
#     "req_params": {
#       "text": "...",
#       "speaker": "zh_female_vv_uranus_bigtts",  # 2.0 音色，或 ICL speaker_id
#       "audio_params": {
#         "format": "mp3",
#         "sample_rate": 24000,
#         "speech_rate": 0,            # -50 ~ 100，0=原速
#         "loudness_rate": 0,          # -50 ~ 100
#         "enable_subtitle": false,
#         "disable_markdown_filter": true,
#         # emotion / instruction_text / speaker_style 按需打开（GLM 警告：
#         # 字段名需真实 Key 验证，这里留接口 + 默认不传，避免静默错）
#       },
#     },
#   }
#
# 响应：HTTP Chunked 流式 JSON 序列（一行一个 JSON）。成功 chunk 形如
#       {"code": 0 或 20000000, "message": "OK", "data": "<base64 音频分片>",
#        "sentence": {...}, "usage": {...}}（见 _V3_SUCCESS_CODES 注释）；
#       失败 chunk 形如 {"code": <非成功码>, "message": "..."}。
#       客户端把所有 chunk 的 data 字段解码后拼接得到 MP3。
#
# 与 v1 共存策略：
#   - v3 为默认路径：settings.DOUBAO_TTS_USE_V3 默认 True（2026-09-18 起）
#   - v1 (DoubaoTTSProvider, endpoints /api/v1/tts) 仅作显式兜底：把开关手动关掉才走
#     旧默认 False 的原因已不成立 —— v1 的 _build_payload 里 app.appid/app.token 恒为空，
#     新版控制台单一 API Key 走 v1 必然 500（实测 BV158_streaming）
# =====================================================================

_DEFAULT_V3_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
# 固定值（官方协议要求；新鉴权方式也必须带上）
_V3_FIXED_X_API_APP_KEY = "aGjiRDfUWi"

# v3 业务码：成功码见 _V3_SUCCESS_CODES；非成功码一律抛错（不静默错）。
# 具体可重试判定不在 v3 spec 中明确，这里保守：网络层 + 5xx 重试，业务码直接抛错。
_V3_RETRYABLE_NETWORK = True  # 网络错 / 5xx / 429 重试

# ⚠️ 成功码不止 0：文档《单向流式语音合成HTTP》响应示例写的是 `"code": 0`，
# 并注明「message 返回 OK 则表示合成成功」，但**实测返回的是 `code=20000000,
# message=OK`**（2026-09-20，音色 zh_female_vv_uranus_bigtts，logid
# 2026092013535759634A20BCF37CCA033B，HTTP 200、耗时 2s、音频正常）。
# 20000000 不在官方错误码表（6561/2534853）里，属该接口的 OK 码。
# 旧实现只认 0，于是把成功响应当成业务错——并且因为错误分支会 `continue`，
# 顺带把同一条 chunk 里的音频也跳过了，两个 bug 互相掩盖。
_V3_SUCCESS_CODES: frozenset[int] = frozenset({0, 20000000})


def _v3_chunk_is_success(obj: dict) -> bool:
    """判断一个流式 chunk 是否为成功响应。

    文档《单向流式语音合成HTTP》对成功条件有两处表述：「`code` 返回 0 则表示
    语音合成成功」与「`message` 返回 OK 则表示语音合成成功」；实测又出现
    `code=20000000` + `message=OK`（见 `_V3_SUCCESS_CODES`）。因此**以 message
    为准、code 白名单为辅**，任一命中即视为成功 —— 我们已经在「成功码只有 0」
    这个假设上错过一次，不要再赌第二个码。

    误判为成功的代价可控：真正的失败响应拿到最后也是**没有音频**（没有 `data`
    字段，或 `data` 为空 —— 后者实测存在，见 `_v3_empty_audio_hint`），随后会命中
    「无音频分片」检查并抛出带字段名 / 形态线索的错误，不会静默产出坏音频。
    """
    if str(obj.get("message") or "").strip().upper() == "OK":
        return True
    code_val = obj.get("code")
    if code_val is None:
        # 既没有 message 也没有 code：无法判定为失败，交给后续「无音频分片」兜住
        return True
    try:
        return int(code_val) in _V3_SUCCESS_CODES
    except (TypeError, ValueError):
        return False


def _text_is_mostly_cjk(text: str) -> bool:
    """文本是否以中文（CJK 表意字）为主 —— 用于空音频的归因提示。"""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    cjk = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff")
    return cjk * 2 >= len(chars)


def _v3_empty_audio_hint(payload: dict[str, Any]) -> str:
    """「成功码但没合成出音频」的归因提示。

    实测（2026-09-20，音色试听）：把**中文文本**发给**纯外语音色**（Stokie，
    `en_female_stokie_uranus_bigtts`，音色表声明语种 `["en"]`）时，上游返回
    `code=20000000 / message=OK` 且 `data` 为空 —— 既不是 HTTP 失败也不是业务
    错误码，只看「无音频分片」根本定位不到，所以这里把「音色声明的语种」与
    「文本语种」一起带出来。音色表里中文音色注明「亦具备英文能力」，英文音色
    没有反向声明，跨语种并不通用。
    """
    req = payload.get("req_params") if isinstance(payload, dict) else None
    if not isinstance(req, dict):
        return ""
    text = str(req.get("text") or "")
    speaker = _strip_voice_id_for_api_v3(str(req.get("speaker") or ""))
    if not text or not speaker or not _text_is_mostly_cjk(text):
        return ""
    for v in _BUILTIN_VOICES:
        if v["id"] != speaker:
            continue
        langs = [str(x).lower() for x in (v.get("languages") or [])]
        if langs and "zh" not in langs:
            return (
                f"；疑似语种不匹配：文本以中文为主，而音色 {speaker} 声明的语种是 "
                f"{langs}，纯外语音色读中文时上游会返回「成功但无音频」。"
                "请改用支持中文的音色，或把文本换成该音色对应的语种"
            )
        return ""
    return ""


class DoubaoTTSResponseV3Error(RuntimeError):
    """v3 协议错误：HTTP 失败 + 业务码非 0 都会抛此。"""

    def __init__(
        self,
        message: str,
        *,
        code: int = -1,
        logid: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.logid = logid


def _strip_voice_id_for_api_v3(voice_id: str) -> str:
    """与 v1 同语义：剥 `doubao:` / `icl:` 前缀得到真实 speaker_id。"""
    if voice_id.startswith("icl:"):
        return voice_id.split(":", 1)[1]
    if voice_id.startswith("doubao:"):
        return voice_id.split(":", 1)[1]
    return voice_id


def _resolve_resource_id_for_v3(model: str) -> str:
    """按音色表 `model` 字段映射到 X-Api-Resource-Id。

    已知：
      - seed-tts-1.0 / seed-tts-1.0-concurr → 1.0 音色
      - seed-tts-2.0 → 2.0 音色
      - seed-icl-1.0 / seed-icl-2.0 → ICL 声音复刻
    """
    m = (model or "").strip().lower()
    if m in ("seed-tts-2.0",):
        return "seed-tts-2.0"
    # 显式声明 1.0 的入参（远程同步 / 用户自定义音色可能仍带）如实映射；
    # 调用方再按 is_voice_usable_on_v3 把它过滤掉，不要在这里悄悄改写成 2.0。
    if m in ("seed-tts-1.0", "seed-tts-1.0-concurr"):
        return "seed-tts-1.0"
    if m in ("seed-icl-2.0",):
        return "seed-icl-2.0"
    if m in ("seed-icl-1.0", "seed-icl-1.0-concurr"):
        return "seed-icl-1.0"
    # 未知值兜底 2.0（1.0 已整表下线，账号也只有 2.0 资源）
    return "seed-tts-2.0"


# v3 端点（DOUBAO_TTS_USE_V3=True，当前默认路径）官方只支持这两类合成资源：
#   seed-tts-2.0 → *_uranus_bigtts 等 2.0 官方音色
#   seed-icl-2.0 → 声音复刻音色
# 声明 model=seed-tts-1.0 的音色（远程同步 / 用户自定义表里可能仍有 BV* 小模型）
# 在 v3 上会被要求 1.0 资源（服务端别名 volc.service_type.10029），账号未开通时
# 直接 403 + code=45000030 "requested resource not granted"（实测 2026-09-20
# 音色 BV158_streaming），因此这类音色要在这里被过滤掉；内置表已不含 1.0。
_V3_SUPPORTED_MODELS: frozenset[str] = frozenset({"seed-tts-2.0", "seed-icl-2.0"})


def is_voice_usable_on_v3(voice: dict[str, Any]) -> bool:
    """判断音色是否落在 v3 端点支持的资源上（供音色推荐筛选用）。

    只对 `provider="doubao"` 生效；`icl:` 前缀与缺失 `model` 元数据的一律放行
    —— 宁可不筛，也不要误伤复刻音色和用户自定义音色。
    """
    if (voice.get("provider") or "") != "doubao":
        return True
    if str(voice.get("id") or "").startswith("icl:"):
        return True
    model = str(voice.get("model") or "").strip().lower()
    if not model:
        return True
    return model in _V3_SUPPORTED_MODELS


def _v3_http_error_hint(status_code: int, body: str) -> str:
    """把网关级 4xx 的裸响应翻译成可操作提示。

    这些码不在官方错误码表里（表里只覆盖 HTTP 200 + code 非 0 的业务错），
    实测 403 会回 `{"header":{"code":45000030,"message":"[resource_id=...]
    requested resource not granted"}}` —— 意思是「该资源账号没开通」，
    跟音色/文本都无关。不翻译的话，用户只能看到一句 403 Forbidden。
    """
    if "requested resource not granted" in body:
        return (
            "→ 该 X-Api-Resource-Id 对应的资源账号未开通（seed-tts-1.0 即 "
            "volc.service_type.10029）。请到火山引擎控制台开通对应资源，"
            "或改用 2.0 音色（*_uranus_bigtts，对应 seed-tts-2.0）"
        )
    if status_code in (401, 403):
        return (
            "→ 鉴权/资源未授权：确认 API Key 属于该项目，且 "
            "X-Api-Resource-Id 已在控制台开通"
        )
    return ""


class DoubaoTTSProviderV3(BaseTTSProvider):
    """豆包 TTS v3 单向流式 HTTP Provider。

    与 DoubaoTTSProvider (v1) 共享音色表 _BUILTIN_VOICES；
    接口规范按官方 1598757 文档（V3 HTTP Chunked）。
    """

    name = "doubao_tts_v3"
    provider = "doubao"

    MIN_SPEED = 0.5
    MAX_SPEED = 2.0
    MAX_RETRIES = 5
    BASE_BACKOFF_SECS = 0.6
    JITTER_SECS = 0.3
    DEFAULT_SAMPLE_RATE = 24000

    # [P-fastfail] 5xx 快失败：与 v1 共用模块级常量。
    # 缺陷修复：这两个常量原先只在 v1 类上定义，v3 的重试分支却直接引用
    # self.MAX_5XX_RETRIES / self.HTTP_5XX_BACKOFF_SECS →
    # 开启 DOUBAO_TTS_USE_V3 且上游返回 5xx 时必然抛 AttributeError。
    MAX_5XX_RETRIES = _MAX_5XX_RETRIES
    HTTP_5XX_BACKOFF_SECS = _HTTP_5XX_BACKOFF_SECS

    @property
    def _endpoint(self) -> str:
        try:
            from ....core.config import doubao_field
            return doubao_field("tts_v3_endpoint") or _DEFAULT_V3_ENDPOINT
        except Exception:
            return _DEFAULT_V3_ENDPOINT

    # -----------------------------------------------------------------
    # 鉴权
    # -----------------------------------------------------------------
    def _resolve_api_key(self) -> str:
        """解析 v3 API Key（优先级与 v1 _get_authorization 一致）。"""
        try:
            from ....core.config import doubao_field
            ak = doubao_field("api_key") or ""
        except Exception:
            ak = ""
        if not ak:
            ak = os.environ.get("MEGACORE_ACCESS_KEY_FROM_ENV") or ""
        if not ak:
            raise RuntimeError(
                "未配置豆包凭据：请在「设置 → 模型厂商 → 火山引擎豆包语音」"
                "启用并填入 API Key 后保存，或设置 DOUBAO_AK 环境变量。"
            )
        return ak.strip().split()[-1] if " " in ak else ak.strip()

    def _auth_headers(self, *, speaker_id: str) -> dict[str, str]:
        """构造 v3 鉴权头。新版 X-Api-Key；旧版（key 为纯数字）走 X-Api-App-Key +
        X-Api-Access-Key。同时必须带 X-Api-Resource-Id + 固定的 X-Api-App-Key。"""
        ak = self._resolve_api_key()
        ak_value = ak.strip()
        resource_id = _resolve_resource_id_for_v3(
            self._resolve_model_for_speaker(speaker_id)
        )
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Api-App-Key": _V3_FIXED_X_API_APP_KEY,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": f"novel-{uuid.uuid4().hex}",
        }
        if ak_value.lstrip("-").isdigit():
            headers["X-Api-App-Id"] = ak_value
            # 旧版 Access Token：与 api_key 同 值（兼容历史"只填一个"的部署）
            headers["X-Api-Access-Key"] = ak_value
        else:
            headers["X-Api-Key"] = ak_value
        return headers

    def _resolve_model_for_speaker(self, speaker_id: str) -> str:
        """根据 speaker_id 查 _BUILTIN_VOICES 返回 model；找不到则兜底 seed-tts-2.0。"""
        # icl: 开头按 ICL 2.0 走
        if speaker_id.startswith("S_") or speaker_id.startswith("icl_"):
            return "seed-icl-2.0"
        for v in _BUILTIN_VOICES:
            if v["id"] == speaker_id:
                return v.get("model") or "seed-tts-2.0"
        # 兜底 2.0：1.0 小模型音色已下线，未知 speaker 只可能是外部自定义，
        # 按账号已开通的 2.0 资源请求更不容易直接 403。
        return "seed-tts-2.0"

    # -----------------------------------------------------------------
    # 音色列表：复用 v1 内置音色（保证前后端列表稳定）
    # -----------------------------------------------------------------
    async def list_voices(self) -> list[dict[str, Any]]:
        """三层合并：内置 < 远程 < 自定义。P2-1。"""
        voices_by_id: dict[str, dict[str, Any]] = {}
        for v in _BUILTIN_VOICES:
            voices_by_id[f"doubao:{v['id']}"] = {
                "id": f"doubao:{v['id']}",
                "name": v["name"],
                "provider": "doubao",
                "gender": v["gender"],
                "age": v["age"],
                "scene": list(v["scene"]),
                "dialect": v["dialect"],
                "languages": list(v.get("languages") or ["zh"]),
                "zh_tags": list(v["zh_tags"]),
                "supports_emotion": bool(v.get("supports_emotion", False)),
                "supports_subtitle": bool(v.get("supports_subtitle", True)),
                "supports_language": bool(v.get("supports_language", False)),
                "free": bool(v.get("free", False)),
                "model": v.get("model", "seed-tts-2.0"),
                "protocol": "v3",  # 标记当前 provider 走 v3
            }
        # P2-1：远程音色（启动时由 doubao_list_speakers 同步写入磁盘）
        try:
            from backend.app.services.doubao_list_speakers import load_remote_voices
            for v in load_remote_voices():
                vid = str(v.get("id") or "").strip()
                if not vid:
                    continue
                if not vid.startswith("doubao:") and not vid.startswith("icl:"):
                    vid = f"doubao:{vid}"
                merged: dict[str, Any] = {
                    "id": vid,
                    "name": v.get("name") or vid.split(":", 1)[-1],
                    "provider": "doubao",
                    "gender": v.get("gender", "neutral"),
                    "age": v.get("age", "youth"),
                    "scene": v.get("scene", ["远程"]),
                    "dialect": v.get("dialect", ""),
                    "languages": list(v.get("languages") or ["zh"]),
                    "zh_tags": list(v.get("zh_tags") or ["远程"]),
                    "supports_emotion": bool(v.get("supports_emotion", False)),
                    "supports_subtitle": bool(v.get("supports_subtitle", True)),
                    "supports_language": bool(v.get("supports_language", False)),
                    "free": bool(v.get("free", False)),
                    "model": v.get("model", "seed-tts-2.0"),
                    "protocol": "v3",
                }
                for k, val in v.items():
                    if k not in merged and k != "id":
                        merged[k] = val
                voices_by_id[vid] = merged
        except Exception:
            logger.exception("v3 list_voices: 读取 voices_doubao_remote.json 失败")
        # 用户自定义 voices_doubao.json（与 v1 同样路径）
        try:
            custom_path = Path(settings.DATA_DIR) / "voices_doubao.json"
            if custom_path.exists():
                raw = json.loads(custom_path.read_text(encoding="utf-8"))
                for v in raw:
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
                        "protocol": "v3",
                    }
                    for k, val in v.items():
                        if k not in merged and k != "id":
                            merged[k] = val
                    voices_by_id[vid] = merged
        except Exception:
            logger.exception("v3 list_voices: 读取 voices_doubao.json 失败")
        return list(voices_by_id.values())

    # -----------------------------------------------------------------
    # Payload 构造
    # -----------------------------------------------------------------
    def _build_v3_payload(
        self,
        text: str,
        voice_id: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
        instruction_text: str | None = None,
        speaker_style: str | None = None,
    ) -> dict[str, Any]:
        speaker_for_api = _strip_voice_id_for_api_v3(voice_id)
        # speech_rate: -50~100，0=原速；speed 1.0 → 0；2.0 → +100；0.5 → -50
        speech_rate = self._map_speed_to_speech_rate(speed)
        audio_params: dict[str, Any] = {
            "format": "mp3",
            # P1-4：sample_rate/loudness_rate 从 settings 注入，便于运维统一调音
            "sample_rate": int(getattr(settings, "DOUBAO_AUDIO_SAMPLE_RATE", 24000) or 24000),
            "speech_rate": speech_rate,
            "loudness_rate": int(getattr(settings, "DOUBAO_AUDIO_LOUDNESS_RATE", 0) or 0),
            "disable_markdown_filter": True,
            "enable_subtitle": False,
        }
        # GLM 警告：emotion / instruction_text 字段名需真实 Key 验证；这里按
        # 官方 v3 文档暂放 audio_params.emotion；可后续按联调结果调整
        # （P1-1 范围：保守骨架，验证完毕后再真实落地情绪链路）
        # P1-2：emotion 链路落地 — 音色元数据 supports_emotion=False 时降级打 warning，不下发。
        if emotion and emotion not in ("calm", "neutral", ""):
            if not _voice_supports_emotion(speaker_for_api):
                logger.warning(
                    f"[DoubaoTTS-v3] 音色 {speaker_for_api} 不支持 emotion，"
                    f"忽略 emotion={emotion!r}（降级为音色默认情绪）"
                )
            else:
                audio_params["emotion"] = emotion
        # P1-5：instruction_text 按官方 v3 文档放入 req_params.context_texts；
        # 复刻音色（speaker 以 icl_/S_ 开头）忽略并 warning。
        is_clone_speaker = (
            speaker_for_api.startswith("icl_")
            or speaker_for_api.startswith("S_")
        )
        payload_extras: dict[str, Any] = {}
        if speaker_style:
            payload_extras["speaker_style"] = speaker_style
        # ⚠️ model 是「语音指令 / 语音标签」的总开关（2026-09-20 用户反馈「指令不生效」的根因）：
        # 官方《模型列表》写明 2.0 分两版 —— seed-tts-2.0-standard（接口默认）**不支持
        # 语音指令 QA 和语音标签 CoT**，只有 seed-tts-2.0-expressive 支持。本项目此前
        # 从不传 model，于是指令发出去也被上游**静默忽略**，听感与不加指令完全一样。
        # 复刻音色不下发：官方 HTTP 文档写明「model 仅当 speaker 为复刻音色时需指定，
        # 且指定后不支持 context_texts」——复刻场景保住 context_texts 更有价值。
        tts_model = str(getattr(settings, "DOUBAO_TTS_MODEL", "") or "").strip()
        if tts_model and not is_clone_speaker:
            payload_extras["model"] = tts_model
        if instruction_text and not is_clone_speaker:
            payload_extras["context_texts"] = [str(instruction_text)]
        elif instruction_text:
            logger.warning(
                f"[DoubaoTTS-v3] 复刻音色 {speaker_for_api} 不支持 instruction_text，已忽略"
            )
        return {
            "user": {"uid": f"local-{os.getpid() % 10000:04d}"},
            "req_params": {
                "text": text,
                "speaker": speaker_for_api,
                "audio_params": audio_params,
                **payload_extras,
            },
        }

    def _map_speed_to_speech_rate(self, speed: float) -> int:
        """speed [0.5, 2.0] → speech_rate [-50, 100] 线性映射；clamp 兜底。"""
        v = float(speed or 1.0)
        if math.isnan(v) or v <= 0:
            v = 1.0
        v = max(self.MIN_SPEED, min(self.MAX_SPEED, v))
        # 0.5 → -50；1.0 → 0；2.0 → 100
        # 公式：rate = (v - 1.0) * 100 / 1.0 → 但要 clamp 到 [-50, 100]
        rate = int(round((v - 1.0) * 100.0))
        return max(-50, min(100, rate))

    # -----------------------------------------------------------------
    # HTTP 流式响应解析
    # -----------------------------------------------------------------
    async def _post_stream_v3(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        timeout_read_s: float = 60.0,
    ) -> bytes:
        """POST 到 v3 HTTP Chunked 端点；按行解析流式 JSON，拼接 audio 字段。

        Returns:
            拼接后的 MP3 字节。

        Raises:
            DoubaoTTSResponseV3Error: 任意 chunk 含业务码非 0 或 HTTP 失败。
            httpx.HTTPError: 网络层错误（让上层走重试）。
        """
        import httpx

        timeout = httpx.Timeout(connect=10.0, read=timeout_read_s, write=10.0, pool=10.0)
        # 用 httpx 流式读取，逐行解析
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                logid = resp.headers.get("X-Tt-Logid") or resp.headers.get("x-tt-logid")
                if resp.status_code >= 400:
                    # 非 2xx 必须先把 body 读出来：上游会把真正原因放在 body 里
                    # （形如 {"code":45000001,"message":"[Invalid argument] speaker not found"}），
                    # 而 `raise_for_status()` 只带状态码 —— 光看 "403 Forbidden" 无法区分
                    # 「Key 没有该资源权限」和「X-Api-Resource-Id 与音色不匹配」，
                    # 排查时会完全卡住（旧实现在 4xx 直接 raise，body 被丢弃）。
                    raw = await resp.aread()
                    body = raw.decode("utf-8", "replace").strip()
                    message = f"HTTP {resp.status_code}: {body[:500] or '(empty body)'}"
                    hint = _v3_http_error_hint(resp.status_code, body)
                    if hint:
                        message = f"{message} {hint}"
                    raise httpx.HTTPStatusError(
                        message,
                        request=getattr(resp, "request", None),
                        response=resp,
                    )
                resp.raise_for_status()
                chunks: list[bytes] = []
                # 诊断用：记下实际收到过哪些字段名。协议字段名一旦猜错，
                # 「无音频分片」这种报错本身不提供任何线索，只能靠这里。
                seen_keys: list[str] = []
                # 诊断用：data/audio 字段的形态，以及首个 base64 解码失败原因。
                # 必须能区分三种情况 —— ①没有 data/audio 字段 ②有但为空
                # ③有内容但解不出音频。旧报错把三者混说成「既无 data 也无 audio
                # 字段」，于是「字段明明有 data」的日志看起来自相矛盾
                # （2026-09-20 英文音色试听即此形态）。
                data_shape: str | None = None
                decode_err = ""
                json_lines = 0
                async for line in resp.aiter_lines():
                    line = (line or "").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        # 非 JSON 行（chunked 编码空行）忽略
                        continue
                    if not isinstance(obj, dict):
                        continue
                    json_lines += 1
                    for k in obj:
                        if k not in seen_keys and len(seen_keys) < 12:
                            seen_keys.append(k)
                    if data_shape is None and ("data" in obj or "audio" in obj):
                        # 直接取原值，不要 `or` 串：`data: ""` 会被 `or` 吃掉，
                        # 形态就变成 NoneType，"存在但为空"与"字段不存在"又混了
                        val = obj["data"] if "data" in obj else obj["audio"]
                        size = (
                            len(val) if isinstance(val, (str, bytes, list, dict))
                            else "n/a"
                        )
                        data_shape = f"type={type(val).__name__} len={size}"
                    # 失败 chunk：成功判定见 _v3_chunk_is_success（message=OK 或
                    # code 命中白名单；0 与 20000000 都表示 OK）
                    if not _v3_chunk_is_success(obj):
                        # 上游返回失败 chunk：**即使已经收到若干音频分片也必须抛错**。
                        # 旧实现是「只在 chunks 为空时才抛」，于是「先收到 N 个音频分片、
                        # 中途才报错」会把**被截断的 MP3** 当成功返回 —— 上层据此写盘、
                        # 记账、算时长，用户听到半句且字幕/进度全部错位（静默数据错误）。
                        err_code = int(obj.get("code") or -1)
                        err_msg = str(obj.get("message") or "")
                        if chunks:
                            err_msg = f"{err_msg}（已收到 {len(chunks)} 个音频分片，仍判定失败）"
                        raise DoubaoTTSResponseV3Error(
                            f"v3 TTS 业务错：code={err_code} msg={err_msg}",
                            code=err_code,
                            logid=logid,
                        )
                    # 音频字段：官方文档《单向流式语音合成HTTP》响应示例为 `data`
                    # （"data": "<base64 encoded audio chunk>"）。`audio` 是历史实现
                    # 猜错的名字，保留兜底以兼容可能存在的其它版本。
                    audio_b64 = obj.get("data") or obj.get("audio") or ""
                    if audio_b64:
                        try:
                            chunks.append(base64.b64decode(audio_b64, validate=False))
                        except Exception as e:
                            # 单段解码失败不致命，继续收后续 chunk；但要留痕，
                            # 「字段有值但解不出音频」与「字段本身就是空的」在日志里
                            # 必须能分开看
                            if not decode_err:
                                decode_err = type(e).__name__
                            continue
                if not chunks:
                    raise DoubaoTTSResponseV3Error(
                        "v3 TTS 响应无音频分片（成功码但音频为空）："
                        f"JSON 行数={json_lines} 字段={seen_keys or '(无 JSON 行)'} "
                        f"data 形态={data_shape or '(未出现 data/audio 字段)'}"
                        + (f" 解码失败={decode_err}" if decode_err else "")
                        + _v3_empty_audio_hint(payload),
                        code=-1,
                        logid=logid,
                    )
                return b"".join(chunks)

    # -----------------------------------------------------------------
    # 合成入口
    # -----------------------------------------------------------------
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
        if not text or not text.strip():
            empty = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 48)
            return empty, 0
        payload = self._build_v3_payload(
            text, voice_id, emotion=emotion, speed=speed,
            instruction_text=instruction_text, speaker_style=speaker_style,
        )
        speaker_for_api = _strip_voice_id_for_api_v3(voice_id)
        headers = self._auth_headers(speaker_id=speaker_for_api)
        # 诊断用：401/403 这类网关级拒绝（Key 无该资源权限、resource id 与音色
        # 不匹配、服务未开通）光看状态码无法定位，必须把「实际发出的音色 + 实际
        # 发出的 X-Api-Resource-Id」一起带进日志和异常消息。
        resource_id = headers.get("X-Api-Resource-Id", "")

        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                await _doubao_rpm_wait_acquire("tts")
                mp3_bytes = await self._post_stream_v3(
                    self._endpoint, headers, payload,
                )
                dur_ms = _estimate_mp3_duration_ms(mp3_bytes)
                logger.debug(
                    f"[DoubaoTTS-v3] 成功 text_len={len(text)} dur_ms={dur_ms}"
                )
                return mp3_bytes, dur_ms
            except DoubaoTTSResponseV3Error as e:
                # 业务错：不重试
                logger.warning(
                    f"[DoubaoTTS-v3] 业务错（放弃重试）：code={e.code} logid={e.logid} {e}"
                )
                raise
            except Exception as e:
                last_exc = e
                # P1-6：透传 logid（httpx.HTTPStatusError 带有 response.headers）
                resp_obj = getattr(e, "response", None)
                try:
                    if resp_obj is not None and getattr(last_exc, "logid", None) is None:
                        logid_hdr = (
                            resp_obj.headers.get("X-Tt-Logid")
                            or resp_obj.headers.get("x-tt-logid")
                        )
                        if logid_hdr:
                            setattr(last_exc, "logid", logid_hdr)
                except Exception:
                    pass
                # 网络层：可重试
                resp_obj_status = (
                    int(getattr(resp_obj, "status_code", 0))
                    if resp_obj is not None
                    else 0
                )
                is_server_5xx = 500 <= resp_obj_status < 600
                is_429 = resp_obj_status == 429
                if not _V3_RETRYABLE_NETWORK or isinstance(e, DoubaoTTSResponseV3Error):
                    is_retryable = False
                elif is_server_5xx:
                    # [P-fastfail] 5xx → 最多 MAX_5XX_RETRIES 次（默认 1 次重试）。
                    is_retryable = attempt < self.MAX_5XX_RETRIES
                else:
                    # 429 / 网络层（无 resp_obj）：走 MAX_RETRIES。
                    is_retryable = is_429 or resp_obj is None
                if attempt >= self.MAX_RETRIES or not is_retryable:
                    logger.warning(
                        f"[DoubaoTTS-v3] 放弃重试（attempt={attempt}/{self.MAX_RETRIES}）："
                        f"speaker={speaker_for_api} resource_id={resource_id} "
                        f"{type(e).__name__}: {e}"
                    )
                    break
                if is_server_5xx:
                    wait_s = self.HTTP_5XX_BACKOFF_SECS
                else:
                    wait_s = self.BASE_BACKOFF_SECS * (2 ** (attempt - 1)) + self.JITTER_SECS
                logger.warning(
                    f"[DoubaoTTS-v3] 重试（attempt={attempt}/{self.MAX_RETRIES}）："
                    f"退避 {wait_s:.1f}s {type(e).__name__}: {e}"
                )
                await asyncio.sleep(wait_s)
        assert last_exc is not None
        # P1-6：final 错也带 logid（如能从 last_exc 继承）
        logid_final = getattr(last_exc, "logid", None)
        msg = (
            f"豆包 TTS v3 合成失败：speaker={speaker_for_api} "
            f"X-Api-Resource-Id={resource_id}：{type(last_exc).__name__}: {last_exc}"
        )
        if logid_final:
            msg = f"{msg} logid={logid_final}"
        err = RuntimeError(msg)
        try:
            err.logid = logid_final  # type: ignore[attr-defined]
        except Exception:
            pass
        raise err from last_exc

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
        data, dur_ms = await self.synthesize_to_bytes(
            text, voice_id,
            emotion=emotion, speed=speed,
            instruction_text=instruction_text, speaker_style=speaker_style,
        )
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
