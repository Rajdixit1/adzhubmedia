from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import current_user, scoped, team_ids
from app.models import ActivityLog, Client, Payment, User
from app.routers.logs import scope_logs
from app.routers.verifications import base_query, count, filter_state
from app.web import render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role == "ca":
        return RedirectResponse("/verifications", 303)
    ids = team_ids(db, user)
    client_count = db.scalar(scoped(select(func.count(Client.id)), Client.owner_id, ids))
    payment_count = db.scalar(scoped(select(func.count(Payment.id)), Payment.owner_id, ids))
    totals = db.execute(scoped(
        select(Payment.direction, Payment.currency, Payment.status, func.sum(Payment.amount))
        .group_by(Payment.direction, Payment.currency, Payment.status), Payment.owner_id, ids)).all()

    verifications = base_query(db, user)
    awaiting = count(db, filter_state(verifications, "pending"))
    overdue = count(db, filter_state(verifications, "overdue"))

    pending_users = 0
    if user.role == "admin":
        pending_users = db.scalar(select(func.count(User.id)).where(User.status == "pending"))
    team = len(user.children) if user.is_manager else 0

    recent = db.scalars(scope_logs(select(ActivityLog), ids).order_by(ActivityLog.id.desc()).limit(10)).all()
    return render(request, "dashboard.html", client_count=client_count, payment_count=payment_count,
                  totals=totals, pending_users=pending_users, team=team, recent=recent,
                  awaiting=awaiting, overdue=overdue)
