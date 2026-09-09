from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


# ============================================================
# 默认厂商模板（前端新建厂商时可参考；settings.PROVIDERS_CONFIG 初值）
# ============================================================
DEFAULT_PROVIDERS_TEMPLATE: list[dict[str, Any]] = [
    {
        "id": "minimax",
        "label": "MiniMax",
        "enabled": True,
        "api_key": "",
        "base_url": "https://api.minimaxi.com/v1",
        "tts_endpoint": "/v1/t2a_v2",
        "extra_headers": {},
        "models": [
            {"id": "MiniMax-speech-01", "label": "Speech-01 (TTS 2.0)", "kind": "tts"},
            {"id": "MiniMax-M3", "label": "MiniMax-M3", "kind": "llm"},
        ],
    },
    {
        "id": "doubao",
        "label": "火山引擎豆包语音",
        "enabled": False,
        "api_key": "",
        "base_url": "https://openspeech.bytedance.com/api/v1",
        "tts_endpoint": "/tts",
        "extra_headers": {},
        "models": [
            {"id": "volcano_tts", "label": "豆包语音合成 2.0", "kind": "tts"},
            {"id": "Doubao-pro-32k", "label": "Doubao-pro-32k", "kind": "llm"},
        ],
    },
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )
    TTS_API_KEY: str = ""
    TTS_BASE_URL: str = "https://api.minimaxi.com/v1"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.minimaxi.com/v1"
    LLM_MODEL_PRO: str = "MiniMax-M3"
    # fast 模型也用 M3：M2.x 的 thinking 无法关闭，会偶发陷入循环耗尽 token；
    # M3 可通过 thinking:{type:disabled} 真正关闭 thinking，反而更快更稳。
    LLM_MODEL_FAST: str = "MiniMax-M3"

    # ============ 多厂商 TTS 路由 ============
    # 全局默认 TTS 厂商（minimax | doubao）。Project.default_tts_provider 可覆写。
    TTS_PROVIDER: str = "minimax"

    # ============ 豆包语音全家桶 ============
    # 凭据：留空表示不启用豆包能力，系统降级仅用 MiniMax 且不报错。
    DOUBAO_AK: str = ""
    DOUBAO_SK: str = ""
    DOUBAO_APP_ID: str = ""

    # ICL 声音复刻专属凭据（新版控制台：API Key；旧版：Access Key）
    # 留空时回退到 DOUBAO_AK / DOUBAO_APP_ID + DOUBAO_SK
    DOUBAO_ICL_API_KEY: str = ""
    DOUBAO_ICL_ACCESS_KEY: str = ""

    # 三个服务端点（使用豆包直连域名时，可按需覆写）
    # 注意：DOUBAO_ICL_BASE_URL 默认仍是 v1 自造端点路径（向后兼容旧部署）；
    # 实际 client 在运行时若检测到 /v1/voice_clone 会强制重写到 v3 标准端点。
    DOUBAO_TTS_BASE_URL: str = "https://openspeech.bytedance.com/api/v1/tts"
    DOUBAO_ICL_BASE_URL: str = "https://openspeech.bytedance.com/api/v1/voice_clone"
    DOUBAO_SEED_AUDIO_BASE_URL: str = "https://openspeech.bytedance.com/api/v1/seed_audio"

    # RPM 限流（多厂商独立桶）
    DOUBAO_TTS_RPM_LIMIT: int = 60
    DOUBAO_SEED_AUDIO_RPM_LIMIT: int = 10
    # ICL 查询被 worker(每 poll 间隔) + 前端详情轮询双路触发，12/min 不够用
    DOUBAO_ICL_RPM_LIMIT: int = 60

    # ICL 声音复刻：训练轮询间隔 / 超时 / 参考音频大小上限
    DOUBAO_ICL_POLL_INTERVAL_SECS: float = 5.0
    DOUBAO_ICL_TIMEOUT_SECS: int = 1800
    ICL_MAX_AUDIO_BYTES: int = 10 * 1024 * 1024  # 10MB

    # 多播剧严格失败模式（默认 true：任何章节失败 → 整 Build 失败，不占位降级）
    # 留配置点仅用于集成测试做对照回归，线上应保持 True。
    MULTICAST_STRICT_MODE: bool = True

    # =====================================================================
    # 多厂商模型配置（PROVIDERS_CONFIG）
    # 顶层 JSON 持久化结构；前端设置页按此渲染"厂商卡片"。
    # schema:
    #   {
    #     "providers": [
    #       {"id": "minimax", "label": "MiniMax", "enabled": true,
    #        "api_key": "...", "base_url": "https://...",
    #        "tts_endpoint": "/v1/t2a_v2",
    #        "extra_headers": {"X-Trace": "1"},
    #        "models": [
    #          {"id": "MiniMax-speech-01", "label": "Speech-01", "kind": "tts"},
    #          {"id": "MiniMax-M3", "label": "M3", "kind": "llm"},
    #        ]
    #       }, ...
    #     ],
    #     "active": {
    #       "tts": {"provider_id": "minimax", "model_id": "MiniMax-speech-01"},
    #       "llm": {"provider_id": "minimax", "model_id": "MiniMax-M3"},
    #     }
    #   }
    # 注：初值从 .env 的扁平 TTS_/LLM_ 字段一次性迁移；
    # 改 PROVIDERS_CONFIG 会原地覆盖（不重置）。
    # =====================================================================
    PROVIDERS_CONFIG: str = ""

    # 当前激活模型（指向 PROVIDERS_CONFIG 中的某个 provider.model）；
    # 用扁平字段便于 settings page 直接绑定与回填。
    ACTIVE_TTS_PROVIDER: str = "minimax"
    ACTIVE_TTS_MODEL: str = "MiniMax-speech-01"
    ACTIVE_LLM_PROVIDER: str = "minimax"
    ACTIVE_LLM_MODEL: str = "MiniMax-M3"

    # Server
    ENV: str = "dev"  # dev / test / prod / stage
    BIND_HOST: str = "127.0.0.1"
    PORT: int = 28000

    # Paths
    DATA_DIR: Path = Path("./data")
    AUDIO_DIR: Path = Path("./data/audio")
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/app.db"

    # Timeouts (sec)
    LLM_TIMEOUT: int = 600
    TTS_TIMEOUT: int = 600
    UVICORN_TIMEOUT: int = 600

    # LLM 限流：同时最多 N 个请求在飞（按量套餐 RPM 严格时建议 1=串行）
    # 业务层可能并发调用（如每章对白归属 asyncio.gather），这里在 provider 层强制串行
    LLM_MAX_CONCURRENCY: int = 1

    # 角色识别切片大小（字符）：整本小说角色识别时，按该大小切块后
    # **全量串行**调用 LLM（不抽样、不截断），最后对所有切块结果做一次
    # 跨切块合并 + dedup。50k 是 MiniMax 角色识别 prompt 的比较稳妥上限，
    # 既保证上下文足够又不会因超长输出导致 JSON 解析失败。
    # 估算：3000 字/章 × 1000 章 = 300 万字 → 60 个切片 × 串行 10s/个 ≈ 10 分钟
    LLM_CHAR_EXTRACT_SLICE_SIZE: int = 50_000

    # 注意：角色识别 **已移除"前 N 字抽样"策略**（LLM_CHAR_EXTRACT_LIMIT 已不再使用）。
    # 现在无论小说多长，都会按 SLICE_SIZE 全量切片跑完后合并去重；
    # 否则后半本书的新角色会被整本书漏掉，后续对白归属全部失败。

    # 对白归属批大小（章/批）：一次 LLM 请求同时处理 N 章的对白归属，
    # 能显著降低 HTTP 开销和 LLM 调度耗时。M2.7-highspeed 1M 上下文
    # 3000 字/章 × 14 章 ≈ 42k 字 + prompt ≈ 50k，远低于上限。
    DIALOGUE_BATCH_CHAPTERS: int = 14

    # 对白归属批并发度：同时在飞的批数量。LLM_MAX_CONCURRENCY=1 时这里多高都
    # 会被 provider semaphore 串行，但大于 1 时可在多个"批"之间让 HTTP/响应解析
    # 和下一批的语义处理重叠，总体更快。
    DIALOGUE_BATCH_CONCURRENCY: int = 2
    # 对白归属单批 LLM 调用失败后的业务层重试次数（0=不重试，provider 层另有兜底 3 次）
    DIALOGUE_BATCH_RETRY_COUNT: int = 2

    # TTS 并发限流（全局，段级）：同时最多 N 个 TTS synthesize 调用在飞。
    # 现在已经改成 **段级** semaphore（不是"每章并发"），默认 100 段并行是
    # 相对保守的值：短对白 1s/TTS，100 并发 ≈ 100 段/秒的吞吐。
    # 如果调用方遇到 TTS RPM 429，可下调到 50 / 20。
    TTS_MAX_CONCURRENCY: int = 200
    # TTS 分钟级 RPM 限流：60 秒窗口内最多 N 次 t2a_v2 请求。
    # 使用固定间隔 token bucket：每 (60/N) 秒放 1 个请求，严格匀速零脉冲。
    # 充值用户官方 20 RPM，默认 12 留 40% 安全余量（账号共享/网络抖动）。
    TTS_RPM_LIMIT: int = 12
    # TTS 段缓存：内存 LRU 上限（条）；超上限淘汰最旧。
    # 注：磁盘缓存不限制大小（AUDIO_DIR/_seg_cache/），重启后仍可命中。
    TTS_SEGMENT_CACHE_MAX_ENTRIES: int = 20_000
    # TTS 段缓存磁盘过期天数（启动 GC 时 mtime 超过此天数字段被删）
    TTS_SEGMENT_CACHE_TTL_DAYS: int = 30
    # TTS 段缓存磁盘总大小上限（GB）；启动 GC 超上限时按 mtime 从旧到新删除
    TTS_SEGMENT_CACHE_MAX_SIZE_GB: int = 20

    # 单段最大字符数：超长旁白/对白段自动按句读边界切分。
    # 动机：无对白章节原本会生成一整段数千字的旁白请求，超出 TTS 厂商长文本
    # 上限即整章失败（1 秒静音占位）。切分后单段失败只影响一小段。
    TTS_MAX_SEGMENT_CHARS: int = 600

    # prepare 阶段接入 LLM 润色纠错（polish）：按章调用，修正错别字/同音字。
    # 默认关闭 —— 每章一次 LLM 调用，会显著增加 prepare 费用与时长；
    # 在 设置页「合成质量」中开启。
    POLISH_ENABLED: bool = False


    # Build running 超时（小时）：如果 start_build 命中的 running build
    # started_at 距离现在超过该值，认为是被 kill 的孤儿，直接改 status 回 queued
    # 并起新 worker。避免"重启后端后 Build 永远合成中"。
    BUILD_RUNNING_TIMEOUT_HOURS: int = 6

    # Auth (JWT)
    JWT_SECRET: str = "change-me-in-production-please-use-a-long-random-string"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXP_DAYS: int = 7
    SEED_ADMIN_USER: str = "admin"
    SEED_ADMIN_PASS: str = "admin"
    # 测试/本地调试用：DISABLE_AUTH=1 时所有 /api/* 不校验 token
    DISABLE_AUTH: bool = False
    # ENV=prod 时必须把默认 admin/admin 改掉；如果仍使用默认密码，则
    # - 登录成功 must_change_password 强制 true
    # - 或者（严格模式）直接拒绝登录
    STRICT_PROD_SECURITY: bool = False

    # =====================================================================
    # 日志：默认同时输出到 stdout 和文件（RotatingFileHandler 10MB×5）
    # - LOG_FILE 为相对路径时，相对进程工作目录（即 start.sh 的 PROJ_DIR）
    # - 置空字符串 → 不落盘，只走 stdout
    # - LOG_LEVEL 支持 DEBUG / INFO / WARNING / ERROR
    # =====================================================================
    LOG_FILE: str = "./data/logs/app.log"
    LOG_LEVEL: str = "INFO"
    LOG_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB
    LOG_BACKUP_COUNT: int = 5

    # =====================================================================
    # 章节切分（正则驱动，完全不调 LLM）
    # =====================================================================

    # 章节标题正则列表：按顺序逐条匹配，每条均以 re.MULTILINE 编译。
    # 命中后整行作为章节标题（原文不改动）；不同模式命中同一位置会自动去重。
    # 可在 .env 中用 JSON 数组覆盖；也可直接在下方 DEFAULT 值里加自定义规则。
    # 提示：行首请用 ^[ \t]* 匹配前导空格/制表，避免正文内同名短语误命中。
    CHAPTER_SPLIT_PATTERNS: list[str] = [
        # 中文常见：第X章 / 第X回 / 第X节 / 第X卷 / 第X篇 / 第X部
        # X 支持汉字数字（一二三四五十百千万〇）、阿拉伯数字
        r"^[ \t]*第[ \t]*([零〇一二三四五六七八九十百千0-9]+)[ \t]*(章|回|节|卷|篇|部)[ \t]*[:：、\.]*[ \t]*([^\n]*)$",
        # 半角变体：Ep.X / Vol.X / Ch.X
        r"^[ \t]*(Ep|Episode|Vol|Volume|Ch|Chapter)[ \t]*[\.\-:：]?[ \t]*([0-9IVXLCDM]+)[\.\t \-:：]*([^\n]*)$",
        # 英文小说通用：Chapter X Title / CHAPTER I. Title
        r"^[ \t]*Chapter[ \t]+([0-9IVXLCDM]+)[\.\t \-:：]*([^\n]*)$",
        # 关键词标题：序章 / 楔子 / 引子 / 前言 / 序言 / 尾声 / 终章 / 后记 / 番外篇? / 番外 / 缘起 / 题辞 / 自叙
        r"^[ \t]*(序章|楔子|引子|前言|序言|尾声|终章|后记|缘起|题辞|自叙|番外篇|番外|结尾语|写在最后|附录)[ \t]*[:：]?[ \t]*([^\n]*)$",
    ]

    # 正则命中的最少章节数：低于该数量判定为"切章失败"。
    # 通常 2 即可（证明文本中至少存在两个独立标题段）。
    CHAPTER_SPLIT_MIN_MATCHES: int = 2

    # 切章失败时是否启用 3 万字/块 硬切兜底。
    # - False（默认）：切章失败直接提醒用户，**不**调用 LLM 也不硬切，
    #   用户可在配置 CHAPTER_SPLIT_PATTERNS 中补自定义正则后重试。
    # - True：切章失败时仍按字数硬切（标题用「第 N 部分」占位），保留旧行为。
    CHAPTER_SPLIT_HARD_FALLBACK_ENABLED: bool = False

    # 硬切每块字符上限（CHAPTER_SPLIT_HARD_FALLBACK_ENABLED=True 时生效）
    CHAPTER_SPLIT_HARD_FALLBACK_MAX_CHARS: int = 30_000


