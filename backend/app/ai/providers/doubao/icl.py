"""豆包 ICL 2.0 声音复刻客户端（基于官方 v3 接口 /api/v3/tts/voice_clone + /get_voice）。

接口规范（按官方文档 https://www.volcengine.com/docs/6561/2227958?lang=zh +
                              https://www.volcengine.com/docs/6561/2535742?lang=en）：

  - 创建训练：POST https://openspeech.bytedance.com/api/v3/tts/voice_clone
    Headers: Content-Type: application/json
             X-Api-Key: <API Key>                  # 新版控制台
             X-Api-Request-Id: <uuid>              # 每次唯一
             （旧版兼容）X-Api-App-Key + X-Api-Access-Key
    Body（2026-09-21 按官方文档 6561/2534906 + 2227958 逐字段核对）: {
      "speaker_id": "custom_speaker_id",     # 必填。后付费音色固定传这个字面值
      "custom_speaker_id": "iclvoice<hex>",  # 后付费自定义代号（有官方防冲突正则）
      "audio": {"data": "<base64>", "format": "mp3|wav|m4a|..."},
      "text": "<可选，按此文本念诵，校验 WER>",
      "language": 0,                         # 0=cn / 1=en / 2=ja ...
      "extra_params": {                      # 官方把 demo_text 放在这一层
        "demo_text": "<可选，试听文本，4~300 字>",
        "enable_audio_denoise": false,
      },
    }
    响应: { "code": <int>, "message": ..., "speaker_id": "S_xxx",
            "status": 1/2/3/4, "speaker_status": [{"model_type": 5, ...}],
            "X-Tt-Logid": <header> }

    ⚠️ 两个历史错误（2026-09-21 修，都会让上游直接 500）：
      1. `model_type` 曾被下发到 body —— 它是**V1**训练接口
         （/api/v1/mega_tts/audio/upload）的整型字段，V3 请求参数表里没有它。
         V3 一次训练的音色对 1.0/2.0 **同时可用**，"用哪个版本合成"由合成时的
         X-Api-Resource-Id 决定；算法版本只体现在响应 speaker_status[].model_type
         （4=ICL V2 / 5=ICL V3）。
      2. `speaker_id` 曾被填成自己生成的 "icl_xxx" —— V3 要求它要么是控制台购买音色
         槽位后得到的 `S_xxx`（预付费），要么是固定字面值 `"custom_speaker_id"`
         （后付费，真实代号写在 custom_speaker_id 里）。而且自定义代号**不能以
         ICL_ 开头**（官方防冲突正则 `^((?i:S_|ICL_|MIX_|DiT_|BV)|...)`），
         所以 "icl_xxx" 本身就是非法名。

  - 查询训练：POST https://openspeech.bytedance.com/api/v3/tts/get_voice
    Headers: 同上
    Body: {"speaker_id": "S_xxx"} 或 {speaker_id:"custom_speaker_id", custom_speaker_id:"..."}
    响应: {
      "code": <int>, "message": ...,
      "speaker_id": "S_xxx",
      "status": 0/1/2/3/4,   # NotFound/Training/Success/Failed/Active
      "speaker_status": [{"model_type": 5, "demo_audio": "https://..."}, ...],
      "available_training_times": <int>,
      "create_time": <ms>, "language": <int>,
      "X-Tt-Logid": <header>
    }

本模块对调用方提供统一抽象：
  - create_training(voice_name, audio_bytes) → str  (返回豆包侧 speaker_id)
  - query_training(speaker_id) → dict 返回:
      {status, progress, cloned_voice_id, error, model_type, demo_audio}
      status 语义按官方文档翻译：0 NotFound / 1 Training / 2 Success /
      3 Failed / 4 Active（2/4 均可合成）
  - 所有调用走 factory._doubao_rpm_wait_acquire("icl") 统一限流。
  - 测试可 monkeypatch `_http_post_json`（与原约定一致，向后兼容）。
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time
import uuid
from typing import Any

from backend.app.ai.factory import _doubao_rpm_wait_acquire
from backend.app.core.config import settings

logger = logging.getLogger(__name__)


# 官方 status 语义（参考火山文档 /api/v3/tts/get_voice 响应字段说明）
# 0 NotFound / 1 Training / 2 Success / 3 Failed / 4 Active（2/4 均可合成）
STATUS_NOT_FOUND = 0
STATUS_TRAINING = 1
STATUS_SUCCESS = 2
STATUS_FAILED = 3
STATUS_ACTIVE = 4
_USABLE_STATUSES = (STATUS_SUCCESS, STATUS_ACTIVE)

# 官方 API 端点（v3 协议）
_DEFAULT_VOICE_CLONE_URL = "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
_DEFAULT_GET_VOICE_URL = "https://openspeech.bytedance.com/api/v3/tts/get_voice"


# ---------------------------------------------------------------------
# 后付费音色代号（custom_speaker_id）
# ---------------------------------------------------------------------
# 官方命名规范（6561/2534906 §命名规范）：
#   - 8~256 字符，仅 [a-zA-Z0-9_-]，必须英文字母开头
#   - 同 accountID 内不可重复
#   - 命中下面这条防冲突正则即被拦截
_OFFICIAL_SPEAKER_ID_FORBIDDEN_RE = re.compile(
    r"^((?i:S_|ICL_|MIX_|DiT_|BV)|[a-z]{2}_|"
    r"(?i:(wvae|moon|mercury|venus|earth|mars|jupiter|saturn|uranus|neptune|pluto|umm)_)).*"
    r"|.*_(?i:bigtts|bigtts_cc|tob|cs_tob|streaming)$"
    r"|^[^a-zA-Z]|.*[-_]$|^.{0,7}$|^.{257,}$|.*[^a-zA-Z0-9_-].*$"
)
# 前缀刻意**不带下划线**：`ICL_`（大小写不敏感）是官方保留前缀，`icl_xxx` 会被
# 防冲突正则直接拦截 —— 这正是历史实现踩的坑。
_CUSTOM_SPEAKER_ID_PREFIX = "iclvoice"
# 后付费音色在 speaker_id 字段里必须传这个固定字面值，真实代号写在 custom_speaker_id
_CUSTOM_SPEAKER_ID_SENTINEL = "custom_speaker_id"
# 预付费音色：控制台购买音色槽位后拿到的 S_xxx（直接用 speaker_id 定位）
_PREPAID_SPEAKER_ID_PREFIX = "S_"


def _generate_custom_speaker_id() -> str:
    """生成一个符合官方命名规范的后付费音色代号。"""
    sid = f"{_CUSTOM_SPEAKER_ID_PREFIX}{uuid.uuid4().hex}"
    m = _OFFICIAL_SPEAKER_ID_FORBIDDEN_RE.search(sid)
    if m:  # pragma: no cover - 常量写错时才会命中，测试有专门用例守着
        raise RuntimeError(f"生成的音色代号不符合豆包命名规范：{sid!r} 命中 {m.group(0)!r}")
    return sid


# ICL 复刻音色的 id 前缀（合成路由靠它决定走 seed-icl-* 还是 seed-tts-*）：
#   S_       官方预付费音色槽位（控制台购买后得到）
#   icl_     本项目历史自造前缀（已被官方防冲突正则拒绝，这里仅用于识别存量数据）
#   iclvoice 现行后付费自定义代号前缀
_ICL_SPEAKER_ID_PREFIXES: tuple[str, ...] = (
    _PREPAID_SPEAKER_ID_PREFIX,
    "icl_",
    _CUSTOM_SPEAKER_ID_PREFIX,
)


def is_cloned_speaker_id(voice_id: str) -> bool:
    """是否 ICL 声音复刻音色（裸 id 或带 `icl:` / `doubao:` 命名空间前缀都可以）。

    集中在这里判定，是因为音色代号的生成也在这里 —— tts.py / build.py 的
    资源通道（cluster / X-Api-Resource-Id）与缓存键都依赖这个判定。
    """
    bare = str(voice_id or "").strip()
    changed = True
    while changed:  # 允许 "icl:doubao:xxx" 这类叠加前缀
        changed = False
        for p in ("icl:", "doubao:"):
            if bare.startswith(p):
                bare = bare[len(p):]
                changed = True
    return bare.startswith(_ICL_SPEAKER_ID_PREFIXES)


def _speaker_lookup_payload(voice_id: str) -> dict[str, str]:
    """构造「查 / 定位音色」用的请求参数。

    官方查询接口支持两种定位方式：
      - 预付费：`{"speaker_id": "S_xxx"}`
      - 后付费：必须成对 `{"speaker_id": "custom_speaker_id",
                          "custom_speaker_id": "<自定义代号>"}`

    历史实现把自定义代号塞进 speaker_id，上游按预付费槽位去查，必然失败。
    """
    sid = str(voice_id or "").strip()
    if sid.startswith(_PREPAID_SPEAKER_ID_PREFIX):
        return {"speaker_id": sid}
    return {
        "speaker_id": _CUSTOM_SPEAKER_ID_SENTINEL,
        "custom_speaker_id": sid,
    }


class DoubaoICLHTTPError(RuntimeError):
    """豆包 ICL 接口返回 HTTP 非 2xx。

    官方文档明确：**训练失败时 HTTP 状态码不为 200**，真实原因放在 body 的
    `code` / `message` 里（如 45001107 speaker_id 未找到、45001109 WER 不一致、
    45001114 音频质量差）。所以这类响应必须把 body 读出来再抛，否则日志里只剩
    一句 `500 Internal Server Error`，完全无法定位。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        code: int | str | None = None,
        logid: str | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.logid = logid
        self.body = body


