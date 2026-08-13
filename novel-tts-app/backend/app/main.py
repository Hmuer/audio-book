from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .core.config import settings
from .db.session import init_db
from .api.routes import router as api_router, auth_router, public_router
from .services.auth import seed_admin_user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("novel-tts")

# 屏蔽 httpx 内部 INFO 级别的请求日志（我们自己会在 provider 层打更有上下文的日志）
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("DB initialized")
    # 启动时确保默认 admin 账号存在
    await seed_admin_user()
    yield


app = FastAPI(
    title="AI 有声小说生成器",
    version="1.0.0",
    lifespan=lifespan,
)

# 路由挂载顺序：先 auth（无鉴权）→ public（无鉴权）→ 业务（强制 JWT）
app.include_router(auth_router)
app.include_router(public_router)
app.include_router(api_router)

# /media -> audio files
media_dir = Path(settings.AUDIO_DIR)
media_dir.mkdir(parents=True, exist_ok=True)

# 自定义 StaticFiles 支持 Range（浏览器 <audio> 拖动）
class _RangedStaticFiles(StaticFiles):
    pass

app.mount("/media", _RangedStaticFiles(directory=str(media_dir)), name="media")

# 前端 out 目录：先尝试相对 repo 根路径
FRONTEND_OUT_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent / "frontend" / "out",
    Path("./frontend/out").resolve(),
]
frontend_out: Path | None = None
for p in FRONTEND_OUT_CANDIDATES:
    if p.exists() and (p / "index.html").exists():
        frontend_out = p
        break

if frontend_out:
    logger.info(f"挂载前端静态目录: {frontend_out}")
    app.mount("/", StaticFiles(directory=str(frontend_out), html=True), name="frontend")
else:
    logger.warning(
        "前端构建产物未找到 (frontend/out/index.html)。"
        "请先 `cd frontend && npm install && npm run build`，或运行 start.sh。"
    )

    @app.get("/")
    async def root_missing():
        return {
            "message": "前端还未构建，请先运行 start.sh 或构建 frontend",
            "docs": "/docs",
        }
