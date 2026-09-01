from __future__ import annotations
import logging
import time as _time
import urllib.parse
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session_factory
from ..db.models import Build, BuildArtifact, Project, User
from ..ai.factory import get_tts, get_tts_by_voice_id
from ..core.config import settings
from ..services.auth import (
    authenticate,
    create_access_token,
    decode_token,
    get_user_by_username,
    LoginResp,
    UserInfo,
    change_password,
    login as auth_service_login,
)
from ..services.project import (
    create_project,
    import_file as project_import_file,
    import_text as project_import_text,
    trigger_prepare_project,
    get_project,
    list_projects,
    update_project,
    delete_project,
    get_project_chapters,
    get_project_chapter_detail,
    get_project_characters,
    update_character_voice,
    list_pronunciation_rules,
    create_pronunciation_rule,
    update_pronunciation_rule,
    delete_pronunciation_rule,
    ProjectResp,
    ProjectDetailResp,
    ProjectListItem,
    ProjectPrepareResp,
    ProjectPrepareTriggerResp,
    ChapterSummary,
    ChapterDetail,
    CharacterWithVoice,
    CharacterResp,
    PronunciationRule,
    PronunciationRuleInput,
)
from ..services.build import (
    start_build,
    get_build,
    list_builds,
    get_build_status,
    delete_build,
    cancel_build,
    retry_failed_build,
    _ensure_project_not_running,
    BuildResp,
    BuildDetailResp,
    BuildListItem,
    BuildStatusResp,
)
from ..services.book_split import strip_chapter_prefix

logger = logging.getLogger(__name__)

# 业务路由：所有 endpoint 默认强制 JWT 鉴权（dependencies 在 get_current_user 定义后追加）
router = APIRouter(prefix="/api", tags=["novel-tts"])

# auth 路由：无鉴权（登录本身不需要 token；me/change-password/logout 在 endpoint 内显式 Depends）
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])

# 公开路由：/health 仅用于运维健康检查，不要求登录
public_router = APIRouter(prefix="/api", tags=["public"])


async def get_db() -> AsyncSession:
    factory = get_session_factory()
    async with factory() as s:
        yield s


# =====================================================================
# 鉴权：JWT 依赖 + /api/auth/* 路由
# =====================================================================

