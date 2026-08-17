from __future__ import annotations
import logging
import os
import re
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response

from .core.config import settings
from .db.session import init_db
from .api.routes import router as api_router, auth_router, public_router
from .services.auth import seed_admin_user

# ---------------------------------------------------------------------------
# 日志配置：同时输出到 stdout 和文件（RotatingFileHandler，10MB × 5 份）
# - LOG_FILE="" 时只走 stdout，不落盘
# - 文件 UTF-8，避免中文字符 ??? 替换
# - 解决 502/prepare 异常时"关掉终端日志就丢"的问题
# ---------------------------------------------------------------------------
_LOG_FMT = logging.Formatter(
    "%(asctime)s | %(levelname)-5s | %(name)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_log_level = getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO)

# 根 logger：先清掉 uvicorn/pytest 可能预挂的 handler，避免重复行
_root = logging.getLogger()
for _h in list(_root.handlers):
    _root.removeHandler(_h)
_root.setLevel(_log_level)

# 1) stdout handler
_stream_h = logging.StreamHandler()
_stream_h.setLevel(_log_level)
_stream_h.setFormatter(_LOG_FMT)
_root.addHandler(_stream_h)

# 2) 文件 handler（默认 ./data/logs/app.log，10MB × 5 份滚动）
if settings.LOG_FILE:
    try:
        _log_path = Path(settings.LOG_FILE)
        # 相对路径相对进程 CWD 解析；父目录不存在则自动创建
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _file_h = RotatingFileHandler(
            filename=str(_log_path),
            maxBytes=settings.LOG_MAX_BYTES,
            backupCount=settings.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        _file_h.setLevel(_log_level)
        _file_h.setFormatter(_LOG_FMT)
        _root.addHandler(_file_h)
        # 用 print 不走 logging，避免首行自己被过滤；给部署者一个明确指向
        print(
            f"[logger] 文件日志已启用: {str(_log_path)} "
            f"(level={logging.getLevelName(_log_level)}, "
            f"maxBytes={settings.LOG_MAX_BYTES}, backupCount={settings.LOG_BACKUP_COUNT})",
            flush=True,
        )
    except Exception as _e:  # 权限/路径不可写时，不能把整个 app 拖崩
        print(f"[logger][WARN] 启用文件日志失败，回退仅 stdout: {_e}", flush=True)

logger = logging.getLogger("novel-tts")

# 屏蔽 httpx 内部 INFO 级别的请求日志（我们自己会在 provider 层打更有上下文的日志）
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("DB initialized")
    # 启动时确保默认 admin 账号存在
    await seed_admin_user()
    # 启动 prepare 的启动恢复 + 看门狗：
    #   - 3s 后扫 DB 中 status=preparing 的项目，从 checkpoint 自动恢复
    #     （解决服务重启 / uvicorn reload / 杀进程后 status 卡死 preparing）
    #   - 15s 看门狗轮询：progress.updated_at 超过 PREPARE_STUCK_MINUTES 未更新，
    #     视为卡死自动恢复
    from .services.project import ensure_prepare_watchdog_started
    ensure_prepare_watchdog_started()
    yield


app = FastAPI(
    title="AI 有声小说生成器",
    version="1.0.0",
    lifespan=lifespan,
)

# 大文件下载 / 流式传输：告诉前端反向代理（Nginx / Caddy）
# "不要缓冲整个响应再发"。代理默认会等整个 body 写完才 flush，
# 下载大 ZIP（可能 300MB+/整本书）时代理长时间不回客户端字节，
# 客户端 / CDN 先认为超时断开，浏览器就会看到 502，哪怕 uvicorn 没挂。
# 命中以下路径时统一设置响应头：
#   - download-all      : 整包 ZIP 下载
#   - chapters/*/download: 单章 MP3 下载
#   - /media/*          : 静态 MP3 试听 / <audio> 流式
_DOWNLOAD_PATH_RE = re.compile(
    r"(download-all$|chapters/\d+/download$|^/media/)"
)


@app.middleware("http")
async def _disable_proxy_buffering_for_downloads(
    request: Request, call_next
) -> Response:
    resp: Response = await call_next(request)
    if _DOWNLOAD_PATH_RE.search(request.url.path):
        # X-Accel-Buffering=no 对 Nginx 生效；Caddy 用类似的 disable_buffering
        # 也会尊重该头；对直连客户端没副作用。
        resp.headers.setdefault("X-Accel-Buffering", "no")
        # 额外显式给 Transfer-Encoding 放行（chunked），
        # 避免某些代理强制 Content-Length 后再发。
        if "Content-Length" not in resp.headers:
            resp.headers.setdefault("Transfer-Encoding", "chunked")
    return resp


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
