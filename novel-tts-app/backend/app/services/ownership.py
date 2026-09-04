"""
资源归属（Resource ownership）守卫 —— P1 #5 修复。

策略：
- 每个 Project 有 owner_user_id 字段（nullable，DB 演进期间保留旧 NULL）。
- 普通用户只能看到/操作自己 own 的项目；admin (用户名为 settings.SEED_ADMIN_USER) 可访问任意项目。
- owner_user_id = 0 表示"管理员孤儿池"，任何登录用户可读，仅 admin 可写/删（保留旧行为兼容）。
- 资源"不存在"和"无权限"应被严格区分：本 helper 在未找到时抛 HTTPException(404)，
  找到但无权限时抛 HTTPException(403)。

为什么不用 Depends：
- Depends 写起来更"FastAPI"，但无法在 Service 层复用；业务里很多地方（prepare_project、
  start_build 等）已经直接调 service 层函数。让 service 拿"已校验过的 current user"更
  一致：所有读/写路径在同一个 helper 上，授权决策集中。
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..db.models import Project, User


def is_admin_user(user: User | None) -> bool:
    """是否为 seed admin（特殊身份，可访问所有项目）。"""
    if not user:
        return False
    return user.username == settings.SEED_ADMIN_USER


async def get_project_for_user(
    session: AsyncSession, project_id: str, user: User
) -> Project:
    """
    取项目并校验当前用户可读：
    - 项目不存在 → 404
    - owner_user_id == user.id 或 owner_user_id == 0（孤儿池）→ 通过
    - admin → 任意项目通过
    - 其它 → 403

    返回 Project 对象（未 detach），调用方继续用即可。
    """
    p = await session.get(Project, project_id)
    if not p:
        raise HTTPException(404, f"项目不存在: {project_id}")
    if is_admin_user(user):
        return p
    if p.owner_user_id is None or p.owner_user_id == user.id or p.owner_user_id == 0:
        return p
    raise HTTPException(403, "无权访问该项目")


async def assert_project_writable(
    session: AsyncSession, project_id: str, user: User
) -> Project:
    """
    取项目并校验当前用户可写：
    - 不存在 → 404
    - owner_user_id == user.id → 通过
    - admin → 任意项目通过
    - 孤儿池 (owner_user_id == 0) → 仅 admin 可写（普通用户不能改 admin 的孤儿池）
    - 其它 → 403
    """
    p = await session.get(Project, project_id)
    if not p:
        raise HTTPException(404, f"项目不存在: {project_id}")
    if is_admin_user(user):
        return p
    if p.owner_user_id == user.id:
        return p
    raise HTTPException(403, "无权操作该项目")


async def claim_orphan_projects(session: AsyncSession, admin_user: User) -> int:
    """
    启动时调用一次：把 owner_user_id IS NULL 的项目归属到 admin。
    返回本次实际更新条数。
    """
    from sqlalchemy import update  # 局部导入避免顶部增加无关依赖
    stmt = (
        update(Project)
        .where(Project.owner_user_id.is_(None))
        .values(owner_user_id=admin_user.id)
    )
    res = await session.execute(stmt)
    await session.commit()
    return res.rowcount or 0