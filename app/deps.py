from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.web import LoginRequired, PageError


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if not user or user.status != "active":
        request.session.pop("user_id", None)
        raise LoginRequired()
    request.state.user = user
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise PageError()
    return user


def member_user(user: User = Depends(current_user)) -> User:
    """Admin, advertiser or publisher. CA accounts only work in the verification section."""
    if user.role == "ca":
        raise PageError()
    return user


def team_ids(db: Session, user: User) -> list[int] | None:
    """User ids whose data this user can see. None means everything (admin)."""
    if user.role == "admin":
        return None
    ids = [user.id]
    if user.is_manager:
        ids += db.scalars(select(User.id).where(User.parent_id == user.id)).all()
    return ids


def team_users(db: Session, user: User) -> list[User]:
    if user.role == "admin":
        return db.scalars(select(User).where(User.status == "active", User.role != "ca")
                          .order_by(User.role, User.name)).all()
    if user.is_manager:
        return [user] + db.scalars(select(User).where(User.parent_id == user.id).order_by(User.name)).all()
    return [user]


def scoped(stmt, column, ids: list[int] | None):
    return stmt if ids is None else stmt.where(column.in_(ids))


def check_access(db: Session, user: User, owner_id: int) -> None:
    ids = team_ids(db, user)
    if ids is not None and owner_id not in ids:
        raise PageError()


def pick_owner(db: Session, user: User, value) -> int:
    allowed = {u.id for u in team_users(db, user)}
    if value and str(value).isdigit() and int(value) in allowed:
        return int(value)
    return user.id
