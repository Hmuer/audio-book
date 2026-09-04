from __future__ import annotations
import logging
import os
import re
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, Response

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
    # P2 #14：从 data/providers_config.json 和 data/runtime_settings.json 回填
    # settings，避免「重启后丢失前端通过 PUT /providers 和 PUT /settings 写入的配置」。
    # 必须在 init_db / startup 钩子之前先跑。
    try:
        from .core.config import (
            load_providers_config_from_disk,
            load_runtime_settings_from_disk,
            _init_persistable_keys,
        )
        _init_persistable_keys()
        load_providers_config_from_disk()
        load_runtime_settings_from_disk()
    except Exception as e:
        logger.warning(f"[startup] 配置回填失败: {type(e).__name__}: {e}")
    await init_db()
    logger.info("DB initialized")
    # 启动时确保默认 admin 账号存在
    await seed_admin_user()
    # P1 #5：把 owner_user_id IS NULL 的孤儿项目一次性归属到 admin，
    # 让资源归属过滤立刻生效（不再泄露给"任意登录用户"）。
    try:
        from sqlalchemy import select
        from .db.session import get_session_factory
        from .db.models import User
        from .services.ownership import claim_orphan_projects
        factory = get_session_factory()
        async with factory() as s:
            admin = (
                await s.execute(
                    select(User).where(User.username == settings.SEED_ADMIN_USER)
                )
            ).scalar_one_or_none()
            if admin:
                n = await claim_orphan_projects(s, admin)
                if n:
                    logger.info(f"[ownership] 启动时已将 {n} 个孤儿项目归属到 admin")
    except Exception as e:
        logger.error(f"[ownership] 孤儿项目归属失败: {type(e).__name__}: {e}")
    # 启动 prepare 的启动恢复 + 看门狗：
    #   - 3s 后扫 DB 中 status=preparing 的项目，从 checkpoint 自动恢复
    #     （解决服务重启 / uvicorn reload / 杀进程后 status 卡死 preparing）
    #   - 15s 看门狗轮询：progress.updated_at 超过 PREPARE_STUCK_MINUTES 未更新，
    #     视为卡死自动恢复
    from .services.project import ensure_prepare_watchdog_started
    ensure_prepare_watchdog_started()
    # P1 #6：启动一次性媒体签名 token 过期清理后台任务
    try:
        from .services.media_sign import ensure_cleanup_started
        ensure_cleanup_started()
    except Exception as e:
        logger.error(f"[media_sign] 启动清理后台任务失败: {type(e).__name__}: {e}")
    # P1 #7：启动后台任务持久化（JobTask 表）的启动恢复 + 看门狗；
    # 把上次没跑完的孤儿任务自动 enqueue 一次，避免 UI 永远卡"合成中"。
    try:
        from .services.job_tasks import ensure_job_task_watchdog_started
        ensure_job_task_watchdog_started()
    except Exception as e:
        logger.error(f"[job_task] 启动 watchdog 失败: {type(e).__name__}: {e}")
    yield


