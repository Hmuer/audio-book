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

    # 角色识别切片大小（字符）：整本小说角色识别时，按该大小切块后
    # **全量串行**调用 LLM（不抽样、不截断），最后对所有切块结果做一次
    # 跨切块合并 + dedup。50k 是 MiniMax 角色识别 prompt 的比较稳妥上限，
    # 既保证上下文足够又不会因超长输出导致 JSON 解析失败。
    # 估算：3000 字/章 × 1000 章 = 300 万字 → 60 个切片 × 串行 10s/个 ≈ 10 分钟
    LLM_CHAR_EXTRACT_SLICE_SIZE: int = 50_000

    # 注意：角色识别 **已移除"前 N 字抽样"策略**（LLM_CHAR_EXTRACT_LIMIT 已不再使用）。
    # 现在无论小说多长，都会按 SLICE_SIZE 全量切片跑完后合并去重；
    # 否则后半本书的新角色会被整本书漏掉，后续对白归属全部失败。

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
