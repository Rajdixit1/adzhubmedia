import json
import logging

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ActivityLog, User
from app.web import client_ip

_loggers: dict[str, logging.Logger] = {}


def file_logger(name: str, path) -> logging.Logger:
    if name not in _loggers:
        logger = logging.getLogger(f"adzhub.{name}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        logger.addHandler(handler)
        _loggers[name] = logger
    return _loggers[name]


def app_log() -> logging.Logger:
    return file_logger("app", settings.LOG_DIR / "app.log")


def user_log_path(user_id: int):
    return settings.LOG_DIR / "users" / f"user_{user_id}.log"


def user_log(user_id: int) -> logging.Logger:
    return file_logger(f"user_{user_id}", user_log_path(user_id))


def record(db: Session, request: Request, actor: User, action: str, entity: str,
           entity_id: int | None = None, owner_id: int | None = None, details: dict | None = None) -> None:
    """Write one activity entry to the database, app.log and the per-user log files."""
    payload = json.dumps(details, default=str) if details else None
    ip = client_ip(request)
    db.add(ActivityLog(user_id=actor.id, user_email=actor.email, owner_id=owner_id, action=action,
                       entity=entity, entity_id=entity_id, details=payload, ip=ip))
    db.commit()

    line = f"{actor.email} (#{actor.id}) | {action} | {entity}#{entity_id or '-'} | ip={ip} | {payload or ''}"
    app_log().info(line)
    user_log(actor.id).info(line)
    if owner_id and owner_id != actor.id:
        user_log(owner_id).info(line)


def apply_changes(obj, data: dict) -> dict:
    """Set attributes on obj and return {field: [old, new]} for fields that actually changed."""
    changes = {}
    for field, new in data.items():
        old = getattr(obj, field)
        if (str(old) if old is not None else "") != (str(new) if new is not None else ""):
            changes[field] = [old, new]
            setattr(obj, field, new)
    return changes