# auto_error=False 让 401 由我们自己抛（带 WWW-Authenticate 头）
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = None,
) -> User:
    """
    JWT 鉴权依赖。所有需要登录的 /api/* 路由通过 Depends(get_current_user) 强制校验。
    DISABLE_AUTH=True 时直接放行（仅本地调试/测试用）。

    token 来源（优先级）：
      1. Authorization: Bearer <token> 头（前端 fetch 默认走这里）
      2. ?token=<jwt> 查询参数（浏览器 <a href download> / <audio src> 无法设头时的兜底）
    """
    if settings.DISABLE_AUTH:
        # 测试模式：放行。无 token 时返回一个虚拟 admin（保证非 None）
        user = await get_user_by_username(settings.SEED_ADMIN_USER)
        if not user:
            # 极端情况：DB 还没 seed，构造一个临时 User
            user = User(id=0, username="disabled-auth", password_hash="", is_active=True)
        return user

    # 解析原始 token：优先 header，回退到查询参数
    raw_token: str | None = None
    if creds and creds.scheme.lower() == "bearer":
        raw_token = creds.credentials
    elif token:
        raw_token = token
    if not raw_token:
        raise HTTPException(
            status_code=401,
            detail="未提供认证 token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    username = decode_token(raw_token)
    if not username:
        raise HTTPException(
            status_code=401,
            detail="token 无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await get_user_by_username(username)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="用户不存在或已禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# 现在 get_current_user 已定义，给业务 router 追加全局 dependencies
router.dependencies = [Depends(get_current_user)]


def _decode_token_for_static(raw_token: str, settings) -> None:
    """供 StaticFiles 调用的精简鉴权：校验 token 签名/有效性，失败抛异常。

    与 get_current_user 共享 decode_token，行为一致；唯一区别是不返回 User 对象
    （/media 只判断"是不是当前用户的请求"，无需加载 User 进 ORM）。
    """
    if settings.DISABLE_AUTH:
        return
    username = decode_token(raw_token)
    if not username:
        raise HTTPException(
            status_code=401,
            detail="token 无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------- Auth Requests ----------

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


@auth_router.post("/login", response_model=LoginResp)
async def api_auth_login(req: LoginRequest, request: Request):
    """登录：用户名 + 密码 → JWT。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/auth/login client={remote} username={req.username!r}"
    )
    try:
        login_resp = await auth_service_login(req.username, req.password)
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/auth/login client={remote} "
            f"username={login_resp.user.username!r} "
            f"must_change_password={login_resp.must_change_password} total_ms={elapsed_ms}"
        )
        return login_resp
    except HTTPException:
        raise
    except ValueError as e:
        msg = str(e)
        # prod 安全拦截（默认 admin/admin 默认密码 → 禁登）这类明确"权限/策略拒绝"，返回 403；
        # 其余（用户名/密码错误等鉴权失败）返回 401（旧契约）
        if any(x in msg for x in (
            "生产环境需先修改默认",
            "STRICT_PROD_SECURITY",
            "请联系管理员",
            "默认 admin 密码",
        )):
            logger.warning(
                f"[HTTP] 403 /api/auth/login client={remote} "
                f"username={req.username!r} -> {e}"
            )
            raise HTTPException(403, msg)
        logger.warning(
            f"[HTTP] 401 /api/auth/login client={remote} "
            f"username={req.username!r} -> {e}"
        )
        raise HTTPException(401, msg)
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/auth/login -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"登录失败: {type(e).__name__}: {e}")


@auth_router.get("/me", response_model=UserInfo)
async def api_auth_me(current: User = Depends(get_current_user)):
    """返回当前登录用户信息（用于前端刷新页面后校验 token 有效性）。"""
    return UserInfo(
        id=current.id,
        username=current.username,
        is_active=current.is_active,
        created_at=current.created_at.isoformat() if current.created_at else None,
    )


@auth_router.post("/change-password")
async def api_auth_change_password(
    req: ChangePasswordRequest,
    current: User = Depends(get_current_user),
):
    """修改自己的密码（admin 首次登录强制改密会在此接口把 must_change_password 置 False）。"""
    try:
        res = await change_password(
            username=current.username,
            old_password=req.old_password,
            new_password=req.new_password,
            is_admin_self_change=True,
        )
        logger.info(f"[auth] 用户 {current.username!r} 修改了密码")
        return res
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"[auth] 改密失败 user={current.username!r} -> {type(e).__name__}: {e}")
        raise HTTPException(500, f"改密失败: {type(e).__name__}: {e}")


@auth_router.post("/logout")
async def api_auth_logout(current: User = Depends(get_current_user)):
    """
    无状态 JWT 注销：服务端不存黑名单（单机单用户场景没必要）。
    前端清掉本地 token 即可。这里只是占位返回 ok，方便前端统一调用。
    """
    return {"ok": True}


# ---------- Requests ----------

class PrepareRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=50000)
    enable_polish: bool = True


class SynthesizeRequest(BaseModel):
    job_id: str
    voice_assignments: dict[str, str] = Field(default_factory=dict)
    narrator_voice_id: str
    segment_overrides: dict[int, str] | None = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class TtsPreviewRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    voice_id: str
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


# ---------- Health & Voices ----------

@public_router.get("/health")
async def health():
    return {"status": "ok", "service": "novel-tts"}


@router.get("/voices")
async def api_list_voices(
    tts_provider: str | None = None,
    current: User = Depends(get_current_user),
):
    """列出可用音色（当前用户视角：含其可用 ICL 克隆音色）。"""
    user_id = getattr(current, "id", None)
    return await list_voices(tts_provider, icl_user_id=user_id)


async def list_voices(
    tts_provider: str | None = None,
    *,
    icl_user_id: int | None = None,
):
    """列出可用音色（服务层，可直接调用）。
    - tts_provider 未给：返回 minimax + doubao（两套并集，按 id 去重）。
    - tts_provider ∈ {minimax, doubao, icl}：仅返回对应厂商音色。
    - icl_user_id 给出时：附带该用户已训练可用的 ICL 克隆音色（icl:<clone_id>）。
    每条音色都带有 provider 字段，前端可据此分组。
    """
    import asyncio as _as_nc

    requested_providers: list[str]
    if tts_provider:
        requested_providers = [tts_provider.lower()]
    else:
        requested_providers = ["minimax", "doubao"]

    # ICL 音色只在请求 doubao/icl 或全量聚合时附带（icl: 走豆包合成通道）
    want_icl = icl_user_id is not None and (
        not tts_provider or tts_provider.lower() in ("doubao", "icl")
    )

    # 并发 list_voices（加速响应）
    async def _fetch(p: str) -> list[dict]:
        # 修复：原来用 `from backend.app.ai.factory import get_tts as _get_tts`，
        # 该写法依赖 PYTHONPATH 包含项目根目录，导致直接 `cd backend && uvicorn ...`
        # 启动后 GET /api/voices 抛 ModuleNotFoundError: No module named 'backend'。
        # 改用包内相对导入，避免对启动 cwd 的隐式依赖。
        from ..ai.factory import get_tts as _get_tts
        tts_inst = _get_tts(p)
        try:
            return await tts_inst.list_voices()
        except Exception as e:
            # 若某 provider 临时不可用（例如 Doubao 未填 AK，但 list_voices 是读 JSON，通常不报错）
            # 吞异常保证另一个仍可返回；真实请求再抛
            logger.warning(f"[voices] provider={p} list_voices 失败: {type(e).__name__}: {e}")
            return []

    tasks = [_fetch(p) for p in requested_providers]
    results = await _as_nc.gather(*tasks)
    merged: dict[str, dict] = {}
    for voices in results:
        for v in voices:
            vid = v.get("id")
            if not vid:
                continue
            merged[vid] = v

    if want_icl:
        try:
            from ..services.icl import icl_voices_for_user
            for v in await icl_voices_for_user(icl_user_id):  # type: ignore[arg-type]
                merged[v["id"]] = v
        except Exception:
            logger.exception("ICL 音色聚合失败（忽略）")

    final = list(merged.values())
    return {"voices": final, "count": len(final)}


# ---------- ICL 声音复刻（豆包 ICL 2.0） ----------


@router.post("/icl/voices")
async def api_icl_create_voice(
    request: Request,
    voice_name: str = Form(...),
    file: UploadFile = File(...),
    current: User = Depends(get_current_user),
):
    """上传 3~10 秒参考音频，创建 ICL 声音复刻训练任务。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    # 先按声明的大小拒绝（避免超大上传先整体读入内存）
    declared = request.headers.get("content-length")
    if declared and int(declared) > settings.ICL_MAX_AUDIO_BYTES + 64 * 1024:
        raise HTTPException(413, f"参考音频过大: {declared} > {settings.ICL_MAX_AUDIO_BYTES}")
    content = await file.read()
    logger.info(
        f"[HTTP] POST /api/icl/voices client={remote} user={current.username} "
        f"name={voice_name!r} filename={file.filename} size={len(content)}"
    )
    if not content:
        raise HTTPException(400, "参考音频为空")
    if len(content) > settings.ICL_MAX_AUDIO_BYTES:
        raise HTTPException(413, f"参考音频过大: {len(content)} > {settings.ICL_MAX_AUDIO_BYTES}")
    try:
        from ..services.icl import start_icl_training
        resp = await start_icl_training(
            user_id=current.id,
            voice_name=voice_name,
            audio_bytes=content,
            filename=file.filename,
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(f"[HTTP] 200 /api/icl/voices task={resp['task_id'][:12]}... total_ms={elapsed_ms}")
        return resp
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"[HTTP] 500 /api/icl/voices -> {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(500, f"创建训练任务失败: {type(e).__name__}: {e}")


@router.get("/icl/voices")
async def api_icl_list_voices(
    current: User = Depends(get_current_user),
):
    """列出当前用户的 ICL 训练任务（含进行中/成功/失败）。"""
    try:
        from ..services.icl import list_icl_tasks
        return {"tasks": await list_icl_tasks(current.id)}
    except Exception as e:
        logger.error(f"[HTTP] 500 /api/icl/voices -> {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(500, f"查询失败: {type(e).__name__}: {e}")


@router.get("/icl/voices/{task_id}")
async def api_icl_get_voice(
    task_id: str,
    current: User = Depends(get_current_user),
):
    """任务详情（进行中会顺带刷新一次豆包侧状态；归属校验在刷新之前）。"""
    try:
        from ..services.icl import get_icl_task
        resp = await get_icl_task(task_id, current.id)
        if resp is None:
            raise HTTPException(404, f"任务不存在或不属于当前用户: {task_id}")
        return resp
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/icl/voices/{task_id} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"查询失败: {type(e).__name__}: {e}")


@router.delete("/icl/voices/{task_id}")
async def api_icl_delete_voice(
    task_id: str,
    current: User = Depends(get_current_user),
):
    """删除训练任务（含参考音频）。"""
    try:
        from ..services.icl import delete_icl_task
        ok = await delete_icl_task(current.id, task_id)
        if not ok:
            raise HTTPException(404, f"任务不存在或不属于当前用户: {task_id}")
        return {"ok": True, "task_id": task_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[HTTP] 500 DELETE /api/icl/voices/{task_id} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"删除失败: {type(e).__name__}: {e}")


# ---------- Chapter & Book 路由已移除（项目制统一入口：/api/projects/*）----------
# 已删除：
#   POST /api/chapter/prepare        → 旧单章模式（移除）
#   POST /api/chapter/synthesize     → 旧单章模式（移除）
#   POST /api/book/upload            → 旧整本模式（移除）
#   POST /api/book/prepare           → 旧整本模式（移除）
#   POST /api/book/synthesize        → 旧整本模式（移除）
#   GET  /api/book/{job_id}/status   → 旧整本模式（移除）
#   GET  /api/book/{job_id}/download-all → 旧整本模式（移除）
#   GET  /api/book/{job_id}/chapters/{idx}/download → 旧整本模式（移除）


@router.post("/tts/preview")
async def api_tts_preview(
    req: TtsPreviewRequest,
    request: Request,
):
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/tts/preview client={remote} "
        f"voice={req.voice_id} text_len={len(req.text)} speed={req.speed}"
    )
    try:
        # 按音色命名空间前缀路由厂商（minimax: → MiniMax；doubao:/icl: → 豆包）
        tts = get_tts_by_voice_id(req.voice_id)
        audio_dir = Path(settings.AUDIO_DIR)
        audio_dir.mkdir(parents=True, exist_ok=True)
        import uuid as _uuid
        fname = f"preview_{_uuid.uuid4().hex[:10]}.mp3"
        fpath = str(audio_dir / fname)
        path, dur_ms = await tts.synthesize_to_file(
            req.text, req.voice_id, fpath, speed=req.speed
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/tts/preview voice={req.voice_id} "
            f"dur_ms={dur_ms} total_ms={elapsed_ms}"
        )
        return {
            "audio_filename": fname,
            "audio_url": f"/media/{fname}",
            "duration_ms": dur_ms,
        }
    except Exception as e:
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.error(
            f"[HTTP] 500 /api/tts/preview client={remote} "
            f"voice={req.voice_id} total_ms={elapsed_ms} -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"TTS 失败: {type(e).__name__}: {e}")


# =====================================================================
# 项目制 API（新架构：Project → Build → BuildArtifact）
# =====================================================================


# ---------- Requests ----------

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)


class ImportTextRequest(BaseModel):
    text: str = Field(..., min_length=1)
    filename_hint: str = Field(default="pasted_text.txt", max_length=256)


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, max_length=256)
    description: str | None = None
    tags: str | None = Field(default=None, max_length=256)
    default_narrator_voice_id: str | None = None
    default_speed: float | None = Field(default=None, ge=0.5, le=2.0)
    cover_color: str | None = None


class UpdateCharacterVoiceRequest(BaseModel):
    voice_id: str | None = None  # None 表示清除


class StartBuildRequest(BaseModel):
    voice_assignments: dict[str, str] = Field(default_factory=dict)
    narrator_voice_id: str = ""
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    # 多厂商 & 构建模式（Task 4/8 新增）
    mode: str = Field(default="classic")  # classic | multicast
    tts_provider: str | None = Field(default=None)


class CancelBuildRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class RetryFailedBuildRequest(BaseModel):
    """失败章重试。默认仅重跑失败章（失败章列表来源：failed_chapters_json 或 Artifact.status==failed）。"""
    force_restart_failed_only: bool = True


def _safe_download_name_build(book_title: str | None, build_id: str, ext: str) -> str:
    """构造 build 下载文件名（中文 + fallback 安全），ext 带点。"""
    base = (book_title or "").strip() or f"有声书_{build_id[:8]}"
    for ch in '\\/:*?"<>|\r\n\t':
        base = base.replace(ch, "_")
    base = base[:100].strip() or f"有声书_{build_id[:8]}"
    return f"{base}{ext}"


# ---------- 项目 CRUD ----------

@router.post("/projects", response_model=ProjectResp)
async def api_create_project(req: CreateProjectRequest, request: Request):
    """创建项目（status=draft）。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(f"[HTTP] POST /api/projects client={remote} name={req.name!r}")
    try:
        resp = await create_project(req.name)
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/projects project_id={resp.project_id[:8]}... total_ms={elapsed_ms}"
        )
        return resp
    except Exception as e:
        logger.error(f"[HTTP] 500 /api/projects -> {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(500, f"创建项目失败: {type(e).__name__}: {e}")


@router.get("/projects", response_model=list[ProjectListItem])
async def api_list_projects(request: Request):
    """项目列表。"""
    try:
        return await list_projects()
    except Exception as e:
        logger.error(f"[HTTP] 500 /api/projects -> {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(500, f"列表查询失败: {type(e).__name__}: {e}")


@router.get("/projects/{project_id}", response_model=ProjectDetailResp)
async def api_get_project(project_id: str, request: Request):
    """项目详情（含 chapters 摘要 + characters + 最近 build）。"""
    try:
        return await get_project(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"详情查询失败: {type(e).__name__}: {e}")


@router.patch("/projects/{project_id}", response_model=ProjectResp)
async def api_update_project(
    project_id: str,
    req: UpdateProjectRequest,
    request: Request,
):
    """更新项目名称/备注/标签/配置。"""
    try:
        return await update_project(
            project_id,
            name=req.name,
            description=req.description,
            tags=req.tags,
            default_narrator_voice_id=req.default_narrator_voice_id,
            default_speed=req.default_speed,
            cover_color=req.cover_color,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 PATCH /api/projects/{project_id} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"更新失败: {type(e).__name__}: {e}")


@router.delete("/projects/{project_id}")
async def api_delete_project(project_id: str, request: Request):
    """删除项目（级联删除 DB + 磁盘文件）。项目正在合成（Build 运行中）时拒绝删除。"""
    try:
        await _ensure_project_not_running(project_id, action="删除项目")
        await delete_project(project_id)
        return {"ok": True, "project_id": project_id}
    except ValueError as e:
        # 来自 _ensure_project_not_running 的"项目正在运行"错误 → 409 冲突
        raise HTTPException(409, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 DELETE /api/projects/{project_id} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"删除失败: {type(e).__name__}: {e}")


# ---------- 文件导入 + 识别 ----------

@router.post("/projects/{project_id}/import", response_model=ProjectResp)
async def api_project_import(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
):
    """上传/替换项目源文件（multipart/form-data）。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    MAX_SIZE = 50 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, f"文件过大: {len(content)} > {MAX_SIZE}")
    logger.info(
        f"[HTTP] POST /api/projects/{project_id[:8]}.../import "
        f"client={remote} filename={file.filename} size={len(content)}"
    )
    try:
        resp = await project_import_file(project_id, content, file.filename or "book.txt")
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/projects/{project_id[:8]}.../import total_ms={elapsed_ms}"
        )
        return resp
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../import -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"导入失败: {type(e).__name__}: {e}")


