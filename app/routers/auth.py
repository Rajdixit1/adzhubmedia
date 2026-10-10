from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import apply_changes, record
from app.database import get_db
from app.deps import current_user
from app.mailer import admin_emails, send_email
from app.models import User
from app.security import hash_password, verify_password
from app.web import flash, render

router = APIRouter()

LOGIN_ROLES = ("advertiser", "publisher", "ca", "admin")
SIGNUP_ROLES = ("advertiser", "publisher")


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
