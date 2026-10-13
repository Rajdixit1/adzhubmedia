import secrets
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit import apply_changes, record
from app.config import now
from app.database import get_db
from app.deps import current_user
from app.mailer import admin_emails, notify, send_email
from app.models import PasswordReset, User
from app.security import hash_password, verify_password
from app.web import flash, render
from app.whatsapp import normalize_phone

router = APIRouter()

LOGIN_ROLES = ("advertiser", "publisher", "ca", "admin")
SIGNUP_ROLES = ("advertiser", "publisher")
OTP_MINUTES = 10
OTP_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60


@router.get("/login")
def login_page(request: Request, role: str = "publisher"):
    if request.session.get("user_id"):
        return RedirectResponse("/", 303)
    return render(request, "login.html", role=role if role in LOGIN_ROLES else "publisher")


@router.post("/login")
def login(request: Request, db: Session = Depends(get_db), email: str = Form(...),
          password: str = Form(...), role: str = Form("publisher")):
    role = role if role in LOGIN_ROLES else "publisher"
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or user.role != role or not verify_password(password, user.password_hash):
        flash(request, "Invalid email, password or account type.", "error")
        return RedirectResponse(f"/login?role={role}", 303)
    if user.status != "active":
        msg = "Your account is waiting for admin approval." if user.status == "pending" else "Your account is blocked."
        flash(request, msg, "error")
        return RedirectResponse(f"/login?role={role}", 303)

    request.session["user_id"] = user.id
    record(db, request, user, "login", "auth", user.id, user.id)
    return RedirectResponse("/", 303)


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = db.get(User, request.session.get("user_id") or 0)
    if user:
        record(db, request, user, "logout", "auth", user.id, user.id)
    request.session.clear()
    return RedirectResponse("/login", 303)


@router.get("/register")
def register_page(request: Request, role: str = "publisher"):
    return render(request, "register.html", role=role if role in SIGNUP_ROLES else "publisher", form={})


@router.post("/register")
def register(request: Request, background: BackgroundTasks, db: Session = Depends(get_db),
             role: str = Form(...), name: str = Form(...), email: str = Form(...), password: str = Form(...),
             company_name: str = Form(""), phone: str = Form("")):
    role = role if role in SIGNUP_ROLES else "publisher"
    email = email.strip().lower()
    form = {"name": name, "email": email, "company_name": company_name, "phone": phone}

    errors = []
    if len(password) < 6:
        errors.append("Password must be at least 6 characters.")
    if db.scalar(select(User.id).where(User.email == email)):
        errors.append("This email is already registered.")
    if errors:
        for e in errors:
            flash(request, e, "error")
        return render(request, "register.html", role=role, form=form)

    user = User(role=role, name=name.strip(), email=email, password_hash=hash_password(password),
                company_name=company_name.strip() or None, phone=phone.strip() or None, status="pending")
    db.add(user)
    db.commit()
    record(db, request, user, "register", "user", user.id, user.id, {"role": role, **form})

    background.add_task(send_email, [user.email], "Registration received - Adzhubmedia",
                        f"Hi {user.name},\n\nYour {role} account has been created and is waiting for admin approval.")
    background.add_task(send_email, admin_emails(db), f"New {role} registration: {user.name}",
                        f"Name: {user.name}\nEmail: {user.email}\nCompany: {user.company_name or '-'}\n"
                        f"Phone: {user.phone or '-'}\n\nApprove it from the Users section.")

    flash(request, "Registration successful. You can log in once the admin approves your account.")
    return RedirectResponse(f"/login?role={role}", 303)


@router.get("/profile")
def profile(request: Request, user: User = Depends(current_user)):
    return render(request, "profile.html")


@router.post("/profile")
def update_profile(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user),
                   name: str = Form(...), company_name: str = Form(""), phone: str = Form("")):
    changes = apply_changes(user, {"name": name.strip(), "company_name": company_name.strip() or None,
                                   "phone": phone.strip() or None})
    if changes:
        db.commit()
        record(db, request, user, "update", "user", user.id, user.id, {"changes": changes})
        flash(request, "Profile updated.")
    else:
        flash(request, "Nothing changed.", "info")
    return RedirectResponse("/profile", 303)


@router.post("/profile/password")
def change_password(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user),
                    current_password: str = Form(...), new_password: str = Form(...)):
    if not verify_password(current_password, user.password_hash):
        flash(request, "Current password is wrong.", "error")
    elif len(new_password) < 6:
        flash(request, "New password must be at least 6 characters.", "error")
    else:
        user.password_hash = hash_password(new_password)
        db.commit()
        record(db, request, user, "password", "user", user.id, user.id)
        flash(request, "Password changed.")
    return RedirectResponse("/profile", 303)


