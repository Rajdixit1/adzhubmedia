from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit import apply_changes, record
from app.database import get_db
from app.deps import admin_user, current_user
from app.mailer import notify
from app.models import Client, Payment, User
from app.security import hash_password
from app.web import PageError, flash, not_found, paginate, render

router = APIRouter(prefix="/users")

ROLES = ("admin", "advertiser", "publisher", "ca")
STATUSES = ("pending", "active", "blocked")


def child_count(db: Session, parent_id: int, exclude_id: int | None = None) -> int:
    stmt = select(func.count(User.id)).where(User.parent_id == parent_id)
    if exclude_id:
        stmt = stmt.where(User.id != exclude_id)
    return db.scalar(stmt)


def read_user_form(db: Session, form, target: User | None):
    max_children = form.get("max_children") or "0"
    parent_id = form.get("parent_id") or ""
    data = {
        "name": (form.get("name") or "").strip(),
        "email": (form.get("email") or "").strip().lower(),
        "company_name": (form.get("company_name") or "").strip() or None,
        "phone": (form.get("phone") or "").strip() or None,
        "role": form.get("role"),
        "status": form.get("status"),
        "is_manager": form.get("is_manager") == "on",
        "max_children": int(max_children) if max_children.isdigit() else 0,
        "parent_id": int(parent_id) if parent_id.isdigit() else None,
    }
    password = form.get("password") or ""
    errors = []

    if not data["name"] or not data["email"]:
        errors.append("Name and email are required.")
    if data["role"] not in ROLES or data["status"] not in STATUSES:
        errors.append("Invalid role or status.")
    if db.scalar(select(User.id).where(User.email == data["email"], User.id != (target.id if target else 0))):
        errors.append("Email is already used by another account.")
    if (target is None or password) and len(password) < 6:
        errors.append("Password must be at least 6 characters.")
    if target and target.children and data["role"] != target.role:
        errors.append("Remove child accounts before changing the role of a manager.")

    if data["role"] == "ca":
        existing_ca = db.scalar(select(User).where(User.role == "ca", User.id != (target.id if target else 0)))
        if existing_ca:
            errors.append(f"Only one CA account is allowed. {existing_ca.email} is already the CA.")

    if data["role"] in ("admin", "ca"):
        data.update(is_manager=False, max_children=0, parent_id=None)
    if not data["is_manager"]:
        data["max_children"] = 0

    if data["parent_id"]:
        parent = db.get(User, data["parent_id"])
        if data["is_manager"]:
            errors.append("A manager account cannot also be a child account.")
        elif not parent or not parent.is_manager or parent.role != data["role"] or (target and parent.id == target.id):
            errors.append("Parent must be a manager with the same account type.")
        else:
            used = child_count(db, parent.id, target.id if target else None)
            if used >= parent.max_children:
                errors.append(f"{parent.name} already manages {used}/{parent.max_children} accounts.")

    if target and data["is_manager"]:
        current = child_count(db, target.id)
        if current > data["max_children"]:
            errors.append(f"This manager already has {current} child accounts; limit cannot be lower.")
    return data, password, errors


def form_context(db: Session):
    managers = db.scalars(select(User).where(User.is_manager.is_(True)).order_by(User.role, User.name)).all()
    ca_user = db.scalar(select(User).where(User.role == "ca"))
    return {"managers": managers, "roles": ROLES, "statuses": STATUSES, "ca_user": ca_user}


def login_url(request: Request, target: User) -> str:
    return f"{str(request.base_url).rstrip('/')}/login?role={target.role}"


def approval_mail(request: Request, background: BackgroundTasks, target: User, admin: User):
    notify(background, [target], "Your account is approved - Adzhubmedia",
           f"Hi {target.name},\n\nYour {target.role} account has been approved by {admin.name} ({admin.email}) "
           f"and is now active.\n\nLogin email: {target.email}\nLogin here: {login_url(request, target)}",
           reply_to=admin.email)


def welcome_mail(request: Request, background: BackgroundTasks, target: User, admin: User):
    notify(background, [target], "Your account has been created - Adzhubmedia",
           f"Hi {target.name},\n\n{admin.name} ({admin.email}) created a {target.role} account for you.\n\n"
           f"Login email: {target.email}\nLogin here: {login_url(request, target)}\n\n"
           f"Get the password from the admin, or use \"Forgot password?\" on the login page to set your own.",
           reply_to=admin.email)


