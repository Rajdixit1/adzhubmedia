from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit import user_log_path
from app.config import settings
from app.database import get_db
from app.deps import current_user, team_ids
from app.models import ActivityLog, User
from app.web import PageError, paginate, render

router = APIRouter(prefix="/logs")

ACTIONS = ["login", "logout", "register", "create", "update", "delete", "approve", "password",
           "submit_verification", "verify",
           "upload", "download", "remove_file"]
ENTITIES = ["auth", "user", "client", "payment"]


def scope_logs(stmt, ids: list[int] | None):
    if ids is None:
        return stmt
    return stmt.where(or_(ActivityLog.owner_id.in_(ids), ActivityLog.user_id.in_(ids)))


def parse_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


@router.get("")
def list_logs(request: Request, action: str = "", entity: str = "", entity_id: str = "", q: str = "",
              date_from: str = "", date_to: str = "", page: int = 1,
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = scope_logs(select(ActivityLog), team_ids(db, user))
    if action:
        stmt = stmt.where(ActivityLog.action == action)
    if entity:
        stmt = stmt.where(ActivityLog.entity == entity)
    if entity_id.isdigit():
        stmt = stmt.where(ActivityLog.entity_id == int(entity_id))
    if q:
        stmt = stmt.where(or_(ActivityLog.user_email.ilike(f"%{q}%"), ActivityLog.details.ilike(f"%{q}%")))
    if start := parse_date(date_from):
        stmt = stmt.where(ActivityLog.created_at >= start)
    if end := parse_date(date_to):
        stmt = stmt.where(ActivityLog.created_at < end + timedelta(days=1))

    logs, pager = paginate(db, stmt.order_by(ActivityLog.id.desc()), page)
    filters = {"action": action, "entity": entity, "entity_id": entity_id, "q": q,
               "date_from": date_from, "date_to": date_to}
    return render(request, "logs.html", logs=logs, pager=pager, filters=filters, actions=ACTIONS, entities=ENTITIES)


@router.get("/download")
def download_log(user: User = Depends(current_user)):
    path = settings.LOG_DIR / "app.log" if user.role == "admin" else user_log_path(user.id)
    if not path.exists():
        raise PageError(404, "No log file yet.")
    return FileResponse(path, filename=path.name, media_type="text/plain")
