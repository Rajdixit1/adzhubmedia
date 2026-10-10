import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
    DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{(BASE_DIR / 'adzhub.db').as_posix()}"
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@adzhubmedia.com")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_TLS = os.getenv("SMTP_TLS", "true").lower() == "true"
    MAIL_FROM = os.getenv("MAIL_FROM", "no-reply@adzhubmedia.com")
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))
    LOG_DIR = BASE_DIR / "logs"
    UPLOAD_DIR = BASE_DIR / "uploads"


settings = Settings()
# Render/Heroku give postgres:// URLs; SQLAlchemy needs the psycopg driver name.
if settings.DATABASE_URL.startswith(("postgres://", "postgresql://")):
    settings.DATABASE_URL = "postgresql+psycopg://" + settings.DATABASE_URL.split("://", 1)[1]
(settings.LOG_DIR / "users").mkdir(parents=True, exist_ok=True)
settings.UPLOAD_DIR.mkdir(exist_ok=True)