@router.get("")
def list_users(request: Request, role: str = "", status: str = "", q: str = "", page: int = 1,
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role != "admin" and not user.is_manager:
        raise PageError()
    stmt = select(User)
    if user.role != "admin":
        stmt = stmt.where(User.parent_id == user.id)
    if role:
        stmt = stmt.where(User.role == role)
    if status:
        stmt = stmt.where(User.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(User.name.ilike(like), User.email.ilike(like), User.company_name.ilike(like)))
    users, pager = paginate(db, stmt.order_by(User.id.desc()), page)
    return render(request, "users_list.html", users=users, pager=pager,
                  filters={"role": role, "status": status, "q": q}, roles=ROLES, statuses=STATUSES)


@router.get("/new")
def new_user(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    return render(request, "user_form.html", target=None, form={"role": "publisher", "status": "active"},
                  **form_context(db))


@router.post("/new")
async def create_user(request: Request, background: BackgroundTasks,
                      db: Session = Depends(get_db), user: User = Depends(admin_user)):
    data, password, errors = read_user_form(db, await request.form(), None)
    if errors:
        for e in errors:
            flash(request, e, "error")
        return render(request, "user_form.html", target=None, form=data, **form_context(db))

    target = User(**data, password_hash=hash_password(password))
    db.add(target)
    db.commit()
    record(db, request, user, "create", "user", target.id, target.id, data)
    if target.status == "active":
        welcome_mail(request, background, target, user)
    flash(request, f"Account {target.email} created.")
    return RedirectResponse("/users", 303)


@router.get("/{uid}/edit")
def edit_user(uid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    target = db.get(User, uid)
    if not target:
        raise not_found("User")
    return render(request, "user_form.html", target=target, form=vars(target), **form_context(db))


@router.post("/{uid}/edit")
async def update_user(uid: int, request: Request, background: BackgroundTasks,
                      db: Session = Depends(get_db), user: User = Depends(admin_user)):
    target = db.get(User, uid)
    if not target:
        raise not_found("User")
    data, password, errors = read_user_form(db, await request.form(), target)
    if errors:
        for e in errors:
            flash(request, e, "error")
        return render(request, "user_form.html", target=target, form=data, **form_context(db))

    was_pending = target.status == "pending"
    changes = apply_changes(target, data)
    if password:
        target.password_hash = hash_password(password)
        changes["password"] = ["***", "***"]
    if not target.is_manager and target.children:
        changes["detached_children"] = [[c.email for c in target.children], []]
        for child in target.children:
            child.parent_id = None

    if not changes:
        flash(request, "Nothing changed.", "info")
        return RedirectResponse(f"/users/{uid}/edit", 303)
    db.commit()
    record(db, request, user, "update", "user", target.id, target.id, {"changes": changes})
    if was_pending and target.status == "active":
        approval_mail(request, background, target, user)
    flash(request, "Account updated.")
    return RedirectResponse("/users", 303)


@router.post("/{uid}/approve")
def approve_user(uid: int, request: Request, background: BackgroundTasks,
                 db: Session = Depends(get_db), user: User = Depends(admin_user)):
    target = db.get(User, uid)
    if not target:
        raise not_found("User")
    if target.status != "active":
        old = target.status
        target.status = "active"
        db.commit()
        record(db, request, user, "approve", "user", target.id, target.id, {"changes": {"status": [old, "active"]}})
        approval_mail(request, background, target, user)
        flash(request, f"{target.email} approved.")
    return RedirectResponse("/users", 303)


@router.post("/{uid}/delete")
def delete_user(uid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    target = db.get(User, uid)
    if not target:
        raise not_found("User")
    in_use = (db.scalar(select(func.count(Client.id)).where(Client.owner_id == uid))
              or db.scalar(select(func.count(Payment.id)).where(Payment.owner_id == uid)))
    if target.id == user.id:
        flash(request, "You cannot delete your own account.", "error")
    elif in_use or target.children:
        flash(request, "This account has clients, payments or child accounts. Block it instead.", "error")
    else:
        snapshot = {"email": target.email, "name": target.name, "role": target.role}
        db.delete(target)
        db.commit()
        record(db, request, user, "delete", "user", uid, uid, snapshot)
        flash(request, f"{snapshot['email']} deleted.")
    return RedirectResponse("/users", 303)