# 复刻接口错误码 → 可执行提示（码表见 .trae/notes/doubao-voice-apis.md §12.2）
_ICL_ERROR_HINTS: dict[int, str] = {
    45001001: (
        "参数缺失/格式不符：检查 audio.format（m4a / pcm 必传）、文件大小（≤10MB）、"
        "以及 speaker_id 与 custom_speaker_id 是否成对"
    ),
    45001102: "ASR 转写失败：参考音频不清晰，换一段安静环境下的人声",
    45001104: "声纹检测未通过（疑似敏感声纹），换一段素材",
    45001107: (
        "speaker_id 未找到：后付费音色必须传 speaker_id=\"custom_speaker_id\" + "
        "custom_speaker_id=<自定义代号>（代号不能以 ICL_ 开头）；"
        "预付费音色必须传控制台购买音色槽位后拿到的 S_xxx"
    ),
    45001109: "WER 不一致：参考音频内容与 text 字段对不上，去掉 text 或按音频实际内容填写",
    45001112: "SNR 检测错误：音频信噪比过低，换一段更干净的人声",
    45001114: "音频质量较差：换一段 6~10 秒的清晰人声（远离噪声 / 音乐 / 混响）",
    45001122: "ASR 未检测到人声：参考音频里没有可识别的说话声，或音量过低",
    45001123: "达到音色训练次数上限（预付费音色每个槽位 15 次）",
    45001124: "ASR 文本审核拒绝，换一段素材",
    45001125: "demo 文本审核拒绝，换一段 demo_text",
    45001126: "demo 文本长度错误：官方要求 4~300 字，且语种要与 language 一致",
    45001127: "参考音频审核拒绝，换一段素材",
    45001128: "参考音频文本审核拒绝，换一段素材",
    45000030: "账号未开通该资源，请在控制台开通对应服务",
}