@router.post("/projects/{project_id}/import-text", response_model=ProjectResp)
async def api_project_import_text(
    project_id: str,
    req: ImportTextRequest,
    request: Request,
):
    """粘贴文本导入项目（浏览器直接粘贴小说正文）。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    text_len = len(req.text)
    logger.info(
        f"[HTTP] POST /api/projects/{project_id[:8]}.../import-text "
        f"client={remote} text_len={text_len} hint={req.filename_hint!r}"
    )
    # 大小限制：50MB UTF-8 上限 = ~1700 万字（对中文 txt 绰绰有余）
    MAX_LEN = 50 * 1024 * 1024
    if text_len > MAX_LEN:
        raise HTTPException(413, f"粘贴内容过大: {text_len} 字 > {MAX_LEN}（约 50MB）")
    try:
        resp = await project_import_text(
            project_id,
            text=req.text,
            filename_hint=req.filename_hint,
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/projects/{project_id[:8]}.../import-text total_ms={elapsed_ms}"
        )
        return resp
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../import-text -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"导入失败: {type(e).__name__}: {e}")


@router.post(
    "/projects/{project_id}/prepare",
    response_model=ProjectPrepareTriggerResp,
    status_code=202,
)
async def api_project_prepare(project_id: str, request: Request):
    """
    触发识别（后台异步执行，HTTP 202 立即返回）。
    执行路径：切章 → 角色识别（50k 切片）→ 角色 dedup → 对白归属（14 章/批）→ 音色推荐 → 落库。
    进度/错误请轮询 GET /projects/{project_id}，读取 prepare_progress：
      - stage: start / split / characters / dedup / dialogues / voice_recs / done
      - last_error / last_error_at：失败时带具体原因（切章失败、LLM 429 等）
      - char_slice_total / char_slice_completed_n / char_failed_slices：角色识别进度
      - dialogue_total_batches / dialogue_completed_chapters_count / dialogue_failed_batches：对白归属进度
    """
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/projects/{project_id[:8]}.../prepare client={remote}"
    )
    try:
        resp = await trigger_prepare_project(project_id)
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 202 /api/projects/{project_id[:8]}.../prepare "
            f"triggered total_ms={elapsed_ms}"
        )
        return resp
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../prepare -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        # 触发阶段异常（非后台执行期）返回具体错误，方便前端直接提示
        raise HTTPException(
            500,
            f"触发识别失败: {type(e).__name__}: {e}",
        )


@router.get("/projects/{project_id}/chapters", response_model=list[ChapterSummary])
async def api_project_chapters(project_id: str, request: Request):
    """章节列表。"""
    try:
        return await get_project_chapters(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../chapters -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"章节查询失败: {type(e).__name__}: {e}")


@router.get(
    "/projects/{project_id}/chapters/{chapter_idx}",
    response_model=ChapterDetail,
)
async def api_project_chapter_detail(
    project_id: str, chapter_idx: int, request: Request
):
    """单章详情：正文 + 逐行对白归属（用于前端角色-行标注视图）。"""
    try:
        return await get_project_chapter_detail(project_id, chapter_idx)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../chapters/{chapter_idx} -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"章节详情查询失败: {type(e).__name__}: {e}")


@router.get(
    "/projects/{project_id}/characters",
    response_model=list[CharacterWithVoice],
)
async def api_project_characters(project_id: str, request: Request):
    """角色 + 音色列表。"""
    try:
        return await get_project_characters(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../characters -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"角色查询失败: {type(e).__name__}: {e}")


@router.patch(
    "/projects/{project_id}/characters/{char_id}",
    response_model=CharacterResp,
)
async def api_update_character_voice(
    project_id: str,
    char_id: int,
    req: UpdateCharacterVoiceRequest,
    request: Request,
):
    """更新角色音色。"""
    try:
        return await update_character_voice(project_id, char_id, req.voice_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 PATCH /api/projects/{project_id[:8]}.../characters/{char_id} -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"更新音色失败: {type(e).__name__}: {e}")


# =================== 发音规则 ===================
@router.get(
    "/projects/{project_id}/pronunciation-rules",
    response_model=list[PronunciationRule],
)
async def api_list_pronunciation_rules(project_id: str):
    return await list_pronunciation_rules(project_id)


@router.post(
    "/projects/{project_id}/pronunciation-rules",
    response_model=PronunciationRule,
)
async def api_create_pronunciation_rule(
    project_id: str, body: PronunciationRuleInput
):
    try:
        return await create_pronunciation_rule(project_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put(
    "/projects/{project_id}/pronunciation-rules/{rule_id}",
    response_model=PronunciationRule,
)
async def api_update_pronunciation_rule(
    project_id: str, rule_id: int, body: PronunciationRuleInput
):
    try:
        return await update_pronunciation_rule(project_id, rule_id, body)
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(404, msg)
        raise HTTPException(400, msg)


@router.delete("/projects/{project_id}/pronunciation-rules/{rule_id}")
async def api_delete_pronunciation_rule(project_id: str, rule_id: int):
    try:
        await delete_pronunciation_rule(project_id, rule_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------- Build 任务 ----------

@router.post("/projects/{project_id}/builds", response_model=BuildResp)
async def api_start_build(
    project_id: str,
    req: StartBuildRequest,
    request: Request,
):
    """创建并启动 Build（后台任务，立即返回）。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/projects/{project_id[:8]}.../builds client={remote} "
        f"voices={len(req.voice_assignments)} narrator={req.narrator_voice_id} "
        f"speed={req.speed}"
    )
    try:
        resp = await start_build(
            project_id=project_id,
            voice_assignments=req.voice_assignments,
            narrator_voice_id=req.narrator_voice_id,
            speed=req.speed,
            mode=req.mode,
            tts_provider=req.tts_provider,
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/projects/{project_id[:8]}.../builds "
            f"build_id={resp.build_id[:8]}... status={resp.status} total_ms={elapsed_ms}"
        )
        return resp
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../builds -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"启动 build 失败: {type(e).__name__}: {e}")