def find_accounts(db: Session, identifier: str) -> list[User]:
    """Accounts matching an email address or a phone number (phones are not unique)."""
    identifier = identifier.strip()
    if "@" in identifier:
        user = db.scalar(select(User).where(User.email == identifier.lower()))
        return [user] if user else []
    phone = normalize_phone(identifier)
    if not phone:
        return []
    return [u for u in db.scalars(select(User).where(User.phone.is_not(None)))
            if normalize_phone(u.phone) == phone]


def active_reset(db: Session, user_id: int) -> PasswordReset | None:
    return db.scalar(select(PasswordReset).where(PasswordReset.user_id == user_id, PasswordReset.used_at.is_(None))
                     .order_by(PasswordReset.id.desc()))


@router.get("/forgot-password")
def forgot_password_page(request: Request, role: str = "publisher"):
    return render(request, "forgot_password.html", role=role if role in LOGIN_ROLES else "publisher")


@router.post("/forgot-password")
def forgot_password(request: Request, background: BackgroundTasks, db: Session = Depends(get_db),
                    identifier: str = Form(...), role: str = Form("publisher")):
    matches = find_accounts(db, identifier)
    if len(matches) > 1:
        flash(request, "This phone number is linked to more than one account. Use your email instead.", "error")
        return RedirectResponse(f"/forgot-password?role={role}", 303)

    user = matches[0] if matches and matches[0].status != "blocked" else None
    request.session.pop("reset_user_id", None)
    if user:
        request.session["reset_user_id"] = user.id
        last = active_reset(db, user.id)
        if last and (now() - last.created_at).total_seconds() < OTP_RESEND_SECONDS:
            flash(request, "An OTP was sent less than a minute ago. Use that code or wait a minute.", "info")
            return RedirectResponse("/reset-password", 303)

        code = f"{secrets.randbelow(10**6):06d}"
        db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
                   .values(used_at=now()))
        db.add(PasswordReset(user_id=user.id, code_hash=hash_password(code),
                             expires_at=now() + timedelta(minutes=OTP_MINUTES)))
        db.commit()
        record(db, request, user, "forgot_password", "user", user.id, user.id,
               {"via": "email" if "@" in identifier else "phone"})
        notify(background, [user], "Password reset OTP - Adzhubmedia",
               f"Hi {user.name},\n\nYour password reset OTP is {code}. It is valid for {OTP_MINUTES} minutes.\n\n"
               f"If you did not request this, ignore this message.")

    flash(request, "If the account exists, a 6-digit OTP has been sent to its registered email "
                   "and WhatsApp number.")
    return RedirectResponse("/reset-password", 303)


@router.get("/reset-password")
def reset_password_page(request: Request):
    return render(request, "reset_password.html")


@router.post("/reset-password")
def reset_password(request: Request, background: BackgroundTasks, db: Session = Depends(get_db),
                   otp: str = Form(...), new_password: str = Form(...), confirm_password: str = Form(...)):
    uid = request.session.get("reset_user_id")
    reset = active_reset(db, uid) if uid else None
    if not reset or reset.expires_at < now():
        flash(request, "The OTP is invalid or has expired. Request a new one.", "error")
        return RedirectResponse("/forgot-password", 303)
    if len(new_password) < 6:
        flash(request, "New password must be at least 6 characters.", "error")
        return RedirectResponse("/reset-password", 303)
    if new_password != confirm_password:
        flash(request, "Passwords do not match.", "error")
        return RedirectResponse("/reset-password", 303)

    if not verify_password(otp.strip(), reset.code_hash):
        reset.attempts += 1
        left = OTP_ATTEMPTS - reset.attempts
        if left <= 0:
            reset.used_at = now()
        db.commit()
        if left <= 0:
            flash(request, "Too many wrong attempts. Request a new OTP.", "error")
            return RedirectResponse("/forgot-password", 303)
        flash(request, f"Wrong OTP. {left} attempt{'s' if left != 1 else ''} left.", "error")
        return RedirectResponse("/reset-password", 303)

    user = db.get(User, uid)
    user.password_hash = hash_password(new_password)
    reset.used_at = now()
    db.commit()
    request.session.pop("reset_user_id", None)
    record(db, request, user, "reset_password", "user", user.id, user.id)
    notify(background, [user], "Password changed - Adzhubmedia",
           f"Hi {user.name},\n\nYour password was reset using an OTP. "
           f"If this was not you, contact the admin immediately.")
    flash(request, "Password reset. You can now log in with the new password.")
    return RedirectResponse(f"/login?role={user.role}", 303)
