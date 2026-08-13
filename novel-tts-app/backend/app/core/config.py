from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )
    TTS_API_KEY: str
    TTS_BASE_URL: str = "https://api.minimaxi.com/v1"
    LLM_API_KEY: str
    LLM_BASE_URL: str = "https://api.minimaxi.com/v1"
    LLM_MODEL_PRO: str = "MiniMax-M3"
    LLM_MODEL_FAST: str = "MiniMax-M2.7-highspeed"

    # Server
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

    # 角色识别采样上限（字符）：整本小说角色识别时
    # - <= LLM_CHAR_EXTRACT_LIMIT：逐 50k 切片跑角色识别 + 跨切片合并去重（准确）
    # - >  LLM_CHAR_EXTRACT_LIMIT：只抽前 LLM_CHAR_EXTRACT_LIMIT 字跑一次角色识别
    #   （前 50 万字通常已出场大部分主要角色，足以覆盖整本的对白归属需求；
    #   这样 1000 章 × 2000 字 = 200 万字的书，从 40 次 LLM 降为 1 次）
    LLM_CHAR_EXTRACT_LIMIT: int = 500_000

    # TTS 并发限流（全局）：同时最多 N 个 TTS synthesize 调用在飞。
    # 无论开几个整本合成 worker / 单章合成，都共用同一个 semaphore 计数，
    # 防止多 worker 各自开 4 并发 → 实际并发叠加爆供应商 RPM 限制（429）。
    TTS_MAX_CONCURRENCY: int = 4

    # Auth (JWT)
    JWT_SECRET: str = "change-me-in-production-please-use-a-long-random-string"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXP_DAYS: int = 7
    SEED_ADMIN_USER: str = "admin"
    SEED_ADMIN_PASS: str = "admin"
    # 测试/本地调试用：DISABLE_AUTH=1 时所有 /api/* 不校验 token
    DISABLE_AUTH: bool = False


settings = Settings()