app = FastAPI(
    title="AI 有声小说生成器",
    version="1.0.0",
    lifespan=lifespan,
    # 强制路由尾斜杠规范化：/api/projects/ → 307 → /api/projects
    # 避免前端 trailingSlash 或代理加斜杠导致 StaticFiles fallback 到 404
    redirect_slashes=True,
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
    # ---- 尾斜杠规范化（内部转发，不返回307）----
    # 背景：next.config.js trailingSlash + 某些代理/previewer 会强制
    # "/api/projects" → "/api/projects/"。而 FastAPI 路由注册是"/api/projects"，
    # 精确匹配失败后，StaticFiles mount 在 "/" 会吞掉该请求，返回 404 HTML。
    #
    # 处理策略：
    #   - 检测到请求路径以 "/" 结尾且属于 /api 或 /media 前缀，
    #     直接修改 scope["path"] 去掉末尾斜杠，让 call_next 走正确的 APIRouter。
    #     这样对前端是"同一个请求"，不会触发浏览器的 fetch redirect 安全限制。
    #   - 如果 path == "/" 不处理（首页要正常返回）。
    path = request.url.path
    if (
        len(path) > 1
        and path.endswith("/")
        and (path.startswith("/api/") or path.startswith("/media/"))
    ):
        clean = path.rstrip("/") or "/"
        # Starlette scope 可变对象；直接修改 path、raw_path、full_path
        # 让后续的 call_next 按规范化后的路径重新匹配路由。
        scope = request.scope
        raw_query = scope.get("query_string", b"")
        scope["path"] = clean
        scope["raw_path"] = clean.encode("utf-8")
        if raw_query:
            scope["full_path"] = (clean + "?" + raw_query.decode("latin-1")).encode("utf-8")
        else:
            scope["full_path"] = clean.encode("utf-8")
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

# /media -> audio files（鉴权 + Range 支持）
media_dir = Path(settings.AUDIO_DIR)
media_dir.mkdir(parents=True, exist_ok=True)


def _token_from_scope(scope: dict) -> str | None:
    """从 ASGI scope 提取 token：Authorization: Bearer > ?token=…。

    StaticFiles.get_response(scope) 没有 Request 对象；我们直接解析 scope['headers'] 与 query string。
    """
    # headers（bytes）
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            try:
                decoded = v.decode("latin", errors="replace")
            except Exception:
                continue
            scheme, _, value = decoded.partition(" ")
            if scheme.lower() == "bearer" and value:
                return value.strip()
    # query string
    qs = scope.get("query_string") or b""
    if qs:
        try:
            from urllib.parse import parse_qs
            params = parse_qs(qs.decode("utf-8", errors="replace"))
            tok_list = params.get("token") or []
            if tok_list and tok_list[0]:
                return tok_list[0].strip()
        except Exception:
            return None
    return None


# 自定义 StaticFiles 支持 Range（浏览器 <audio> 拖动）+ JWT 鉴权
class _RangedAuthStaticFiles(StaticFiles):
    """需要有效 JWT 才能访问的静态文件服务。

    校验方式： Authorization: Bearer <token> 或 ?token=<token>（后者供
    <audio src="...?token=..."> 等浏览器原生标签消费；前者供 fetch 用）。

    P1 #5 资源归属：除 JWT 校验外，从请求文件名反解 build_id，查 DB 拿到
    project 的 owner_user_id；当前登录用户必须是 owner 或是 admin 才能访问。
    文件名模式：
      - build_<build_id>_ch<NNNN>.mp3   → 单章 MP3
      - build_<build_id>_ch<NNNN>_failed.mp3
      - build_<build_id>_all.zip        → 整包 ZIP
    其它文件（seg_cache 等）只做 JWT 校验，不查 DB（性能优先）。

    目录解析：每次请求都重新读 settings.AUDIO_DIR，这样测试中通过
    monkeypatch 修改 settings.AUDIO_DIR 也能立即生效（无需重启进程）。
    """

    def lookup_path(self, path: str):  # type: ignore[override]
        """每次请求实时解析 settings.AUDIO_DIR，避免 mount 时锁死目录。"""
        import os as _os
        directory = _os.path.realpath(str(settings.AUDIO_DIR))
        joined = _os.path.join(directory, path)
        full_path = _os.path.realpath(joined)
        if _os.path.commonpath([full_path, directory]) != directory:
            return "", None
        try:
            return full_path, _os.stat(full_path)
        except (FileNotFoundError, NotADirectoryError):
            return "", None

    async def get_response(self, path, scope):  # type: ignore[override]
        from starlette.responses import JSONResponse

        # DISABLE_AUTH（调试模式）：直接放行，不要求 token
        if settings.DISABLE_AUTH:
            return await super().get_response(path, scope)

        token = _token_from_scope(scope)
        if not token:
            return JSONResponse(
                {"detail": "Missing token"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        username: str | None = None
        try:
            from .api.routes import _decode_token_for_static
            username = _decode_token_for_static(token, settings)
        except Exception as e:
            logger.warning(f"[media] 鉴权失败: {type(e).__name__}: {e}")
            return JSONResponse(
                {"detail": "Invalid token"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not username:
            return JSONResponse(
                {"detail": "Invalid token"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        # P1 #5：资源归属校验 —— 只对 build_* 命名的产出生效
        import re as _re
        import asyncio as _asyncio
        m = _re.match(
            r"build_(?P<build_id>[A-Za-z0-9]+)_(?:ch\d+(?:_failed)?\.mp3|all\.zip)$",
            path,
        )
        if m:
            bid = m.group("build_id")
            try:
                from .db.session import get_session_factory
                from .db.models import Build, Project, User
                from .services.ownership import is_admin_user
                from sqlalchemy import select

                async def _check_owner() -> bool:
                    factory = get_session_factory()
                    async with factory() as s:
                        user = (
                            await s.execute(
                                select(User).where(User.username == username)
                            )
                        ).scalar_one_or_none()
                        if not user:
                            return False
                        if is_admin_user(user):
                            return True
                        b = await s.get(Build, bid)
                        if not b:
                            return False
                        p = await s.get(Project, b.project_id)
                        if not p:
                            return False
                        # 自己的；或孤儿池 owner=0（仅 admin；上面已 return True 放过 admin）
                        return p.owner_user_id == user.id

                allowed = await _check_owner()
                if not allowed:
                    return JSONResponse(
                        {"detail": "无权访问该媒体资源"}, status_code=403,
                    )
            except Exception as e:
                # DB 故障保守拒绝，避免越权
                logger.error(f"[media] 归属校验异常: {type(e).__name__}: {e}")
                return JSONResponse(
                    {"detail": "media auth check failed"}, status_code=503,
                )

        return await super().get_response(path, scope)


app.mount("/media", _RangedAuthStaticFiles(directory=str(media_dir)), name="media")

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
