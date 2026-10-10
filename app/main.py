from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select, text
from starlette.middleware.sessions import SessionMiddleware

from app.audit import app_log
from app.config import BASE_DIR, settings
from app.database import Base, SessionLocal, engine
from app.models import User
from app.routers import auth, clients, dashboard, logs, payments, reports, users, verifications
from app.security import hash_password
from app.web import LoginRequired, PageError, render


def create_default_admin():
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.role == "admin")):
            return
        db.add(User(role="admin", name="Administrator", email=settings.ADMIN_EMAIL.lower(),
                    password_hash=hash_password(settings.ADMIN_PASSWORD), status="active"))
        db.commit()
        app_log().info(f"default admin created: {settings.ADMIN_EMAIL}")


def add_missing_columns():
    """Add new nullable columns to existing tables (simple upgrade without a migration tool)."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} "
                                      f"{column.type.compile(engine.dialect)}"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    add_missing_columns()
    create_default_admin()
    yield


app = FastAPI(title="Adzhubmedia", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, same_site="lax", max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", 303)


@app.exception_handler(PageError)
async def page_error(request: Request, exc: PageError):
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, message=exc.message)


for module in (auth, dashboard, users, clients, payments, verifications, reports, logs):
    app.include_router(module.router)
