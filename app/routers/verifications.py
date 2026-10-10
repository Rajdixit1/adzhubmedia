from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import today as local_today
from app.database import get_db
from app.deps import current_user, scoped, team_ids
from app.models import Client, Payment, User
from app.routers.reports import PERIODS, resolve_period
from app.web import DUE_SOON_DAYS, paginate, render

router = APIRouter(prefix="/verifications")

STATES = {"pending": "All pending", "overdue": "Overdue", "due_soon": f"Due in {DUE_SOON_DAYS} days",
          "verified": "Payment received", "not_received": "Not received"}


def base_query(db: Session, user: User):
    stmt = select(Payment).where(Payment.verification_status.is_not(None))
    if user.role != "ca":
        stmt = scoped(stmt, Payment.owner_id, team_ids(db, user))
    return stmt


def filter_state(stmt, state: str):
    today = local_today()
    if state == "overdue":
        return stmt.where(Payment.verification_status == "pending", Payment.due_date < today)
    if state == "due_soon":
        return stmt.where(Payment.verification_status == "pending",
                          Payment.due_date.between(today, today + timedelta(days=DUE_SOON_DAYS)))
    if state in ("pending", "verified", "not_received"):
        return stmt.where(Payment.verification_status == state)
    return stmt


def count(db: Session, stmt) -> int:
    return db.scalar(select(func.count()).select_from(stmt.subquery()))


@router.get("")
def list_verifications(request: Request, state: str = "", q: str = "", period: str = "all",
                       date_from: str = "", date_to: str = "", page: int = 1,
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = base_query(db, user)
    start, end = resolve_period(period, date_from, date_to)
    if start:
        stmt = stmt.where(Payment.submitted_at >= datetime.combine(start, time.min))
    if end:
        stmt = stmt.where(Payment.submitted_at < datetime.combine(end + timedelta(days=1), time.min))
    if q:
        stmt = stmt.join(Client).where(Client.company_name.ilike(f"%{q}%"))

    summary = {key: count(db, filter_state(stmt, key)) for key in STATES}
    ordered = filter_state(stmt, state).order_by(
        case((Payment.verification_status == "pending", 0), else_=1), Payment.due_date.asc(), Payment.id.desc())
    payments, pager = paginate(db, ordered, page)
    return render(request, "verifications.html", payments=payments, pager=pager, summary=summary, states=STATES,
                  periods=PERIODS, filters={"state": state, "q": q, "period": period,
                                            "date_from": date_from, "date_to": date_to})