def _icl_http_error_hint(
    status_code: int, code: int | str | None, message: str
) -> str:
    """把 HTTP 状态码 + body 业务码翻译成一句可执行的排查提示。"""
    try:
        icode = int(code) if code is not None else None
    except (TypeError, ValueError):
        icode = None
    if icode is not None and icode in _ICL_ERROR_HINTS:
        return _ICL_ERROR_HINTS[icode]
    if status_code == 403:
        return (
            "账号未开通对应服务：后付费音色需在控制台开通「豆包声音复刻模型 2.0 + "
            "后付费音色服务」，预付费音色需先购买音色槽位"
        )
    low = (message or "").lower()
    if "not found" in low and "speaker" in low:
        return _ICL_ERROR_HINTS[45001107]
    if 500 <= status_code < 600 and icode is None:
        return (
            "上游 5xx 且 body 里没有业务码：多半是服务端瞬时异常，可重试；"
            "持续失败请带 logid 找火山技术支持"
        )
    return ""


class DoubaoICLClient:
    """豆包 ICL 2.0 声音复刻客户端（基于 v3 接口）。

    鉴权优先级（由 config.doubao_icl_api_key / doubao_icl_access_key 统一处理）：
      1. PROVIDERS_CONFIG[id=doubao].icl_api_key（页面 ICL key）
      2. PROVIDERS_CONFIG[id=doubao].api_key（页面通用 AK）
      3. settings.DOUBAO_ICL_API_KEY（.env 兜底）
      4. settings.DOUBAO_AK（.env 兜底）
      5. 环境变量 MEGACORE_ACCESS_KEY_FROM_ENV
    """

    name = "doubao_icl"

    @property
    def voice_clone_url(self) -> str:
        """创建训练任务端点。"""
        from backend.app.core.config import doubao_field
        base = (doubao_field("icl_endpoint") or _DEFAULT_VOICE_CLONE_URL).rstrip("/")
        # 兼容历史配置：如果用户配置的是 /api/v1/voice_clone（旧的 create/query 自造端点），
        # 强制重写为 v3 标准端点
        if "voice_clone" in base and "/v3/" not in base and base.endswith("/voice_clone"):
            return _DEFAULT_VOICE_CLONE_URL
        return base

    @property
    def get_voice_url(self) -> str:
        """查询训练状态端点。"""
        # 与 voice_clone_url 同源；如果用户改了 BASE_URL 指向了别的协议，
        # 这里直接拼到同一 base 上
        base = self.voice_clone_url
        if base.endswith("/voice_clone"):
            return base[: -len("/voice_clone")] + "/get_voice"
        return _DEFAULT_GET_VOICE_URL

    # ---------------------------------------------------------------
    # 鉴权
    # ---------------------------------------------------------------
    def _resolve_api_key(self) -> str:
        """按优先级解析 API Key（页面 ICL key → 通用 api_key → .env → ENV）。"""
        from backend.app.core.config import doubao_icl_api_key
        ak = doubao_icl_api_key() or ""
        if not ak:
            ak = os.environ.get("MEGACORE_ACCESS_KEY_FROM_ENV") or ""
        if not ak:
            raise RuntimeError(
                "未配置豆包凭据：请在「设置 → 模型厂商 → 火山引擎豆包语音」"
                "启用并填入 API Key 后保存，或设置 DOUBAO_ICL_API_KEY / DOUBAO_AK 环境变量。"
            )
        return ak.strip()

    def _auth_headers(self, *, prefix: str = "icl") -> dict[str, str]:
        """构造鉴权头。新版控制台：X-Api-Key；旧版控制台：X-Api-App-Key + X-Api-Access-Key。

        简单策略：默认按新版控制台发 X-Api-Key；如果 key 看起来像 APP_ID（纯数字），
        则按旧版鉴权（需要额外的 access_key）。

        Args:
            prefix: X-Api-Request-Id 前缀，方便日志按调用类型区分（create/get）。
        """
        from backend.app.core.config import doubao_icl_access_key
        ak = self._resolve_api_key()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Api-Request-Id": f"{prefix}-{uuid.uuid4().hex}",
        }
        # 兼容"Bearer;xxx" 旧写法（与 tts.py 一致的历史约定）
        ak_value = ak.split()[-1] if " " in ak else ak
        if ak_value.lstrip("-").isdigit():
            # 看起来是纯数字 APP_ID → 走旧版鉴权
            access_key = doubao_icl_access_key() or ak_value
            headers["X-Api-App-Key"] = ak_value
            headers["X-Api-Access-Key"] = str(access_key)
        else:
            headers["X-Api-Key"] = ak_value
        return headers

    # ---------------------------------------------------------------
    # 对外 API
    # ---------------------------------------------------------------
    async def create_training(
        self,
        voice_name: str,
        audio_bytes: bytes,
        *,
        audio_format: str = "mp3",
        demo_text: str | None = None,
        language: int = 0,
        model_type: str | None = None,
    ) -> str:
        """创建声音复刻训练任务。

        Args:
            voice_name: 业务方给的展示名（仅用于本地记录，不下发豆包）
            audio_bytes: 参考音频（建议 6~30 秒清晰人声）
            audio_format: wav/mp3/ogg/m4a/aac（pcm、m4a 必须显式传 format）
            demo_text: 可选，试听文本（4~300 字，语种要与 language 一致）
            language: 0=cn / 1=en / 2=ja ...
            model_type: **仅做白名单校验，不下发**（V3 请求体没有该字段，见函数内注释）。
                        None 时回退默认值（ICL2.0）。

        Returns:
            豆包侧音色 ID（后付费通常就是我们提交的 `iclvoice<hex>` 自定义代号；
            预付费是 `S_xxx`），合成时透传给 TTS 接口。

        Raises:
            ValueError: 音频过小 / model_type 不在白名单
            DoubaoICLHTTPError: HTTP 非 2xx（复刻接口的业务错误通道，带 code/message/logid）
            RuntimeError: HTTP 200 但业务码非 0
        """
        if not audio_bytes or len(audio_bytes) < 512:
            raise ValueError("参考音频过小：请上传 3 秒以上（建议 6~10 秒）的清晰人声录音")

        # model_type 白名单校验（None 走默认）。
        # ⚠️ 只校验、**不下发**：该字段是 V1 训练接口的（整型 1~5），V3 请求体里没有它，
        # 下发会被上游以 HTTP 500 拒绝。保留校验是为了让 HTTP 层能对非法取值回 400、
        # 并在落库前拦住脏任务（见 test_icl_model_type_param_red.py）。
        from .models import is_valid_train_model_type, find_train_default
        if model_type is None:
            model_type = find_train_default()  # "ICL2.0"
        elif not is_valid_train_model_type(model_type):
            raise ValueError(
                f"不支持的 model_type={model_type!r}；"
                f"合法值见 /api/doubao/models/options.train_model_types"
            )

        # speaker_id 由我们生成（前端 / DB 唯一标识）。
        # ⚠️ V3 协议里这个字段不能放自定义代号：后付费音色必须传固定字面值
        # "custom_speaker_id"，真实代号写在 custom_speaker_id 里；代号本身还必须
        # 躲开官方防冲突正则（不能以 ICL_ 开头、长度 ≥8、只能 [a-zA-Z0-9_-] 且
        # 首字符为字母）。历史实现直接塞 "icl_xxx" → 上游 500。
        custom_speaker_id = _generate_custom_speaker_id()

        payload: dict[str, Any] = {
            "speaker_id": _CUSTOM_SPEAKER_ID_SENTINEL,
            "custom_speaker_id": custom_speaker_id,
            "audio": {
                "data": base64.b64encode(audio_bytes).decode("ascii"),
                "format": audio_format.lower(),
            },
            "language": int(language),
        }
        # V3 把 demo_text 放在 extra_params 里（顶层不是官方字段）
        extra_params: dict[str, Any] = {}
        if demo_text:
            extra_params["demo_text"] = str(demo_text)[:300]
        if extra_params:
            payload["extra_params"] = extra_params
        # 业务方提供的 voice_name 用于日志/审计（不发给豆包）
        # model_type 不再下发：它是 V1 训练接口的字段，V3 没有；一次训练对
        # 1.0/2.0 同时可用，算法版本由合成时的 X-Api-Resource-Id 决定。

        await _doubao_rpm_wait_acquire("icl")
        # 请求头：让 _auth_headers 内部用 create- 前缀生成 X-Api-Request-Id
        resp = await self._http_post_json(
            self.voice_clone_url, payload,
            _request_kind="create",
        )
        code = int(resp.get("code", -1))
        if code != 0:
            msg = resp.get("message") or "未知错误"
            # P1-6：附 logid（如 _http_post_json 在返回 dict 里塞了 _logid）
            logid = resp.get("_logid")
            err_msg = f"豆包 ICL 创建训练任务失败：code={code} msg={msg} resp={resp}"
            if logid:
                err_msg = f"{err_msg} logid={logid}"
            hint = _icl_http_error_hint(200, code, str(msg))
            if hint:
                err_msg = f"{err_msg} | {hint}"
            err = RuntimeError(err_msg)
            try:
                err.logid = logid  # type: ignore[attr-defined]
            except Exception:
                pass
            raise err

        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        # 服务端返回的 speaker_id 才是权威音色 ID（预付费是 S_xxx；后付费通常就是
        # 我们提交的代号），取不到时回退我们自己生成的代号 —— 但**绝不能**回退成
        # "custom_speaker_id" 这个字面量，那只是个占位符。
        sid = str(data.get("speaker_id") or resp.get("speaker_id") or "")
        if not sid or sid == _CUSTOM_SPEAKER_ID_SENTINEL:
            sid = custom_speaker_id
        logger.info(
            f"[icl_client] create_training OK voice_name={voice_name} "
            f"speaker_id={sid} custom_speaker_id={custom_speaker_id}"
        )
        return str(sid)

    async def query_training(self, speaker_id: str) -> dict[str, Any]:
        """查询训练状态。

        Returns:
            {
              "status": 0/1/2/3/4,
              "progress": 100（终态）或 0（其他），
              "cloned_voice_id": speaker_id,  # 即返回的可用 speaker_id
              "error": "<失败原因>" | None,
              "model_type": <int> | None,        # 来自 speaker_status[0]
              "demo_audio": <url> | None,        # 来自 speaker_status[0]
            }
        """
        if not speaker_id:
            raise ValueError("speaker_id 不能为空")

        payload: dict[str, str] = _speaker_lookup_payload(speaker_id)

        await _doubao_rpm_wait_acquire("icl")
        resp = await self._http_post_json(
            self.get_voice_url, payload,
            _request_kind="get",
        )

        code = int(resp.get("code", -1))
        # NotFound 不算错——返回 status=0 让上层决定如何处理
        if code not in (0,):
            msg = resp.get("message") or "未知错误"
            # P1-6：附 logid
            logid = resp.get("_logid")
            err_msg = f"豆包 ICL 查询训练状态失败：code={code} msg={msg} resp={resp}"
            if logid:
                err_msg = f"{err_msg} logid={logid}"
            hint = _icl_http_error_hint(200, code, str(msg))
            if hint:
                err_msg = f"{err_msg} | {hint}"
            err = RuntimeError(err_msg)
            try:
                err.logid = logid  # type: ignore[attr-defined]
            except Exception:
                pass
            raise err

        status = int(resp.get("status", STATUS_NOT_FOUND))
        # progress：官方没返回具体进度。终态（2/4）置 100，其他置 0
        progress = 100 if status in _USABLE_STATUSES else (
            50 if status == STATUS_TRAINING else 0
        )
        speaker_status_list = resp.get("speaker_status") or []
        first_ss = speaker_status_list[0] if speaker_status_list else {}

        return {
            "status": status,
            "progress": progress,
            "cloned_voice_id": str(speaker_id) if status in _USABLE_STATUSES else None,
            "error": str(resp.get("message") or "") if status == STATUS_FAILED else None,
            "model_type": first_ss.get("model_type"),
            "demo_audio": first_ss.get("demo_audio"),
        }

    # ---------------------------------------------------------------
    # HTTP 内部方法（测试可 monkeypatch）
    # ---------------------------------------------------------------
    async def _http_post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *args: Any,
        _request_kind: str = "icl",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """POST JSON 并解析响应（测试可 monkeypatch）。

        注意：保留 `_http_post_json(url, payload)` 的 2 参数形式以向后兼容现有测试
        （test_doubao_task5_red.py 里 T-IC1/T-IC2 直接把 client._http_post_json 替换成
        一个 `_fake_post(url, payload)` 函数）。`_request_kind` 仅用于 _auth_headers 内部
        决定 X-Api-Request-Id 前缀；测试 fake 函数一般会忽略 kwargs。
        """
        import json as _json

        import httpx

        # 测试 fake 通常签名为 _fake_post(url, payload)，不接受 kwargs——
        # 为了不破坏向后兼容，把 _request_kind 提取后再调用 fake。
        # 但 fake 直接绑定到 client._http_post_json，会拦截方法调用，
        # 所以 fake 必须自己处理额外参数；这里我们改用：把 _request_kind 传给
        # 真实 HTTP 路径（_auth_headers），fake 路径则完全忽略（fake 只看 url/payload）。
        headers = self._auth_headers(prefix=_request_kind)

        timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            # P1-6：透出 logid 便于排查 + 在异常对象里附 logid 供 routes.py 透传
            logid = resp.headers.get("X-Tt-Logid") or resp.headers.get("x-tt-logid")
            if logid:
                logger.debug(f"[icl_client] logid={logid}")
            # 官方文档：**训练失败时 HTTP 状态码不为 200**，真实原因放在 body 的
            # code/message 里。必须先读 body 再抛，否则只剩一句 "500 Internal
            # Server Error"（历史日志就是这样，完全定位不到）。
            if resp.status_code >= 400:
                raise self._http_error(url, resp, logid=logid)
            data = _json.loads(resp.content.decode("utf-8"))
            # P1-6：把 logid 塞进返回 dict 的 _logid 字段（约定下划线前缀非业务字段）
            # 让 create_training / query_training 在 RuntimeError 时附 logid
            if isinstance(data, dict) and logid and "_logid" not in data:
                data["_logid"] = logid
            return data

    @staticmethod
    def _http_error(url: str, resp: Any, *, logid: str | None = None) -> DoubaoICLHTTPError:
        """把 HTTP 非 2xx 响应变成带 body / logid / 排查提示的异常。"""
        import json as _json

        raw = ""
        try:
            raw = resp.content.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - 解码失败也要能抛错
            raw = ""

        code: int | str | None = None
        message = ""
        try:
            parsed = _json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            # body 有两种形态：顶层 {code,message}（复刻接口）或
            # {header:{code,message}}（网关级 4xx）
            src = parsed.get("header") if isinstance(parsed.get("header"), dict) else parsed
            if src.get("code") is not None:
                try:
                    code = int(src["code"])
                except (TypeError, ValueError):
                    code = str(src["code"])
            message = str(src.get("message") or "")

        parts = [f"豆包 ICL 接口返回 HTTP {resp.status_code} url={url}"]
        if code is not None:
            parts.append(f"code={code}")
        if message:
            parts.append(f"msg={message}")
        if logid:
            parts.append(f"logid={logid}")
        hint = _icl_http_error_hint(resp.status_code, code, message)
        if hint:
            parts.append(f"提示：{hint}")
        if raw and not message:
            parts.append(f"body={raw[:300]}")
        return DoubaoICLHTTPError(
            " ".join(parts),
            status_code=resp.status_code,
            code=code,
            logid=logid,
            body=raw[:1000],
        )
