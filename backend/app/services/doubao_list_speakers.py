"""P2-1：豆包 ListSpeakers 官方接口客户端。

对接 [ListSpeakers - 大模型音色列表(新接口)](https://www.volcengine.com/docs/6561/2160690?lang=zh)，
按 ResourceID 分别拉取 seed-tts-1.0 / seed-tts-2.0 / seed-icl-2.0 的全部官方音色，
转成与 `_BUILTIN_VOICES` 兼容的 dict，写到 `data/voices_doubao_remote.json`。

鉴权：
  ListSpeakers 是控制台级 API（host=open.volcengineapi.com），鉴权用 HMAC-SHA256
  （Service=speech_saas_prod / Region=cn-north-1 / Version=2025-05-20 / Action=ListSpeakers）。
  凭据来自 `settings.DOUBAO_AK` / `DOUBAO_SK`（与 TTS 鉴权用的 X-Api-Key 是不同维度）。

降级策略：
  - 任一参数缺失 / 签名失败 / 网络错 → 仅 logger.warning，绝不抛错阻塞启动
  - 失败时下游回退到内置 `_BUILTIN_VOICES`（见 `doubao/tts.py list_voices`）
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from ..core.config import settings


logger = logging.getLogger(__name__)


# 官方接口常量（来自 ListSpeakers 文档）
_LIST_SPEAKERS_HOST = "open.volcengineapi.com"
_LIST_SPEAKERS_PATH = "/"
_LIST_SPEAKERS_SERVICE = "speech_saas_prod"
_LIST_SPEAKERS_REGION = "cn-north-1"
_LIST_SPEAKERS_VERSION = "2025-05-20"
_LIST_SPEAKERS_ACTION = "ListSpeakers"

# 我们要拉取的三个 model（与 provider 的 ResourceID 一致）
_RESOURCE_IDS: tuple[str, ...] = ("seed-tts-1.0", "seed-tts-2.0", "seed-icl-2.0")

# 单页最大 30（官方示例值；Limit 字段必须 string，与示例对齐）
_PAGE_SIZE = "30"


# ---------------------------------------------------------------------
# HMAC-SHA256 签名（参考火山引擎 [公共参数--API签名调用指南]）
# ---------------------------------------------------------------------
def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def _build_signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    """构造签名用的派生 key：SK → date → region → service → "request" 逐层 HMAC。"""
    k_date = _hmac_sha256(secret_key.encode("utf-8"), date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "request")


def _build_authorization(
    *, ak: str, sk: str, host: str, path: str,
    region: str, service: str,
    body: str, query: str, x_date: str, x_content_sha256: str,
) -> str:
    """构造 HMAC-SHA256 Authorization 头（参考 ListSpeakers 公共参数规范）。"""
    canonical_headers = (
        f"content-type:application/json\n"
        f"host:{host}\n"
        f"x-content-sha256:{x_content_sha256}\n"
        f"x-date:{x_date}\n"
    )
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = f"POST\n{path}\n{query}\n{canonical_headers}\n{signed_headers}\n{x_content_sha256}"
    # 规范请求的 hash
    hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    # 凭证范围
    credential_scope = f"{x_date[:8]}/{region}/{service}/request"
    # 待签名字符串
    string_to_sign = (
        f"HMAC-SHA256\n{x_date}\n{credential_scope}\n{hashed_canonical_request}"
    )
    # 派生签名 key
    signing_key = _build_signing_key(sk, x_date[:8], region, service)
    # 计算签名
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


# ---------------------------------------------------------------------
# 单页拉取
# ---------------------------------------------------------------------
async def _fetch_one_page(
    client: httpx.AsyncClient,
    *,
    ak: str, sk: str,
    page: int,
    resource_id: str,
) -> dict[str, Any]:
    """拉取指定 model + page 的 ListSpeakers 响应（原始 dict）。"""
    # 构造 query（Action + Version 必须出现在 URL query string）
    query = (
        f"Action={_LIST_SPEAKERS_ACTION}&Version={_LIST_SPEAKERS_VERSION}"
    )
    # body（application/json）
    body_obj: dict[str, Any] = {
        "ResourceIDs": [resource_id],
        "Page": page,
        "Limit": _PAGE_SIZE,
    }
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))

    # x-date 格式：20260105T072305Z（UTC）
    x_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    x_content_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()

    auth = _build_authorization(
        ak=ak, sk=sk,
        host=_LIST_SPEAKERS_HOST,
        path=_LIST_SPEAKERS_PATH,
        region=_LIST_SPEAKERS_REGION,
        service=_LIST_SPEAKERS_SERVICE,
        body=body,
        query=query,
        x_date=x_date,
        x_content_sha256=x_content_sha256,
    )
    headers = {
        "Host": _LIST_SPEAKERS_HOST,
        "Content-Type": "application/json; charset=UTF-8",
        "X-Date": x_date,
        "X-Content-Sha256": x_content_sha256,
        "Authorization": auth,
    }
    url = f"https://{_LIST_SPEAKERS_HOST}{_LIST_SPEAKERS_PATH}?{query}"
    resp = await client.post(url, content=body, headers=headers, timeout=20.0)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------
# 全量拉取 + 转 _BUILTIN_VOICES 结构
# ---------------------------------------------------------------------
_GENDER_MAP = {"男": "male", "女": "female"}
_AGE_MAP = {"儿童": "child", "少年": "teen", "青年": "youth", "中年": "middle", "老年": "senior"}


def _speaker_to_builtin_entry(speaker: dict[str, Any], default_resource: str) -> dict[str, Any]:
    """把 ListSpeakers 返回的单条 Speaker 转成内置 _BUILTIN_VOICES 同结构的 dict。"""
    voice_type = str(speaker.get("VoiceType") or "").strip()
    name = str(speaker.get("Name") or voice_type or "未命名")
    gender_raw = str(speaker.get("Gender") or "").strip()
    age_raw = str(speaker.get("Age") or "").strip()
    # Languages → ["zh"] / ["en"] 等
    languages: list[str] = []
    for lang in (speaker.get("Languages") or []):
        code = str(lang.get("Language") or "").strip()
        if not code:
            continue
        # 官方 enum: zh-cn / en / ja / ptbr / esmx / id → 取 - 前缀
        languages.append(code.split("-", 1)[0])
    # 分类
    scene: list[str] = []
    for cat in (speaker.get("Categories") or []):
        for c in cat.get("Categories") or []:
            scene.append(str(c))
    # 情感支持：有 Emotions 数组且非空 → supports_emotion=True
    emotions = speaker.get("Emotions") or []
    supports_emotion = bool(emotions)
    # 模型：优先用 speaker.ResourceID，回退调用方的 default_resource
    model = str(speaker.get("ResourceID") or default_resource)
    return {
        "id": voice_type,
        "name": name,
        "gender": _GENDER_MAP.get(gender_raw, "neutral"),
        "age": _AGE_MAP.get(age_raw, "youth"),
        "scene": scene or ["通用"],
        "dialect": "",
        "languages": languages or ["zh"],
        "supports_emotion": supports_emotion,
        "supports_subtitle": True,
        "supports_language": len(languages) > 1,
        # free 字段官方 ListSpeakers 不返回，保守按 False 标注
        # （运营场景：依赖内置白名单 `free=True` 标的 21 款；远程表里 free 留给"促销期"标注）
        "free": False,
        "model": model,
        "zh_tags": scene or ["远程"],
    }


async def fetch_remote_voices(
    *, ak: str | None = None, sk: str | None = None,
    resource_ids: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """拉取全量官方音色（按给定 ResourceID 集合分页拉取）。

    Args:
        ak/sk: 可选覆盖 settings.DOUBAO_AK/DOUBAO_SK（便于单测注入 mock）
        resource_ids: 要拉取的模型列表（默认全部三个）

    Returns:
        与 _BUILTIN_VOICES 完全兼容的 list[dict]（无 "doubao:" 前缀，由调用方按需加）

    Raises:
        RuntimeError: 凭据缺失或网络错 / 鉴权错（调用方决定是否降级）
    """
    _ak = (ak or settings.DOUBAO_AK or "").strip()
    _sk = (sk or settings.DOUBAO_SK or "").strip()
    if not _ak or not _sk:
        raise RuntimeError("DOUBAO_AK / DOUBAO_SK 未配置，无法调用 ListSpeakers")

    rid_list = resource_ids or _RESOURCE_IDS

    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        for rid in rid_list:
            page = 1
            while True:
                try:
                    data = await _fetch_one_page(client, ak=_ak, sk=_sk, page=page, resource_id=rid)
                except httpx.HTTPError as e:
                    raise RuntimeError(f"ListSpeakers 网络错 rid={rid} page={page}: {type(e).__name__}: {e}") from e

                result = data.get("Result") or {}
                speakers = result.get("Speakers") or []
                total = int(result.get("Total") or 0)
                for sp in speakers:
                    entry = _speaker_to_builtin_entry(sp, default_resource=rid)
                    if entry["id"]:
                        out.append(entry)

                # 翻页：已拉条数 ≥ total 或当页不足 → 退出
                pulled_so_far = page * int(_PAGE_SIZE)
                if not speakers or pulled_so_far >= total:
                    break
                page += 1
    return out


# ---------------------------------------------------------------------
# 磁盘缓存
# ---------------------------------------------------------------------
_REMOTE_VOICES_FILE = "voices_doubao_remote.json"


def remote_voices_path() -> Path:
    """远程音色 JSON 文件路径（默认 DATA_DIR/voices_doubao_remote.json）。"""
    return Path(settings.DATA_DIR) / _REMOTE_VOICES_FILE


def save_remote_voices(voices: list[dict[str, Any]]) -> Path:
    """把全量远程音色写到磁盘。原子写（tmp + replace）。"""
    p = remote_voices_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    payload = {
        "version": 1,
        "synced_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "voices": voices,
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def load_remote_voices() -> list[dict[str, Any]]:
    """读远程音色 JSON（不存在 / 解析错 → 返回空 list，不抛错）。"""
    p = remote_voices_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[doubao_list_speakers] 读取 {p.name} 失败，按空缓存处理: {type(e).__name__}: {e}")
        return []
    voices = data.get("voices") or []
    if not isinstance(voices, list):
        return []
    # 兼容旧 schema：list[dict] 直接返回；新 schema 带 version
    return voices


# ---------------------------------------------------------------------
# 顶层入口：拉取 + 写盘
# ---------------------------------------------------------------------
async def refresh_remote_voices() -> int:
    """重新拉取并写盘。返回写入的条数；失败仅 warning 返回 0。"""
    if not (settings.DOUBAO_AK and settings.DOUBAO_SK):
        logger.info("[doubao_list_speakers] DOUBAO_AK/SK 未配置，跳过远程音色同步（仅用内置表）")
        return 0
    try:
        voices = await fetch_remote_voices()
    except Exception as e:
        logger.warning(
            f"[doubao_list_speakers] 拉取远程音色失败，仅用内置表降级: "
            f"{type(e).__name__}: {e}"
        )
        return 0
    save_remote_voices(voices)
    logger.info(f"[doubao_list_speakers] 已同步远程音色 {len(voices)} 条 → {remote_voices_path().name}")
    return len(voices)


async def ensure_remote_voices_synced_once() -> int:
    """启动时调用：磁盘无缓存时同步一次（避免每次启动都打 ListSpeakers）。"""
    if remote_voices_path().is_file():
        cached = load_remote_voices()
        logger.info(
            f"[doubao_list_speakers] 复用磁盘缓存 {len(cached)} 条 "
            f"({remote_voices_path().name})；如需强制刷新请调 refresh_remote_voices()"
        )
        return len(cached)
    return await refresh_remote_voices()
