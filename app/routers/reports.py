from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import member_user, scoped, team_ids
from app.models import Client, Payment, User
from app.web import render

router = APIRouter(prefix="/reports")

PERIODS = {"today": "Today", "last_7": "Last 7 days", "this_month": "This month", "last_month": "Last month",
           "last_30": "Last 30 days", "this_year": "This year", "all": "All time", "custom": "Custom range"}


def resolve_period(period: str, date_from: str = "", date_to: str = "") -> tuple[date | None, date | None]:
    today = date.today()
    if period == "custom":
        def parse(value):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return parse(date_from), parse(date_to)
    if period == "today":
        return today, today
    if period == "last_7":
        return today - timedelta(days=6), today
    if period == "last_30":
        return today - timedelta(days=29), today
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if period == "this_year":
        return date(today.year, 1, 1), today
    return None, None


def amount_if(condition, value=Payment.amount):
    return func.coalesce(func.sum(case((condition, value), else_=0)), 0)


SUMS = [
    func.count(Payment.id).label("count"),
    amount_if(Payment.direction == "incoming").label("incoming"),
    amount_if(Payment.direction == "outgoing").label("outgoing"),
    amount_if(Payment.verification_status.is_not(None)).label("requested"),
    amount_if(Payment.verification_status == "pending").label("pending"),
    amount_if(Payment.verification_status == "verified",
              func.coalesce(Payment.verified_amount, Payment.amount)).label("verified"),
    amount_if(Payment.verification_status == "not_received").label("not_received"),
]


@router.get("")
def revenue_report(request: Request, period: str = "this_month", date_from: str = "", date_to: str = "",
                   db: Session = Depends(get_db), user: User = Depends(member_user)):
    period = period if period in PERIODS else "this_month"
    start, end = resolve_period(period, date_from, date_to)
    day = func.coalesce(Payment.payment_date, func.date(Payment.created_at))

    def in_scope(stmt):
        stmt = scoped(stmt, Payment.owner_id, team_ids(db, user))
        if start:
            stmt = stmt.where(day >= start)
        if end:
            stmt = stmt.where(day <= end)
        return stmt

    totals = db.execute(in_scope(select(Payment.currency, *SUMS).group_by(Payment.currency))).all()
    by_account = db.execute(in_scope(
        select(User.id, User.name, User.email, User.role, Payment.currency, *SUMS)
        .join(User, User.id == Payment.owner_id)
        .group_by(User.id, User.name, User.email, User.role, Payment.currency)
        .order_by(User.name))).all()
    by_client = db.execute(in_scope(
        select(Client.id, Client.company_name, User.name.label("owner"), Payment.currency, *SUMS)
        .join(Client, Client.id == Payment.client_id).join(User, User.id == Payment.owner_id)
        .group_by(Client.id, Client.company_name, User.name, Payment.currency)
        .order_by(Client.company_name))).all()

    return render(request, "reports.html", totals=totals, by_account=by_account, by_client=by_client,
                  periods=PERIODS, start=start, end=end,
                  filters={"period": period, "date_from": date_from, "date_to": date_to})
