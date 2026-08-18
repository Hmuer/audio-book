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
from ..ai.factory import get_tts
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
    get_project_characters,
    update_character_voice,
    ProjectResp,
    ProjectDetailResp,
    ProjectListItem,
    ProjectPrepareResp,
    ProjectPrepareTriggerResp,
    ChapterSummary,
    CharacterWithVoice,
    CharacterResp,
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
async def list_voices():
    tts = get_tts()
    voices = await tts.list_voices()
    return {"voices": voices, "count": len(voices)}


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
        tts = get_tts()
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
    fname = f"第{idx+1:03d}章 {art_title or '章节'}.mp3"
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
