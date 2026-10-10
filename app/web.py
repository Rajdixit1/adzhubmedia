import json
from datetime import date

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.config import BASE_DIR

PAGE_SIZE = 25

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["dt"] = lambda v: v.strftime("%d %b %Y, %H:%M") if v else ""
templates.env.filters["money"] = lambda v: f"{v:,.2f}" if v is not None else ""
templates.env.filters["fromjson"] = lambda v: json.loads(v) if v else {}
templates.env.globals["page_url"] = lambda request, n: str(request.url.include_query_params(page=n))

DUE_SOON_DAYS = 5


def deadline_state(payment) -> tuple[str, str]:
    """(css class, label) for a payment's verification deadline."""
    status = payment.verification_status
    if not status:
        return "", ""
    if status == "verified":
        return "ok", "Payment received"
    if status == "not_received":
        return "bad", "Not received"
    if not payment.due_date:
        return "info", "Pending"
    days = (payment.due_date - date.today()).days
    if days < 0:
        return "bad", f"Overdue by {-days} day{'s' if days != -1 else ''}"
    if days == 0:
        return "warn", "Due today"
    if days <= DUE_SOON_DAYS:
        return "warn", f"Due in {days} day{'s' if days != 1 else ''}"
    return "info", f"{days} days left"


templates.env.globals["deadline_state"] = deadline_state


class PageError(Exception):
    def __init__(self, status_code: int = 403, message: str = "You do not have access to this page."):
        self.status_code = status_code
        self.message = message


class LoginRequired(Exception):
    pass


def not_found(what: str = "Record") -> PageError:
    return PageError(404, f"{what} not found.")


def flash(request: Request, message: str, kind: str = "success") -> None:
    request.session["_flash"] = request.session.get("_flash", []) + [[kind, message]]


def render(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("user", getattr(request.state, "user", None))
    context["flashes"] = request.session.pop("_flash", [])
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def paginate(db, stmt, page: int):
    page = max(page, 1)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = db.scalars(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    return items, {"page": page, "pages": max(1, -(-total // PAGE_SIZE)), "total": total}


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
