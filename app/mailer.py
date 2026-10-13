import smtplib
from email.message import EmailMessage

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import file_logger
from app.config import settings
from app.models import User
from app.whatsapp import send_whatsapp


def mail_log():
    return file_logger("emails", settings.LOG_DIR / "emails.log")


def send_email(recipients: list[str], subject: str, body: str, reply_to: str | None = None) -> None:
    recipients = [r for r in dict.fromkeys(recipients) if r]
    if not recipients:
        return
    if not settings.SMTP_HOST:
        mail_log().info(f"[not sent: SMTP not configured] to={', '.join(recipients)} | {subject}\n{body}\n")
        return

    msg = EmailMessage()
    msg["From"] = settings.MAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
            if settings.SMTP_TLS:
                smtp.starttls()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
        mail_log().info(f"[sent] to={', '.join(recipients)} | {subject}")
    except Exception as exc:
        mail_log().error(f"[failed] to={', '.join(recipients)} | {subject} | {exc}")


def admin_emails(db: Session) -> list[str]:
    return db.scalars(select(User.email).where(User.role == "admin", User.status == "active")).all()


def ca_users(db: Session) -> list[User]:
    return db.scalars(select(User).where(User.role == "ca", User.status == "active")).all()


def stakeholders(db: Session, owner: User) -> list[str]:
    """Owner, the owner's parent manager and all admins."""
    emails = [owner.email]
    if owner.parent:
        emails.append(owner.parent.email)
    return emails + admin_emails(db)


def notify(background: BackgroundTasks, users: list[User], subject: str, body: str,
           reply_to: str | None = None) -> None:
    """Email and WhatsApp each user on their registered email and phone number."""
    background.add_task(send_email, [u.email for u in users], subject, body, reply_to)
    background.add_task(send_whatsapp, [u.phone for u in users], f"*{subject}*\n\n{body}")