settings = Settings()


# ============================================================
# 启动期迁移：如果 PROVIDERS_CONFIG 为空（首次启动 / 老配置），
# 将扁平 TTS_API_KEY / LLM_API_KEY 等一次性迁移到厂商结构。
# 老字段保留为兜底（兼容未来回滚 / 未迁移代码）。
# ============================================================
def _migrate_legacy_providers() -> None:
    if settings.PROVIDERS_CONFIG:
        return  # 已有结构化配置，跳过迁移
    providers = []
    for tmpl in DEFAULT_PROVIDERS_TEMPLATE:
        prov = dict(tmpl)
        prov["models"] = [dict(m) for m in tmpl["models"]]
        providers.append(prov)
    # 注入 miniMax 老凭据
    for prov in providers:
        if prov["id"] == "minimax":
            prov["api_key"] = settings.TTS_API_KEY or settings.LLM_API_KEY
            prov["base_url"] = settings.TTS_BASE_URL or settings.LLM_BASE_URL
            # 找到 LLM 模型
            for m in prov["models"]:
                if m["kind"] == "llm":
                    m["id"] = settings.LLM_MODEL_PRO or "MiniMax-M3"
    # 注入 doubao 老凭据（如有）
    for prov in providers:
        if prov["id"] == "doubao":
            prov["enabled"] = bool(settings.DOUBAO_AK)
            if settings.DOUBAO_AK:
                prov["api_key"] = settings.DOUBAO_AK
    payload = {
        "providers": providers,
        "active": {
            "tts": {"provider_id": "minimax", "model_id": "MiniMax-speech-01"},
            "llm": {"provider_id": "minimax", "model_id": settings.LLM_MODEL_PRO or "MiniMax-M3"},
        },
    }
    settings.PROVIDERS_CONFIG = _json.dumps(payload, ensure_ascii=False)


