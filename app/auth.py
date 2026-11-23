"""RBAC + 行级数据权限。area='*' 表示全部区域。"""
from __future__ import annotations

from sqlalchemy import select

from . import orm
from .models import User

ROLE_PERMS = {
    "admin": {"read", "export", "device:write", "frame:push",
              "ticket:write", "ticket:transition", "review:write"},
    "operator": {"read", "frame:push", "ticket:write",
                 "ticket:transition", "review:write"},
    "auditor": {"read", "export"},
    "viewer": {"read"},
}


def authenticate(session, token: str) -> User | None:
    if not token:
        return None
    row = session.scalar(select(orm.User).where(orm.User.token == token))
    return User(**row.to_dict()) if row else None


def require(user: User | None, perm: str) -> User:
    if user is None:
        raise PermissionError("authentication required")
    if perm not in ROLE_PERMS.get(user.role, set()):
        raise PermissionError(f"role {user.role} lacks permission {perm}")
    return user


def area_filter(user: User):
    """返回 None 表示不加区域条件；否则只可见该区域。"""
    return None if user.area == "*" else user.area


def can_see(user: User, area: str) -> bool:
    f = area_filter(user)
    return f is None or f == area
