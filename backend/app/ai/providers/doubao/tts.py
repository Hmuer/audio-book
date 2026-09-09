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
        resp.raise_for_status()
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
# 豆包官方内置音色清单（BV 系列 + 旧 zh_* 系列）。
# 数据来源：火山引擎语音合成官方音色库（T-DV1a / T-DV-Cloud 流式接口）。
# 覆盖：
#   - 通用 / 情感 / 角色配音 / 有声书 / 新闻播报
#   - 方言：粤语、四川话、东北话、陕西话、上海话、闽南语、长沙话、合肥话等
#   - 童声 / 卡通角色
#   - 中英 / 英文 / 日文 / 多语种
# 每个音色提供 gender / age / scene / dialect / zh_tags，方便前端多维筛选。
#
# 维护要点：
#   - 豆包会不定期新增音色；如有新 ID 需求，可在 voices_doubao.json 里追加覆盖
#     （内置条目作为 id 默认存在时，自定义条目按 id 覆盖）。
#   - 旧 zh_*_xxx 风格 ID 是历史约定（豆包 v1 API 兼容），新音色全部走 BVxxx_stream。
# =====================================================================
_BUILTIN_VOICES: list[dict[str, Any]] = [
    # ===== 通用女声 =====
    {"id": "BV001_stream", "name": "通用女声·磁性", "gender": "female", "age": "youth",
     "scene": ["通用", "情感", "有声书"], "dialect": "",
     "zh_tags": ["通用", "磁性", "成熟"]},
    {"id": "BV002_stream", "name": "通用女声·甜美女声", "gender": "female", "age": "teen",
     "scene": ["通用", "娱乐"], "dialect": "",
     "zh_tags": ["通用", "甜美", "少女"]},
    {"id": "BV003_stream", "name": "通用女声·活力", "gender": "female", "age": "youth",
     "scene": ["通用", "教育"], "dialect": "",
     "zh_tags": ["通用", "活力", "教育"]},
    {"id": "BV004_stream", "name": "通用女声·温柔", "gender": "female", "age": "youth",
     "scene": ["通用", "情感", "有声书"], "dialect": "",
     "zh_tags": ["通用", "温柔", "情感"]},
    {"id": "BV005_stream", "name": "通用女声·知性", "gender": "female", "age": "middle",
     "scene": ["通用", "新闻", "纪录片"], "dialect": "",
     "zh_tags": ["通用", "知性", "新闻"]},
    {"id": "BV006_stream", "name": "通用女声·沉稳", "gender": "female", "age": "middle",
     "scene": ["通用", "纪录片", "广告"], "dialect": "",
     "zh_tags": ["通用", "沉稳", "成熟"]},
    {"id": "BV007_stream", "name": "通用女声·成熟", "gender": "female", "age": "middle",
     "scene": ["通用", "有声书"], "dialect": "",
     "zh_tags": ["通用", "成熟", "有声书"]},
    {"id": "BV008_stream", "name": "通用女声·沙哑", "gender": "female", "age": "youth",
     "scene": ["通用", "广告"], "dialect": "",
     "zh_tags": ["通用", "沙哑", "广告"]},
    {"id": "BV009_stream", "name": "通用女声·萝莉", "gender": "female", "age": "child",
     "scene": ["通用", "娱乐"], "dialect": "",
     "zh_tags": ["通用", "萝莉", "可爱"]},
    {"id": "BV010_stream", "name": "通用女声·御姐", "gender": "female", "age": "youth",
     "scene": ["通用", "情感", "有声书"], "dialect": "",
     "zh_tags": ["通用", "御姐", "成熟"]},
    # ===== 通用男声 =====
    {"id": "BV011_stream", "name": "通用男声·磁性", "gender": "male", "age": "youth",
     "scene": ["通用", "情感", "有声书"], "dialect": "",
     "zh_tags": ["通用", "磁性", "男友"]},
    {"id": "BV012_stream", "name": "通用男声·青年清澈", "gender": "male", "age": "youth",
     "scene": ["通用", "有声书", "日常"], "dialect": "",
     "zh_tags": ["通用", "青年", "清澈"]},
    {"id": "BV013_stream", "name": "通用男声·青年活力", "gender": "male", "age": "youth",
     "scene": ["通用", "教育", "娱乐"], "dialect": "",
     "zh_tags": ["通用", "青年", "活力"]},
    {"id": "BV014_stream", "name": "通用男声·成熟", "gender": "male", "age": "middle",
     "scene": ["通用", "纪录片", "广告"], "dialect": "",
     "zh_tags": ["通用", "成熟", "商务"]},
    {"id": "BV015_stream", "name": "通用男声·沉稳", "gender": "male", "age": "middle",
     "scene": ["通用", "纪录片", "广告"], "dialect": "",
     "zh_tags": ["通用", "沉稳", "广告"]},
    {"id": "BV016_stream", "name": "通用男声·霸气", "gender": "male", "age": "middle",
     "scene": ["通用", "有声书", "影视"], "dialect": "",
     "zh_tags": ["通用", "霸气", "配音"]},
    {"id": "BV017_stream", "name": "通用男声·少年", "gender": "male", "age": "teen",
     "scene": ["通用", "娱乐", "有声书"], "dialect": "",
     "zh_tags": ["通用", "少年", "配音"]},
    {"id": "BV018_stream", "name": "通用男声·低音炮", "gender": "male", "age": "middle",
     "scene": ["通用", "有声书"], "dialect": "",
     "zh_tags": ["通用", "低沉", "有声书"]},
    {"id": "BV019_stream", "name": "通用男声·温暖", "gender": "male", "age": "youth",
     "scene": ["通用", "情感", "有声书"], "dialect": "",
     "zh_tags": ["通用", "温暖", "情感"]},
    {"id": "BV020_stream", "name": "通用男声·新闻主播", "gender": "male", "age": "middle",
     "scene": ["新闻", "播报", "纪录片"], "dialect": "",
     "zh_tags": ["新闻", "主播", "专业"]},
    {"id": "BV021_stream", "name": "通用男声·青年播音", "gender": "male", "age": "youth",
     "scene": ["新闻", "播报"], "dialect": "",
     "zh_tags": ["新闻", "青年", "播音"]},
    # ===== 有声书 / 角色配音 =====
    {"id": "BV030_stream", "name": "配音男声·公子音", "gender": "male", "age": "youth",
     "scene": ["有声书", "古风", "角色"], "dialect": "",
     "zh_tags": ["古风", "公子", "配音"]},
    {"id": "BV031_stream", "name": "配音男声·仙侠道长", "gender": "male", "age": "middle",
     "scene": ["有声书", "古风", "角色"], "dialect": "",
     "zh_tags": ["古风", "道长", "配音"]},
    {"id": "BV032_stream", "name": "配音男声·帝王", "gender": "male", "age": "middle",
     "scene": ["有声书", "古风", "角色"], "dialect": "",
     "zh_tags": ["古风", "帝王", "配音"]},
    {"id": "BV033_stream", "name": "配音男声·少年将军", "gender": "male", "age": "youth",
     "scene": ["有声书", "古风", "角色"], "dialect": "",
     "zh_tags": ["古风", "将军", "少年"]},
    {"id": "BV034_stream", "name": "配音男声·反派", "gender": "male", "age": "middle",
     "scene": ["有声书", "角色"], "dialect": "",
     "zh_tags": ["反派", "配音"]},
    {"id": "BV035_stream", "name": "配音女声·公子音", "gender": "female", "age": "youth",
     "scene": ["有声书", "古风", "角色"], "dialect": "",
     "zh_tags": ["古风", "公子", "配音"]},
    {"id": "BV036_stream", "name": "配音女声·少女音", "gender": "female", "age": "teen",
     "scene": ["有声书", "角色"], "dialect": "",
     "zh_tags": ["少女", "配音"]},
    {"id": "BV037_stream", "name": "配音女声·御姐音", "gender": "female", "age": "youth",
     "scene": ["有声书", "角色"], "dialect": "",
     "zh_tags": ["御姐", "配音"]},
    {"id": "BV038_stream", "name": "配音女声·女王音", "gender": "female", "age": "middle",
     "scene": ["有声书", "古风", "角色"], "dialect": "",
     "zh_tags": ["女王", "配音"]},
    {"id": "BV039_stream", "name": "配音女声·少御音", "gender": "female", "age": "youth",
     "scene": ["有声书", "角色"], "dialect": "",
     "zh_tags": ["少御", "配音"]},
    {"id": "BV040_stream", "name": "配音女声·邻家少女", "gender": "female", "age": "teen",
     "scene": ["有声书", "角色"], "dialect": "",
     "zh_tags": ["少女", "邻家", "配音"]},
    {"id": "BV041_stream", "name": "配音女声·灵动少女", "gender": "female", "age": "teen",
     "scene": ["有声书", "角色"], "dialect": "",
     "zh_tags": ["少女", "灵动", "配音"]},
    {"id": "BV042_stream", "name": "配音女声·甜美女声", "gender": "female", "age": "teen",
     "scene": ["有声书", "角色"], "dialect": "",
     "zh_tags": ["甜美", "少女", "配音"]},
    {"id": "BV043_stream", "name": "配音女声·撒娇", "gender": "female", "age": "teen",
     "scene": ["有声书", "角色"], "dialect": "",
     "zh_tags": ["撒娇", "少女", "配音"]},
    # ===== 童声 / 卡通 =====
    {"id": "BV050_stream", "name": "童声·小女孩", "gender": "female", "age": "child",
     "scene": ["儿童", "亲子"], "dialect": "",
     "zh_tags": ["童声", "女孩", "儿童"]},
    {"id": "BV051_stream", "name": "童声·小男孩", "gender": "male", "age": "child",
     "scene": ["儿童", "亲子"], "dialect": "",
     "zh_tags": ["童声", "男孩", "儿童"]},
    {"id": "BV052_stream", "name": "卡通·小猪佩", "gender": "neutral", "age": "child",
     "scene": ["儿童", "动画"], "dialect": "",
     "zh_tags": ["卡通", "动画"]},
    {"id": "BV053_stream", "name": "卡通·熊孩子", "gender": "neutral", "age": "child",
     "scene": ["儿童", "动画"], "dialect": "",
     "zh_tags": ["卡通", "动画", "童趣"]},
    {"id": "BV054_stream", "name": "童声·讲故事", "gender": "female", "age": "child",
     "scene": ["儿童", "亲子", "故事"], "dialect": "",
     "zh_tags": ["童声", "讲故事"]},
    # ===== 经典 / 历史 zh_* ID（豆包 v1 兼容） =====
    {"id": "zh_female_qingxin", "name": "清新女声", "gender": "female", "age": "youth",
     "scene": ["日常", "新闻"], "dialect": "",
     "zh_tags": ["通用", "新闻", "叙事"]},
    {"id": "zh_female_wanwanxiaohe", "name": "弯弯小河", "gender": "female", "age": "youth",
     "scene": ["有声书", "情感"], "dialect": "",
     "zh_tags": ["甜美", "情感", "故事"]},
    {"id": "zh_male_qingnianqingche", "name": "青年清澈", "gender": "male", "age": "youth",
     "scene": ["有声书", "日常"], "dialect": "",
     "zh_tags": ["青年", "清澈", "叙事"]},
    {"id": "zh_female_tianmei", "name": "甜美女声", "gender": "female", "age": "teen",
     "scene": ["亲子", "娱乐"], "dialect": "",
     "zh_tags": ["萝莉", "可爱", "甜美"]},
    {"id": "zh_male_chuangshijia", "name": "创业家", "gender": "male", "age": "middle",
     "scene": ["商业", "演讲"], "dialect": "",
     "zh_tags": ["商务", "成熟", "演讲"]},
    {"id": "zh_female_yunxi", "name": "芸夕", "gender": "female", "age": "middle",
     "scene": ["有声书", "散文"], "dialect": "",
     "zh_tags": ["知性", "散文", "情感"]},
    {"id": "zh_male_chengshushenchen", "name": "成熟深沉男声", "gender": "male", "age": "middle",
     "scene": ["广告", "纪录片"], "dialect": "",
     "zh_tags": ["广告", "成熟", "沉稳"]},
    {"id": "zh_female_guangbozhuchi", "name": "广播主持女声", "gender": "female", "age": "youth",
     "scene": ["新闻", "播报"], "dialect": "",
     "zh_tags": ["新闻", "播报", "专业"]},
    {"id": "zh_male_nanyou_44100", "name": "男友音", "gender": "male", "age": "teen",
     "scene": ["恋爱", "有声书"], "dialect": "",
     "zh_tags": ["少年", "温柔", "恋爱"]},
    {"id": "zh_female_sunshine", "name": "阳光女声", "gender": "female", "age": "youth",
     "scene": ["教育", "儿童"], "dialect": "",
     "zh_tags": ["阳光", "教育", "活力"]},
    {"id": "zh_male_dianshizhuchi", "name": "电视主持男声", "gender": "male", "age": "middle",
     "scene": ["新闻", "晚会"], "dialect": "",
     "zh_tags": ["主持", "新闻", "权威"]},
    {"id": "zh_female_aidaier", "name": "爱黛儿", "gender": "female", "age": "youth",
     "scene": ["英文", "日常"], "dialect": "",
     "zh_tags": ["中英混合", "外语", "成熟"]},
    {"id": "zh_male_xiaohai", "name": "小男孩", "gender": "male", "age": "child",
     "scene": ["儿童", "动画"], "dialect": "",
     "zh_tags": ["男童", "儿童", "童真"]},
    {"id": "zh_female_lisachangjiang", "name": "丽莎长讲（四川话）", "gender": "female", "age": "youth",
     "scene": ["方言", "搞笑"], "dialect": "sichuan",
     "zh_tags": ["四川话", "方言", "搞笑"]},
    # ===== 方言 =====
    {"id": "BV100_stream", "name": "粤语女声", "gender": "female", "age": "youth",
     "scene": ["方言", "娱乐"], "dialect": "cantonese",
     "zh_tags": ["粤语", "方言"]},
    {"id": "BV101_stream", "name": "粤语男声", "gender": "male", "age": "middle",
     "scene": ["方言", "播报"], "dialect": "cantonese",
     "zh_tags": ["粤语", "方言", "主播"]},
    {"id": "BV102_stream", "name": "四川话男声", "gender": "male", "age": "middle",
     "scene": ["方言", "搞笑"], "dialect": "sichuan",
     "zh_tags": ["四川话", "方言", "搞笑"]},
    {"id": "BV103_stream", "name": "四川话女声", "gender": "female", "age": "youth",
     "scene": ["方言", "搞笑"], "dialect": "sichuan",
     "zh_tags": ["四川话", "方言"]},
    {"id": "BV104_stream", "name": "东北话男声", "gender": "male", "age": "middle",
     "scene": ["方言", "搞笑"], "dialect": "dongbei",
     "zh_tags": ["东北话", "方言", "搞笑"]},
    {"id": "BV105_stream", "name": "东北话女声", "gender": "female", "age": "youth",
     "scene": ["方言", "搞笑"], "dialect": "dongbei",
     "zh_tags": ["东北话", "方言"]},
    {"id": "BV106_stream", "name": "陕西话男声", "gender": "male", "age": "middle",
     "scene": ["方言", "搞笑"], "dialect": "shaanxi",
     "zh_tags": ["陕西话", "方言"]},
    {"id": "BV107_stream", "name": "陕西话女声", "gender": "female", "age": "middle",
     "scene": ["方言", "搞笑"], "dialect": "shaanxi",
     "zh_tags": ["陕西话", "方言"]},
    {"id": "BV108_stream", "name": "上海话女声", "gender": "female", "age": "youth",
     "scene": ["方言", "娱乐"], "dialect": "shanghai",
     "zh_tags": ["上海话", "方言"]},
    {"id": "BV109_stream", "name": "上海话男声", "gender": "male", "age": "middle",
     "scene": ["方言", "娱乐"], "dialect": "shanghai",
     "zh_tags": ["上海话", "方言"]},
    {"id": "BV110_stream", "name": "闽南语男声", "gender": "male", "age": "middle",
     "scene": ["方言", "娱乐"], "dialect": "minnan",
     "zh_tags": ["闽南语", "方言"]},
    {"id": "BV111_stream", "name": "闽南语女声", "gender": "female", "age": "youth",
     "scene": ["方言", "娱乐"], "dialect": "minnan",
     "zh_tags": ["闽南语", "方言"]},
    {"id": "BV112_stream", "name": "长沙话男声", "gender": "male", "age": "youth",
     "scene": ["方言", "娱乐"], "dialect": "changsha",
     "zh_tags": ["长沙话", "方言"]},
    {"id": "BV113_stream", "name": "合肥话女声", "gender": "female", "age": "middle",
     "scene": ["方言", "娱乐"], "dialect": "hefei",
     "zh_tags": ["合肥话", "方言"]},
    {"id": "BV114_stream", "name": "天津话男声", "gender": "male", "age": "middle",
     "scene": ["方言", "搞笑"], "dialect": "tianjin",
     "zh_tags": ["天津话", "方言", "搞笑"]},
    {"id": "BV115_stream", "name": "山东话男声", "gender": "male", "age": "middle",
     "scene": ["方言", "娱乐"], "dialect": "shandong",
     "zh_tags": ["山东话", "方言"]},
    {"id": "BV116_stream", "name": "河南话男声", "gender": "male", "age": "middle",
     "scene": ["方言", "娱乐"], "dialect": "henan",
     "zh_tags": ["河南话", "方言"]},
    # ===== 外语 =====
    {"id": "BV200_stream", "name": "英文女声·美式", "gender": "female", "age": "youth",
     "scene": ["英文", "教育"], "dialect": "",
     "zh_tags": ["英文", "美式", "外语"]},
    {"id": "BV201_stream", "name": "英文男声·美式", "gender": "male", "age": "youth",
     "scene": ["英文", "教育"], "dialect": "",
     "zh_tags": ["英文", "美式", "外语"]},
    {"id": "BV202_stream", "name": "英文女声·英式", "gender": "female", "age": "middle",
     "scene": ["英文", "播报"], "dialect": "",
     "zh_tags": ["英文", "英式", "外语"]},
    {"id": "BV203_stream", "name": "英文男声·英式", "gender": "male", "age": "middle",
     "scene": ["英文", "纪录片"], "dialect": "",
     "zh_tags": ["英文", "英式", "外语"]},
    {"id": "BV204_stream", "name": "日文女声", "gender": "female", "age": "youth",
     "scene": ["日文", "娱乐"], "dialect": "",
     "zh_tags": ["日文", "外语"]},
    {"id": "BV205_stream", "name": "日文男声", "gender": "male", "age": "middle",
     "scene": ["日文", "纪录片"], "dialect": "",
     "zh_tags": ["日文", "外语"]},
    {"id": "BV206_stream", "name": "韩文女声", "gender": "female", "age": "youth",
     "scene": ["韩文", "娱乐"], "dialect": "",
     "zh_tags": ["韩文", "外语"]},
    {"id": "BV207_stream", "name": "中英混合女声", "gender": "female", "age": "youth",
     "scene": ["中英混合", "教育"], "dialect": "",
     "zh_tags": ["中英混合", "外语"]},
    {"id": "BV208_stream", "name": "中英混合男声", "gender": "male", "age": "youth",
     "scene": ["中英混合", "教育"], "dialect": "",
     "zh_tags": ["中英混合", "外语"]},
    # ===== 广告 / 影视 / 情感 / 配音 =====
    {"id": "BV300_stream", "name": "广告男声·浑厚", "gender": "male", "age": "middle",
     "scene": ["广告", "纪录片"], "dialect": "",
     "zh_tags": ["广告", "浑厚"]},
    {"id": "BV301_stream", "name": "广告女声·温婉", "gender": "female", "age": "middle",
     "scene": ["广告", "纪录片"], "dialect": "",
     "zh_tags": ["广告", "温婉"]},
    {"id": "BV302_stream", "name": "广告男声·激情", "gender": "male", "age": "youth",
     "scene": ["广告", "演讲"], "dialect": "",
     "zh_tags": ["广告", "激情"]},
    {"id": "BV303_stream", "name": "广告女声·活力", "gender": "female", "age": "youth",
     "scene": ["广告", "娱乐"], "dialect": "",
     "zh_tags": ["广告", "活力"]},
    {"id": "BV310_stream", "name": "配音·旁白男声", "gender": "male", "age": "middle",
     "scene": ["有声书", "纪录片", "配音"], "dialect": "",
     "zh_tags": ["旁白", "配音"]},
    {"id": "BV311_stream", "name": "配音·旁白女声", "gender": "female", "age": "middle",
     "scene": ["有声书", "纪录片", "配音"], "dialect": "",
     "zh_tags": ["旁白", "配音"]},
    {"id": "BV312_stream", "name": "配音·解说男声", "gender": "male", "age": "middle",
     "scene": ["纪录片", "解说"], "dialect": "",
     "zh_tags": ["解说", "纪录片"]},
    {"id": "BV313_stream", "name": "配音·解说女声", "gender": "female", "age": "middle",
     "scene": ["纪录片", "解说"], "dialect": "",
     "zh_tags": ["解说", "纪录片"]},
    {"id": "BV320_stream", "name": "情感男声·温暖", "gender": "male", "age": "youth",
     "scene": ["情感", "有声书"], "dialect": "",
     "zh_tags": ["情感", "温暖"]},
    {"id": "BV321_stream", "name": "情感女声·温柔", "gender": "female", "age": "youth",
     "scene": ["情感", "有声书"], "dialect": "",
     "zh_tags": ["情感", "温柔"]},
    {"id": "BV322_stream", "name": "情感男声·磁性", "gender": "male", "age": "middle",
     "scene": ["情感", "有声书"], "dialect": "",
     "zh_tags": ["情感", "磁性"]},
    # ===== 教学 / 客服 / 助手 =====
    {"id": "BV400_stream", "name": "教学男声·讲师", "gender": "male", "age": "middle",
     "scene": ["教育", "讲解"], "dialect": "",
     "zh_tags": ["教学", "讲师"]},
    {"id": "BV401_stream", "name": "教学女声·讲师", "gender": "female", "age": "youth",
     "scene": ["教育", "讲解"], "dialect": "",
     "zh_tags": ["教学", "讲师"]},
    {"id": "BV402_stream", "name": "客服男声", "gender": "male", "age": "youth",
     "scene": ["客服", "助手"], "dialect": "",
     "zh_tags": ["客服", "助手"]},
    {"id": "BV403_stream", "name": "客服女声", "gender": "female", "age": "youth",
     "scene": ["客服", "助手"], "dialect": "",
     "zh_tags": ["客服", "助手"]},
    {"id": "BV404_stream", "name": "助手男声·沉稳", "gender": "male", "age": "middle",
     "scene": ["助手", "智能硬件"], "dialect": "",
     "zh_tags": ["助手", "智能"]},
    {"id": "BV405_stream", "name": "助手女声·温柔", "gender": "female", "age": "youth",
     "scene": ["助手", "智能硬件"], "dialect": "",
     "zh_tags": ["助手", "智能"]},
    # ===== 新闻主播 =====
    {"id": "BV500_stream", "name": "新闻主播·男·央视", "gender": "male", "age": "middle",
     "scene": ["新闻", "播报", "纪录片"], "dialect": "",
     "zh_tags": ["新闻", "主播", "播报"]},
    {"id": "BV501_stream", "name": "新闻主播·女·央视", "gender": "female", "age": "middle",
     "scene": ["新闻", "播报", "纪录片"], "dialect": "",
     "zh_tags": ["新闻", "主播", "播报"]},
    {"id": "BV502_stream", "name": "新闻主播·男·青年", "gender": "male", "age": "youth",
     "scene": ["新闻", "播报"], "dialect": "",
     "zh_tags": ["新闻", "青年"]},
    {"id": "BV503_stream", "name": "新闻主播·女·青年", "gender": "female", "age": "youth",
     "scene": ["新闻", "播报"], "dialect": "",
     "zh_tags": ["新闻", "青年"]},
    {"id": "BV504_stream", "name": "财经主播·男", "gender": "male", "age": "middle",
     "scene": ["新闻", "财经", "播报"], "dialect": "",
     "zh_tags": ["财经", "新闻"]},
    {"id": "BV505_stream", "name": "体育主播·男", "gender": "male", "age": "youth",
     "scene": ["新闻", "体育"], "dialect": "",
     "zh_tags": ["体育", "新闻"]},
    # ===== 古风 / 二次元 / 角色 =====
    {"id": "BV600_stream", "name": "古风·男主", "gender": "male", "age": "youth",
     "scene": ["有声书", "古风"], "dialect": "",
     "zh_tags": ["古风", "配音"]},
    {"id": "BV601_stream", "name": "古风·女主", "gender": "female", "age": "youth",
     "scene": ["有声书", "古风"], "dialect": "",
     "zh_tags": ["古风", "配音"]},
    {"id": "BV602_stream", "name": "古风·女主·灵动", "gender": "female", "age": "teen",
     "scene": ["有声书", "古风"], "dialect": "",
     "zh_tags": ["古风", "灵动"]},
    {"id": "BV603_stream", "name": "古风·太后", "gender": "female", "age": "middle",
     "scene": ["有声书", "古风"], "dialect": "",
     "zh_tags": ["古风", "太后"]},
    {"id": "BV604_stream", "name": "古风·皇帝", "gender": "male", "age": "middle",
     "scene": ["有声书", "古风"], "dialect": "",
     "zh_tags": ["古风", "皇帝"]},
    {"id": "BV610_stream", "name": "二次元·学姐", "gender": "female", "age": "youth",
     "scene": ["二次元", "动画"], "dialect": "",
     "zh_tags": ["二次元", "学姐"]},
    {"id": "BV611_stream", "name": "二次元·学妹", "gender": "female", "age": "teen",
     "scene": ["二次元", "动画"], "dialect": "",
     "zh_tags": ["二次元", "学妹"]},
    {"id": "BV612_stream", "name": "二次元·少年", "gender": "male", "age": "teen",
     "scene": ["二次元", "动画"], "dialect": "",
     "zh_tags": ["二次元", "少年"]},
    {"id": "BV613_stream", "name": "二次元·正太", "gender": "male", "age": "child",
     "scene": ["二次元", "动画"], "dialect": "",
     "zh_tags": ["二次元", "正太"]},
    # ===== 经典 zh_* ID（已含上述 14 条之外补一些热门） =====
    {"id": "zh_female_shuangkuai", "name": "爽快女声", "gender": "female", "age": "youth",
     "scene": ["通用", "娱乐"], "dialect": "",
     "zh_tags": ["爽快", "通用"]},
    {"id": "zh_male_jingying", "name": "精英男声", "gender": "male", "age": "middle",
     "scene": ["商业", "演讲"], "dialect": "",
     "zh_tags": ["商务", "精英"]},
    {"id": "zh_female_wenrou", "name": "温柔女声", "gender": "female", "age": "youth",
     "scene": ["情感", "有声书"], "dialect": "",
     "zh_tags": ["温柔", "情感"]},
    {"id": "zh_male_wenrou", "name": "温柔男声", "gender": "male", "age": "youth",
     "scene": ["情感", "有声书"], "dialect": "",
     "zh_tags": ["温柔", "情感"]},
    {"id": "zh_female_xinjiang", "name": "新疆女声", "gender": "female", "age": "youth",
     "scene": ["方言", "娱乐"], "dialect": "xinjiang",
     "zh_tags": ["新疆", "方言"]},
    {"id": "zh_male_xinjiang", "name": "新疆男声", "gender": "male", "age": "youth",
     "scene": ["方言", "娱乐"], "dialect": "xinjiang",
     "zh_tags": ["新疆", "方言"]},
    {"id": "zh_female_minnan", "name": "闽南语女声", "gender": "female", "age": "middle",
     "scene": ["方言", "娱乐"], "dialect": "minnan",
     "zh_tags": ["闽南语", "方言"]},
    {"id": "zh_female_yueyu", "name": "粤语女声", "gender": "female", "age": "youth",
     "scene": ["方言", "娱乐"], "dialect": "cantonese",
     "zh_tags": ["粤语", "方言"]},
    {"id": "zh_male_yueyu", "name": "粤语男声", "gender": "male", "age": "middle",
     "scene": ["方言", "娱乐"], "dialect": "cantonese",
     "zh_tags": ["粤语", "方言"]},
    {"id": "zh_female_baogao", "name": "报告女声", "gender": "female", "age": "middle",
     "scene": ["新闻", "播报"], "dialect": "",
     "zh_tags": ["新闻", "播报"]},
    {"id": "zh_male_baogao", "name": "报告男声", "gender": "male", "age": "middle",
     "scene": ["新闻", "播报"], "dialect": "",
     "zh_tags": ["新闻", "播报"]},
    {"id": "zh_female_lvshi", "name": "女律师声", "gender": "female", "age": "middle",
     "scene": ["纪录片", "配音"], "dialect": "",
     "zh_tags": ["专业", "配音"]},
    {"id": "zh_male_lvshi", "name": "男律师声", "gender": "male", "age": "middle",
     "scene": ["纪录片", "配音"], "dialect": "",
     "zh_tags": ["专业", "配音"]},
]


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
        """合成端点：优先读 settings.DOUBAO_TTS_BASE_URL（支持 .env 覆写）。"""
        try:
            # settings 已在模块顶部导入；不重复导入，避免对启动 cwd 的隐式依赖
            return settings.DOUBAO_TTS_BASE_URL or self.DEFAULT_ENDPOINT
        except Exception:
            return self.DEFAULT_ENDPOINT

    # 重试参数（与 MiniMaxTTSProvider 对齐）
    MAX_RETRIES = 5
    BASE_BACKOFF_SECS = 0.6
    JITTER_SECS = 0.3

    # -----------------------------------------------------------------
    # 音色列表
    # -----------------------------------------------------------------
    def _builtin_voices_sync(self) -> list[dict[str, Any]]:
        """同步返回内置全部豆包音色（全量已填 provider=doubao + id 前缀 doubao:）。"""
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
        """合并内置豆包音色 + DATA_DIR/voices_doubao.json（存在时覆盖）。"""
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
        """取豆包访问令牌。

        优先级（与 MiniMax 厂商结构对齐）：
          1) PROVIDERS_CONFIG 中 id="doubao" 厂商的 api_key
             （设置页「火山引擎豆包语音」启用后填入并保存，会持久化到这里）
          2) settings.DOUBAO_AK（.env 扁平字段，兼容老部署）
          3) 进程环境变量 MEGACORE_ACCESS_KEY_FROM_ENV（容器/部署平台注入）
        任一来源非空都视为有效凭据；返回时按 "是否含空格" 判断走 Bearer 分号风格。
        """
        # 1) 多厂商配置
        try:
            from ....core.config import get_provider
            prov = get_provider("doubao")
            ak = (prov or {}).get("api_key") or ""
        except Exception:
            ak = ""
        if ak:
            if " " in ak:
                return ak.strip()
            return f"Bearer;{ak}"
        # 2) .env 扁平字段
        ak = settings.DOUBAO_AK
        if ak:
            if " " in ak:
                return ak.strip()
            return f"Bearer;{ak}"
        # 3) 进程 ENV 兜底（与原行为兼容）
        fallback = os.environ.get("MEGACORE_ACCESS_KEY_FROM_ENV") or ""
        if fallback:
            if " " in fallback:
                return fallback.strip()
            return f"Bearer;{fallback}"
        raise RuntimeError(
            "未配置豆包凭据：请在「设置 → 模型厂商 → 火山引擎豆包语音」"
            "启用并填入 API Key 后保存，或设置 DOUBAO_AK 环境变量。"
        )

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