_migrate_legacy_providers()


# ============================================================
# 厂商查询助手
# ============================================================
def _parse_providers_config() -> dict[str, Any]:
    """解析 PROVIDERS_CONFIG JSON；解析失败回退空结构。"""
    try:
        cfg = _json.loads(settings.PROVIDERS_CONFIG or "{}")
        if not isinstance(cfg, dict):
            return {"providers": [], "active": {"tts": {}, "llm": {}}}
        cfg.setdefault("providers", [])
        cfg.setdefault("active", {"tts": {}, "llm": {}})
        return cfg
    except Exception:
        return {"providers": [], "active": {"tts": {}, "llm": {}}}


def list_providers() -> list[dict[str, Any]]:
    """返回所有已配置的厂商（含未启用）。"""
    return list(_parse_providers_config().get("providers", []))


def get_provider(provider_id: str) -> dict[str, Any] | None:
    for p in list_providers():
        if p.get("id") == provider_id:
            return p
    return None


def get_active(kind: str) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """返回 (provider_id, model_id, provider_dict)；找不到则 (None, None, None)。
    kind ∈ {"tts", "llm"}。
    """
    cfg = _parse_providers_config()
    active = cfg.get("active", {}).get(kind, {}) or {}
    pid = active.get("provider_id") or (
        settings.ACTIVE_TTS_PROVIDER if kind == "tts" else settings.ACTIVE_LLM_PROVIDER
    )
    mid = active.get("model_id") or (
        settings.ACTIVE_TTS_MODEL if kind == "tts" else settings.ACTIVE_LLM_MODEL
    )
    prov = get_provider(pid) if pid else None
    return pid, mid, prov


