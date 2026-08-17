from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # API Keys
    TTS_API_KEY: str
    TTS_BASE_URL: str = "https://api.minimaxi.com/v1"
    LLM_API_KEY: str
    LLM_BASE_URL: str = "https://api.minimaxi.com/v1"
    LLM_MODEL_PRO: str = "MiniMax-M3"
    # fast 模型也用 M3：M2.x 的 thinking 无法关闭，会偶发陷入循环耗尽 token；
    # M3 可通过 thinking:{type:disabled} 真正关闭 thinking，反而更快更稳。
    LLM_MODEL_FAST: str = "MiniMax-M3"

    # Server
    ENV: str = "dev"  # dev / test / prod / stage
    BIND_HOST: str = "127.0.0.1"
    PORT: int = 8000

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

    # 角色识别：每 50k 汉字切片，避免前 30 万字采样漏后期角色。
    # 一本 1000 万字小说 ≈ 200 切片 × ~20s/切片 ≈ 1h 角色识别。
    CHAR_EXTRACT_SLICE_CHARS: int = 50_000
    # 单切片识别失败的重试次数（provider 层另有 3 次 HTTP 重试，这里是业务层切片级重试）
    CHAR_EXTRACT_RETRIES: int = 0

    # 对白归属批大小：14 章/批。按 3000 字/章 × 14 ≈ 4.2 万字符，
    # 加上 system prompt + few shot 总共 ~4.5 万，远小于 M3 的 1M 上下文。
    # 用 M2.7-highspeed 时更小的上下文（204.8k 字符），也足够。
    DIALOGUE_BATCH_CHAPTERS: int = 14
    # 对白归属批并发：同时在飞的批数量。LLM_MAX_CONCURRENCY=1 时这里多高都
    # 是串行，但设 >1 可以允许"准备下一批输入"与"上一批 LLM 调用"的流水线重叠。
    DIALOGUE_BATCH_CONCURRENCY: int = 2
    # 对白归属单批 LLM 调用失败后的业务层重试次数（0=不重试，provider 层另有兜底 3 次）
    DIALOGUE_BATCH_RETRY_COUNT: int = 2

    # TTS 并发限流（全局，段级）：同时最多 N 个 TTS synthesize 调用在飞。
    # 现在已经改成 **段级** semaphore（不是"每章并发"）。
    # MiniMax TTS 官方 RPM 限制较低，默认 50 段并发是保守值；
    # 如果遇到 429 rate limit exceeded(RPM)，可继续下调。
    TTS_MAX_CONCURRENCY: int = 50
    # TTS 分钟级 RPM 限流：60 秒窗口内最多 N 次 t2a_v2 请求。
    # MiniMax 按量套餐 RPM 限制较严，默认 15/分钟 是保守值；
    # 遇到 429 自动退避重试，避免整章失败。
    TTS_RPM_LIMIT: int = 15
    # TTS 段缓存：内存 LRU 上限（条）；超上限淘汰最旧。
    # 注：磁盘缓存不限制大小（AUDIO_DIR/_seg_cache/），重启后仍可命中。
    TTS_SEGMENT_CACHE_MAX_ENTRIES: int = 20_000
    # TTS 段缓存磁盘过期天数（启动 GC 时 mtime 超过此天数字段被删）
    TTS_SEGMENT_CACHE_TTL_DAYS: int = 30
    # TTS 段缓存磁盘总大小上限（GB）；启动 GC 超上限时按 mtime 从旧到新删除
    TTS_SEGMENT_CACHE_MAX_SIZE_GB: int = 20

    # Build running 超时（小时）：如果 start_build 命中的 running build
    # started_at 距离现在超过该值，认为是被 kill 的孤儿，直接改 status 回 queued
    # 并起新 worker。避免"重启后端后 Build 永远合成中"。
    BUILD_RUNNING_TIMEOUT_HOURS: int = 6

    # 切章正则模式：可在 .env 里用 CHAPTER_SPLIT_PATTERNS 覆盖扩展
    # 每个模式独立按行匹配，命中任意模式算作章标题行。
    # 默认覆盖中文小说常见格式：第x章/回/节/卷/集/话/篇、序、楔子、番外、后记、尾声、结局
    CHAPTER_SPLIT_PATTERNS: list[str] = [
        r"^\s*第[\s]*[零○一二三四五六七八九十百千万\d]+[\s]*[章节回节卷集话篇部卷][：:.．]?.*",
        r"^\s*第[\s]*[零○一二三四五六七八九十百千万\d]+[\s]*部分[：:.．]?.*",
        r"^\s*(序(章|言|之章)?|楔子|引子|卷首语|前记|前言|自序|开篇)[：:.．]?.*",
        r"^\s*(番外|外传|后记|尾声|终章|结局|大结局|完结(记|章)?|终卷|终曲)[：:.．]?.*",
        r"^\s*[上中下终末尾]+卷[：:.．]?.*",
    ]

    # 切章失败时是否回退到 LLM？False=直接抛错并提醒用户，避免 LLM 误切。
    CHAPTER_SPLIT_FALLBACK_LLM: bool = False

    # Auth
    JWT_SECRET: str = "dev-secret-change-me-in-prod"
    JWT_EXPIRE_HOURS: int = 24 * 7  # 默认 7 天过期
    DISABLE_AUTH: bool = False
    SEED_ADMIN_USER: str = "admin"
    SEED_ADMIN_PASS: str = "admin"
    # 生产安全：ENV=prod 时检测是否仍用默认 JWT_SECRET / 默认 admin 密码，
    # 是则 WARN 或直接阻止启动（PROD_STRICT_SECURITY=true 时 BLOCK）
    PROD_STRICT_SECURITY: bool = True

    # 日志：路径相对 DATA_DIR，按大小轮转
    LOG_FILE: str = "logs/app.log"
    LOG_MAX_BYTES: int = 10 * 1024 * 1024  # 10MB
    LOG_BACKUP_COUNT: int = 5

    # prepare_project 看门狗间隔/卡住阈值
    WATCHDOG_INTERVAL_SEC: float = 15.0
    WATCHDOG_STUCK_MINUTES: float = 10.0
    WATCHDOG_RECOVERY_CONCURRENCY: int = 1


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