@router.get("/projects/{project_id}/builds", response_model=list[BuildListItem])
async def api_list_builds(project_id: str, request: Request):
    """Build 历史列表。"""
    try:
        return await list_builds(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../builds -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"build 列表查询失败: {type(e).__name__}: {e}")


@router.get("/projects/{project_id}/builds/{build_id}", response_model=BuildDetailResp)
async def api_get_build(project_id: str, build_id: str, request: Request):
    """Build 详情（含 artifacts，可用于轮询）。"""
    try:
        return await get_build(project_id, build_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../builds/{build_id[:8]}... -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"build 详情查询失败: {type(e).__name__}: {e}")


@router.get(
    "/projects/{project_id}/builds/{build_id}/status",
    response_model=BuildStatusResp,
)
async def api_build_status(project_id: str, build_id: str, request: Request):
    """轮询用：仅 progress + artifacts（精简版，比详情少配置快照字段）。"""
    try:
        # 校验 build 属于该 project
        factory = get_session_factory()
        async with factory() as s:
            b = await s.get(Build, build_id)
            if not b or b.project_id != project_id:
                raise ValueError("build 不存在")
        return await get_build_status(build_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../builds/{build_id[:8]}.../status -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"build 状态查询失败: {type(e).__name__}: {e}")


@router.get(
    "/projects/{project_id}/builds/{build_id}/chapters/{idx}/download"
)
async def api_build_chapter_download(
    project_id: str,
    build_id: str,
    idx: int,
    request: Request,
):
    """单章下载：返回 FileResponse，强制浏览器保存为中文文件名。"""
    # 校验 build 属于该 project
    factory = get_session_factory()
    async with factory() as s:
        b = await s.get(Build, build_id)
        if not b or b.project_id != project_id:
            raise HTTPException(404, "build 不存在")
        art = (
            await s.execute(
                select(BuildArtifact).where(
                    BuildArtifact.build_id == build_id,
                    BuildArtifact.chapter_idx == idx,
                )
            )
        ).scalar_one_or_none()
        if not art or not art.audio_filename:
            raise HTTPException(404, f"章节 {idx} 尚未生成")
        audio_filename = art.audio_filename
        art_title = art.title

    audio_dir = Path(settings.AUDIO_DIR)
    fpath = audio_dir / audio_filename
    if not fpath.is_file():
        raise HTTPException(404, f"章节 {idx} 音频文件不存在")
    clean_title = strip_chapter_prefix(art_title or '')
    fname = f"第{idx+1:03d}章 {clean_title or '章节'}.mp3"
    for ch in '\\/:*?"<>|\r\n\t':
        fname = fname.replace(ch, "_")
    ascii_name = urllib.parse.quote(fname.encode("utf-8"), safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{ascii_name}"
    }
    return FileResponse(
        path=str(fpath),
        media_type="audio/mpeg",
        headers=headers,
        filename=fname,
    )