def get_active_tts_provider() -> dict[str, Any] | None:
    _, _, prov = get_active("tts")
    return prov


def get_active_llm_provider() -> dict[str, Any] | None:
    _, _, prov = get_active("llm")
    return prov


def save_providers_config(cfg: dict[str, Any]) -> None:
    """原样写回 PROVIDERS_CONFIG（前端 PUT /settings 入口调用）。

    P2 #14：同步落盘到 data/providers_config.json，服务重启后回填，
    避免「重启后丢失自定义 API Key / base_url」。
    """
    settings.PROVIDERS_CONFIG = _json.dumps(cfg, ensure_ascii=False)
    try:
        import os as _os
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = settings.DATA_DIR / "providers_config.json"
        # 文件权限 600：包含明文 api_key，避免被同机其他用户读取
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            _json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            _os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(path)
    except Exception as e:
        # 落盘失败只警告；下次重启还是会回退 .env 默认
        import logging
        logging.getLogger(__name__).warning(
            f"[providers_config] 落盘失败: {type(e).__name__}: {e}"
        )


def load_providers_config_from_disk() -> bool:
    """P2 #14：启动时从 data/providers_config.json 回填到 PROVIDERS_CONFIG。

    文件存在 + 解析成功 + 是 dict → 替换 settings.PROVIDERS_CONFIG；
    其它情况返回 False（保持 _migrate_legacy_providers 推出来的初始结构）。
    """
    try:
        path = settings.DATA_DIR / "providers_config.json"
        if not path.is_file():
            return False
        raw = path.read_text(encoding="utf-8")
        parsed = _json.loads(raw)
        if not isinstance(parsed, dict) or "providers" not in parsed:
            return False
        settings.PROVIDERS_CONFIG = _json.dumps(parsed, ensure_ascii=False)
        import logging
        logging.getLogger(__name__).info(
            f"[providers_config] 从 {path} 回填（providers={len(parsed.get('providers', []))}）"
        )
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"[providers_config] 从磁盘回填失败: {type(e).__name__}: {e}"
        )
        return False


