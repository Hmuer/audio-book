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


settings = Settings()