@router.get("/projects/{project_id}/builds/{build_id}/download-all")
async def api_build_download_all(
    project_id: str,
    build_id: str,
    request: Request,
):
    """一键全部下载：返回打包好的 ZIP（中文文件名）。"""
    factory = get_session_factory()
    async with factory() as s:
        b = await s.get(Build, build_id)
        if not b or b.project_id != project_id:
            raise HTTPException(404, "build 不存在")
        if not b.zip_filename:
            raise HTTPException(400, "整包 ZIP 尚未生成，请先等合成完成")
        zip_filename = b.zip_filename
        book_title = None
        proj = await s.get(Project, project_id)
        if proj:
            book_title = proj.book_title

    audio_dir = Path(settings.AUDIO_DIR)
    zip_path = audio_dir / zip_filename
    if not zip_path.is_file():
        raise HTTPException(404, "ZIP 文件不存在")
    download_name = _safe_download_name_build(book_title, build_id, ".zip")
    ascii_name = urllib.parse.quote(download_name.encode("utf-8"), safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{ascii_name}"
    }
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        headers=headers,
        filename=download_name,
    )


@router.delete("/projects/{project_id}/builds/{build_id}")
async def api_delete_build(project_id: str, build_id: str, request: Request):
    """删除 build + 磁盘 MP3 文件。项目正在合成（Build 运行中）时拒绝删除。"""
    try:
        await _ensure_project_not_running(project_id, action="删除 build")
        await delete_build(project_id, build_id)
        return {"ok": True, "build_id": build_id}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 DELETE /api/projects/{project_id[:8]}.../builds/{build_id[:8]}... -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"删除 build 失败: {type(e).__name__}: {e}")