# 键名白名单：仅这部分键可被 PUT /settings 持久化；
# 其它键（如 ENV / SECRET / API Key 等）即使前端 PUT 也不写盘。
_PERSISTABLE_EDITABLE_KEYS: set[str] = set()  # 在 _init_persistable_keys() 里填充


def _init_persistable_keys() -> None:
    """扫描 _EDITABLE_SETTINGS（来自 routes 模块），把 int / str / bool / list[str] /
    json 类的非敏感键加入白名单。运行时配置由 routes._EDITABLE_SETTINGS 集中维护。
    """
    try:
        from ..api.routes import _EDITABLE_SETTINGS  # type: ignore
    except Exception:
        return
    for key, info in _EDITABLE_SETTINGS.items():
        typ = info[0] if isinstance(info, tuple) and info else None
        if typ in ("int", "str", "bool", "list[str]", "json"):
            _PERSISTABLE_EDITABLE_KEYS.add(key)


def save_runtime_settings_to_disk(updates: dict[str, Any]) -> dict[str, Any]:
    """P2 #14：把 PUT /settings 的 updates 持久化到 data/runtime_settings.json。

    仅白名单内的键被落盘；敏感字段（带 key/secret/token/password 的）一律忽略。
    返回 {"saved": [...], "skipped": [...]}。
    """
    import logging
    logger = logging.getLogger(__name__)
    saved: list[str] = []
    skipped: list[str] = []
    try:
        path = settings.DATA_DIR / "runtime_settings.json"
        # 读旧
        old: dict[str, Any] = {}
        if path.is_file():
            try:
                old = _json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(old, dict):
                    old = {}
            except Exception:
                old = {}
        # 过滤敏感 + 白名单
        for key, val in updates.items():
            if not isinstance(key, str):
                skipped.append(f"{key}(非字符串键)")
                continue
            kl = key.lower()
            if any(s in kl for s in (
                "key", "secret", "token", "password", "passwd",
            )):
                skipped.append(f"{key}(敏感字段不持久化)")
                continue
            if key not in _PERSISTABLE_EDITABLE_KEYS:
                skipped.append(f"{key}(不在白名单)")
                continue
            old[key] = val
            saved.append(key)
        # 写
        if saved:
            import os as _os
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                _json.dumps(old, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                _os.chmod(tmp, 0o600)
            except Exception:
                pass
            tmp.replace(path)
            logger.info(f"[runtime_settings] 持久化 {len(saved)} 个键到 {path}")
    except Exception as e:
        logger.warning(f"[runtime_settings] 持久化失败: {type(e).__name__}: {e}")
    return {"saved": saved, "skipped": skipped}


def load_runtime_settings_from_disk() -> int:
    """P2 #14：启动时从 data/runtime_settings.json 恢复 settings.* 字段。

    返回恢复的键数。
    """
    import logging
    logger = logging.getLogger(__name__)
    try:
        path = settings.DATA_DIR / "runtime_settings.json"
        if not path.is_file():
            return 0
        raw = path.read_text(encoding="utf-8")
        data = _json.loads(raw)
        if not isinstance(data, dict):
            return 0
        n = 0
        for key, val in data.items():
            if not isinstance(key, str) or not hasattr(settings, key):
                continue
            # 类型校验：尝试转换（与 routes.update_settings 同样的转换规则简化版）
            try:
                current = getattr(settings, key)
                if isinstance(current, bool):
                    if isinstance(val, str):
                        val = val.lower() in ("1", "true", "yes", "on")
                    val = bool(val)
                elif isinstance(current, int):
                    val = int(val)
                elif isinstance(current, str):
                    val = str(val)
                elif isinstance(current, list):
                    if isinstance(val, str):
                        val = [
                            ln.strip() for ln in val.splitlines()
                            if ln.strip()
                        ]
                    elif not isinstance(val, list):
                        raise ValueError("list expected")
                setattr(settings, key, val)
                n += 1
            except Exception as e:
                logger.warning(
                    f"[runtime_settings] 跳过 {key}: {type(e).__name__}: {e}"
                )
        logger.info(f"[runtime_settings] 从 {path} 恢复 {n} 个键")
        return n
    except Exception as e:
        logger.warning(f"[runtime_settings] 从磁盘恢复失败: {type(e).__name__}: {e}")
        return 0


def is_provider_enabled(provider_id: str) -> bool:
    p = get_provider(provider_id)
    return bool(p and p.get("enabled") and p.get("api_key"))