@router.post("/projects/{project_id}/builds/{build_id}/cancel", response_model=BuildResp)
async def api_cancel_build(
    project_id: str,
    build_id: str,
    request: Request,
    req_body: CancelBuildRequest | None = None,
):
    """取消 Build（queued/running 可取消；终态则抛 409）。"""
    try:
        return await cancel_build(
            project_id=project_id,
            build_id=build_id,
            reason=(req_body.reason if req_body else None),
        )
    except ValueError as e:
        msg = str(e)
        # 简单粗暴：服务里报『已是终态 / 不允许取消』的 ValueError 当作 409
        if "已是终态" in msg or "已取消" in msg or "不可取消" in msg or "终态" in msg:
            raise HTTPException(409, msg)
        raise HTTPException(404, msg)
    except Exception as e:
        logger.error(
            f"[HTTP] 500 POST /api/projects/{project_id[:8]}.../builds/{build_id[:8]}.../cancel -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"取消失败: {type(e).__name__}: {e}")


@router.post("/projects/{project_id}/builds/{build_id}/retry-failed", response_model=BuildResp)
async def api_retry_failed_build(
    project_id: str,
    build_id: str,
    request: Request,
    req_body: RetryFailedBuildRequest | None = None,
):
    """创建一个 retry build：仅重跑 source build 中失败的章节，其余章节直接复用原 MP3（完整 ZIP）。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/projects/{project_id[:8]}.../builds/{build_id[:8]}.../retry-failed "
        f"client={remote}"
    )
    try:
        resp = await retry_failed_build(
            source_build_id=build_id,
            force_restart_failed_only=(
                req_body.force_restart_failed_only if req_body else True
            ),
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 POST /api/projects/{project_id[:8]}.../builds/{build_id[:8]}.../retry-failed -> "
            f"new_build_id={resp.build_id[:8]}... total_ms={elapsed_ms}"
        )
        return resp
    except ValueError as e:
        msg = str(e)
        if "没有失败章可重试" in msg or "无失败章" in msg:
            raise HTTPException(400, msg)
        raise HTTPException(404, msg)
    except Exception as e:
        logger.error(
            f"[HTTP] 500 POST /api/projects/{project_id[:8]}.../builds/{build_id[:8]}.../retry-failed -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"失败章重试失败: {type(e).__name__}: {e}")


# =====================================================================
# 系统设置 GET / PUT
# =====================================================================

# 可通过 API 修改的字段白名单（不含 JWT_SECRET、DISABLE_AUTH 等安全敏感项）
_EDITABLE_SETTINGS = {
    # ---- 多厂商模型配置（新结构；前端按厂商卡片编辑）----
    "PROVIDERS_CONFIG": ("json", "模型配置", "厂商配置 JSON（前端按卡片管理）"),
    "ACTIVE_TTS_PROVIDER": ("str", "模型配置", "激活的 TTS 厂商"),
    "ACTIVE_TTS_MODEL": ("str", "模型配置", "激活的 TTS 模型"),
    "ACTIVE_LLM_PROVIDER": ("str", "模型配置", "激活的 LLM 厂商"),
    "ACTIVE_LLM_MODEL": ("str", "模型配置", "激活的 LLM 模型"),
    # ---- 遗留扁平字段（保留兜底；新 UI 不再展示）----
    "TTS_API_KEY": ("str", "模型配置", "TTS API Key（遗留）"),
    "TTS_BASE_URL": ("str", "模型配置", "TTS Base URL（遗留）"),
    "LLM_API_KEY": ("str", "模型配置", "LLM API Key（遗留）"),
    "LLM_BASE_URL": ("str", "模型配置", "LLM Base URL（遗留）"),
    "LLM_MODEL_PRO": ("str", "模型配置", "LLM 模型（遗留）"),
    # 超时
    "LLM_TIMEOUT": ("int", "超时配置", "LLM 超时（秒）"),
    "TTS_TIMEOUT": ("int", "超时配置", "TTS 超时（秒）"),
    "UVICORN_TIMEOUT": ("int", "超时配置", "Uvicorn 超时（秒）"),
    "BUILD_RUNNING_TIMEOUT_HOURS": ("int", "超时配置", "Build 运行超时（小时）"),
    # 限流
    "LLM_MAX_CONCURRENCY": ("int", "限流配置", "LLM 最大并发"),
    "LLM_CHAR_EXTRACT_SLICE_SIZE": ("int", "限流配置", "角色识别切片大小（字符）"),
    "DIALOGUE_BATCH_CHAPTERS": ("int", "限流配置", "对白归属批大小（章/批）"),
    "DIALOGUE_BATCH_CONCURRENCY": ("int", "限流配置", "对白归属批并发度"),
    "DIALOGUE_BATCH_RETRY_COUNT": ("int", "限流配置", "对白归属重试次数"),
    "TTS_MAX_CONCURRENCY": ("int", "限流配置", "TTS 最大并发"),
    "TTS_RPM_LIMIT": ("int", "限流配置", "TTS RPM 限流"),
    # 缓存
    "TTS_SEGMENT_CACHE_MAX_ENTRIES": ("int", "缓存配置", "段缓存 LRU 上限（条）"),
    "TTS_SEGMENT_CACHE_TTL_DAYS": ("int", "缓存配置", "段缓存过期天数"),
    "TTS_SEGMENT_CACHE_MAX_SIZE_GB": ("int", "缓存配置", "段缓存最大磁盘占用（GB）"),
    # 章节切分
    "CHAPTER_SPLIT_PATTERNS": ("list[str]", "章节切分", "切章正则匹配规则（每条一行）"),
    "CHAPTER_SPLIT_MIN_MATCHES": ("int", "章节切分", "最少匹配章节数"),
    "CHAPTER_SPLIT_HARD_FALLBACK_ENABLED": ("bool", "章节切分", "硬切兜底开关"),
    "CHAPTER_SPLIT_HARD_FALLBACK_MAX_CHARS": ("int", "章节切分", "硬切每块字符上限"),
    # 日志
    "LOG_LEVEL": ("str", "日志配置", "日志级别"),
    "LOG_FILE": ("str", "日志配置", "日志文件路径"),
    # 认证
    "JWT_EXP_DAYS": ("int", "认证配置", "JWT 过期天数"),
}

# 只读字段（展示用，不可通过 API 修改）
_READONLY_SETTINGS = {
    "ENV": ("str", "系统", "运行环境"),
    "DISABLE_AUTH": ("bool", "系统", "禁用认证"),
    "STRICT_PROD_SECURITY": ("bool", "系统", "严格生产安全"),
    "DATABASE_URL": ("str", "系统", "数据库 URL"),
    "DATA_DIR": ("str", "系统", "数据目录"),
    "AUDIO_DIR": ("str", "系统", "音频目录"),
    "BIND_HOST": ("str", "系统", "监听地址"),
    "PORT": ("int", "系统", "监听端口"),
}


class SettingsResp(BaseModel):
    """设置项：key、值、类型、分组、标签、是否只读"""
    key: str
    value: str | int | bool | list[str] | None
    type: str
    group: str
    label: str
    readonly: bool = False


class SettingsUpdateReq(BaseModel):
    updates: dict[str, str | int | bool | list[str] | dict] = Field(default_factory=dict)


# =====================================================================
# 多厂商模型配置（/api/providers GET/PUT）
# 设计：独立于 settings 通用接口，避免把超长 JSON 塞进 settingsUpdate.updates。
# =====================================================================


@router.get("/providers")
async def get_providers(current: User = Depends(get_current_user)):
    """返回所有厂商配置（含未启用）和当前激活模型。"""
    from ..core.config import list_providers, get_active
    tts_pid, tts_mid, _ = get_active("tts")
    llm_pid, llm_mid, _ = get_active("llm")
    return {
        "providers": list_providers(),
        "active": {
            "tts": {"provider_id": tts_pid, "model_id": tts_mid},
            "llm": {"provider_id": llm_pid, "model_id": llm_mid},
        },
    }


@router.put("/providers")
async def update_providers(
    cfg: dict,
    current: User = Depends(get_current_user),
):
    """覆盖式保存多厂商结构；同时同步刷新 ACTIVE_* 字段，确保 TTS/LLM 即时生效。"""
    from ..core.config import save_providers_config, get_provider
    if not isinstance(cfg, dict) or "providers" not in cfg:
        raise HTTPException(400, "请求体必须包含 providers 列表")

    providers_in = cfg.get("providers") or []
    active_in = cfg.get("active") or {}
    # 校验结构
    for i, p in enumerate(providers_in):
        if not isinstance(p, dict):
            raise HTTPException(400, f"providers[{i}] 不是对象")
        if "id" not in p or not p["id"]:
            raise HTTPException(400, f"providers[{i}].id 必填")

    # 写回（先持久化，再校验 active；这样 active 可以指向本次请求里新增的 model）
    save_providers_config(cfg)

    # 激活项：必须是已存在的 provider，且 model_id 必须是该 provider 下的 model 且 kind 一致
    for kind in ("tts", "llm"):
        a = (active_in.get(kind) or {})
        pid = a.get("provider_id")
        mid = a.get("model_id")
        if not pid:
            continue
        prov = get_provider(pid)
        if not prov:
            raise HTTPException(400, f"active.{kind}.provider_id={pid!r} 不存在")
        if mid:
            model = next((m for m in (prov.get("models") or []) if m.get("id") == mid), None)
            if not model:
                raise HTTPException(400, f"active.{kind}.model_id={mid!r} 不在厂商 {pid!r} 的模型列表中")
            if model.get("kind") != kind:
                raise HTTPException(
                    400,
                    f"active.{kind}.model_id={mid!r} 的 kind={model.get('kind')!r}，与角色 {kind!r} 不匹配",
                )
    # 同步激活字段到 settings
    tts_a = active_in.get("tts") or {}
    llm_a = active_in.get("llm") or {}
    settings.ACTIVE_TTS_PROVIDER = tts_a.get("provider_id") or ""
    settings.ACTIVE_TTS_MODEL = tts_a.get("model_id") or ""
    settings.ACTIVE_LLM_PROVIDER = llm_a.get("provider_id") or ""
    settings.ACTIVE_LLM_MODEL = llm_a.get("model_id") or ""

    return {"ok": True, "providers": providers_in, "note": "已保存到内存（重启后端后恢复 .env 默认）"}


@router.get("/settings", response_model=list[SettingsResp])
async def get_settings(
    cred: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
):
    """返回所有可配置项的当前值"""
    payload = decode_token(cred.credentials)
    if not payload:
        raise HTTPException(401, "无效凭证")

    result: list[SettingsResp] = []
    # 可编辑项
    for key, (typ, group, label) in _EDITABLE_SETTINGS.items():
        val = getattr(settings, key, None)
        result.append(SettingsResp(
            key=key, value=val, type=typ, group=group, label=label, readonly=False,
        ))
    # 只读项
    for key, (typ, group, label) in _READONLY_SETTINGS.items():
        val = getattr(settings, key, None)
        # 简化：路径转字符串
        if hasattr(val, "__fspath__"):
            val = str(val)
        result.append(SettingsResp(
            key=key, value=val, type=typ, group=group, label=label, readonly=True,
        ))
    return result


@router.put("/settings", response_model=dict)
async def update_settings(
    req: SettingsUpdateReq,
    cred: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
):
    """更新运行时配置（内存生效）。支持 int/str/bool/list[str]。"""
    payload = decode_token(cred.credentials)
    if not payload:
        raise HTTPException(401, "无效凭证")

    updated: list[str] = []
    skipped: list[str] = []
    note_extras: list[str] = []

    for key, val in req.updates.items():
        if key not in _EDITABLE_SETTINGS:
            skipped.append(key)
            continue
        typ = _EDITABLE_SETTINGS[key][0]
        # 类型转换
        try:
            if typ == "int":
                val = int(val)
            elif typ == "bool":
                if isinstance(val, str):
                    val = val.lower() in ("1", "true", "yes", "on")
                val = bool(val)
            elif typ == "str":
                val = str(val)
            elif typ == "list[str]":
                # 支持传 list，或传入字符串时按换行拆分成 list
                if isinstance(val, list):
                    val = [str(x).strip() for x in val if str(x).strip()]
                else:
                    val = [
                        ln.strip() for ln in str(val).splitlines()
                        if ln.strip()
                    ]
            elif typ == "json":
                # 接受 dict 或 string；序列化后再写回 settings
                import json as _json_mod
                if isinstance(val, (dict, list)):
                    # 简单结构校验
                    val = _json_mod.dumps(val, ensure_ascii=False)
                elif isinstance(val, str):
                    # 校验 JSON 合法
                    parsed = _json_mod.loads(val)
                    val = _json_mod.dumps(parsed, ensure_ascii=False)
                else:
                    raise ValueError(f"json 字段类型不支持: {type(val)}")
            else:
                skipped.append(f"{key}(未知类型 {typ})")
                continue
        except (ValueError, TypeError) as e:
            skipped.append(f"{key}(类型转换失败: {e})")
            continue

        setattr(settings, key, val)
        updated.append(key)

    # 特殊钩子：修改切章正则后即时编译 + 校验
    if "CHAPTER_SPLIT_PATTERNS" in updated:
        try:
            from ..services.book_split import refresh_chapter_patterns
            compiled = refresh_chapter_patterns()
            total = len(settings.CHAPTER_SPLIT_PATTERNS)
            success = len(compiled)
            note_extras.append(f"章节切分规则已即时生效：{success}/{total} 条编译成功")
            if success < total:
                note_extras.append(f"⚠️ 有 {total - success} 条正则语法错误，已自动跳过")
        except Exception as e:
            skipped.append(f"CHAPTER_SPLIT_PATTERNS(编译异常: {e})")
            updated.remove("CHAPTER_SPLIT_PATTERNS")

    logger.info(f"[Settings] 更新配置: {updated}")
    if skipped:
        logger.warning(f"[Settings] 跳过: {skipped}")

    note = "配置已即时生效（内存）。重启后端后恢复 .env 默认值。"
    if note_extras:
        note += " · " + " · ".join(note_extras)

    return {
        "ok": True,
        "updated": updated,
        "skipped": skipped,
        "note": note,
    }
