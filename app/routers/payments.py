import mimetypes
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.audit import apply_changes, record
from app.config import settings
from app.database import get_db
from app.deps import check_access, current_user, member_user, scoped, team_ids
from app.mailer import ca_emails, send_email, stakeholders
from app.models import ActivityLog, Client, Payment, PaymentFile, User
from app.web import PageError, flash, not_found, paginate, render

router = APIRouter(prefix="/payments")

DIRECTIONS = {"outgoing": "Outgoing (we pay)", "incoming": "Incoming (we receive)"}
STATUSES = ("pending", "processing", "completed", "failed", "cancelled")
FILE_KINDS = {"sent": "Sent / uploaded by us", "received": "Received / returned"}
DECISIONS = {"verified": "Payment received", "not_received": "Not received"}
ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".csv", ".xls", ".xlsx", ".doc", ".docx", ".txt", ".zip"}
DEFAULT_FOLLOW_UP = 30


def visible_clients(db: Session, user: User) -> list[Client]:
    return db.scalars(scoped(select(Client), Client.owner_id, team_ids(db, user)).order_by(Client.company_name)).all()


def get_payment(db: Session, user: User, pid: int, ca_ok: bool = False) -> Payment:
    payment = db.get(Payment, pid)
    if not payment:
        raise not_found("Payment")
    if user.role == "ca":
        if not ca_ok or not payment.verification_status:
            raise PageError()
    else:
        check_access(db, user, payment.owner_id)
    return payment


def read_form(form, clients: list[Client]) -> tuple[dict, Client | None, list[str]]:
    errors = []
    by_id = {c.id: c for c in clients}
    client_id = form.get("client_id") or ""
    client = by_id.get(int(client_id)) if client_id.isdigit() else None
    if not client:
        errors.append("Select a client.")

    try:
        amount = Decimal(form.get("amount") or "").quantize(Decimal("0.01"))
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        amount = None
        errors.append("Enter a valid amount greater than 0.")

    try:
        payment_date = date.fromisoformat(form.get("payment_date")) if form.get("payment_date") else None
    except ValueError:
        payment_date = None
        errors.append("Invalid payment date.")

    data = {
        "client_id": client.id if client else None,
        "direction": form.get("direction") if form.get("direction") in DIRECTIONS else "outgoing",
        "amount": amount,
        "currency": (form.get("currency") or "USD").strip().upper()[:10],
        "status": form.get("status") if form.get("status") in STATUSES else "pending",
        "reference": (form.get("reference") or "").strip() or None,
        "payment_date": payment_date,
        "notes": (form.get("notes") or "").strip() or None,
    }
    return data, client, errors


def read_follow_up(form) -> int:
    value = (form.get("follow_up_days") or "").strip()
    return min(int(value), 365) if value.isdigit() and int(value) > 0 else DEFAULT_FOLLOW_UP


async def save_files(payment: Payment, uploads, kind: str, user: User) -> tuple[list[PaymentFile], list[str]]:
    saved, errors = [], []
    for up in uploads:
        if not isinstance(up, UploadFile) or not up.filename:
            continue
        name = Path(up.filename).name[:255]
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            errors.append(f"{name}: file type not allowed.")
            continue
        content = await up.read()
        if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024:
            errors.append(f"{name}: larger than {settings.MAX_UPLOAD_MB} MB.")
            continue
        file = PaymentFile(kind=kind, original_name=name, stored_name=f"{uuid4().hex}{ext}", size=len(content),
                           content=content, uploaded_by=user.id)
        payment.files.append(file)
        saved.append(file)
    return saved, errors


def submit_for_verification(db: Session, request: Request, background: BackgroundTasks, user: User,
                            payment: Payment, days: int, note: str | None) -> None:
    previous = payment.verification_status
    payment.verification_status = "pending"
    payment.follow_up_days = days
    payment.submitted_at = datetime.now()
    payment.due_date = date.today() + timedelta(days=days)
    payment.submit_note = note
    payment.verified_amount = payment.verified_by = payment.verified_at = payment.verification_note = None
    payment.updated_by = user.id
    db.commit()
    record(db, request, user, "submit_verification", "payment", payment.id, payment.owner_id,
           {"previous": previous, "amount": payment.amount, "currency": payment.currency,
            "follow_up_days": days, "due_date": payment.due_date, "note": note})

    background.add_task(send_email, ca_emails(db),
                        f"Receipt for verification: {payment.client.company_name} - "
                        f"{payment.currency} {payment.amount:,.2f}",
                        f"{user.name} ({user.email}) submitted a receipt for verification.\n\n"
                        f"Client: {payment.client.company_name}\nAmount: {payment.currency} {payment.amount:,.2f}\n"
                        f"Reference: {payment.reference or '-'}\nFollow-up deadline: {payment.due_date} ({days} days)\n"
                        f"Note: {note or '-'}\n\nOpen the Verifications section to update it.")


def form_page(request, db, user, payment, form):
    return render(request, "payment_form.html", payment=payment, form=form, clients=visible_clients(db, user),
                  directions=DIRECTIONS, statuses=STATUSES, default_follow_up=DEFAULT_FOLLOW_UP)


@router.get("")
def list_payments(request: Request, client: str = "", direction: str = "", status: str = "", q: str = "",
                  page: int = 1, db: Session = Depends(get_db), user: User = Depends(member_user)):
    stmt = scoped(select(Payment), Payment.owner_id, team_ids(db, user))
    if client.isdigit():
        stmt = stmt.where(Payment.client_id == int(client))
    if direction:
        stmt = stmt.where(Payment.direction == direction)
    if status:
        stmt = stmt.where(Payment.status == status)
    if q:
        stmt = stmt.where(Payment.reference.ilike(f"%{q}%"))
    payments, pager = paginate(db, stmt.order_by(Payment.id.desc()), page)
    return render(request, "payments_list.html", payments=payments, pager=pager, clients=visible_clients(db, user),
                  directions=DIRECTIONS, statuses=STATUSES,
                  filters={"client": client, "direction": direction, "status": status, "q": q})


@router.get("/new")
def new_payment(request: Request, client: str = "", db: Session = Depends(get_db),
                user: User = Depends(member_user)):
    form = {"client_id": int(client) if client.isdigit() else None, "currency": "USD", "status": "pending"}
    return form_page(request, db, user, None, form)


@router.post("/new")
async def create_payment(request: Request, background: BackgroundTasks,
                         db: Session = Depends(get_db), user: User = Depends(member_user)):
    form = await request.form()
    data, client, errors = read_form(form, visible_clients(db, user))
    if errors:
        for e in errors:
            flash(request, e, "error")
        return form_page(request, db, user, None, data)

    payment = Payment(**data, owner_id=client.owner_id, created_by=user.id, updated_by=user.id)
    saved, file_errors = await save_files(payment, form.getlist("files"), "sent", user)
    db.add(payment)
    db.commit()
    record(db, request, user, "create", "payment", payment.id, payment.owner_id,
           {**data, "client": client.company_name, "files": [f.original_name for f in saved]})

    background.add_task(send_email, stakeholders(db, client.owner),
                        f"Payment recorded: {payment.currency} {payment.amount} - {client.company_name}",
                        f"{user.name} recorded a {payment.direction} payment.\n\nClient: {client.company_name}\n"
                        f"Amount: {payment.currency} {payment.amount}\nStatus: {payment.status}\n"
                        f"Reference: {payment.reference or '-'}")
    for e in file_errors:
        flash(request, e, "error")
    flash(request, "Payment created.")

    if form.get("send_for_verification") == "on":
        if saved:
            submit_for_verification(db, request, background, user, payment, read_follow_up(form),
                                    (form.get("submit_note") or "").strip() or None)
            flash(request, f"Sent to CA for verification. Follow-up due on {payment.due_date}.")
        else:
            flash(request, "Upload the receipt before sending it for verification.", "error")
    return RedirectResponse(f"/payments/{payment.id}", 303)


@router.get("/{pid}")
def payment_detail(pid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    payment = get_payment(db, user, pid, ca_ok=True)
    history = db.scalars(select(ActivityLog).where(ActivityLog.entity == "payment", ActivityLog.entity_id == pid)
                         .order_by(ActivityLog.id.desc())).all()
    return render(request, "payment_detail.html", payment=payment, history=history, directions=DIRECTIONS,
                  file_kinds=FILE_KINDS, decisions=DECISIONS, default_follow_up=DEFAULT_FOLLOW_UP)


@router.get("/{pid}/edit")
def edit_payment(pid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(member_user)):
    payment = get_payment(db, user, pid)
    return form_page(request, db, user, payment, vars(payment))


@router.post("/{pid}/edit")
async def update_payment(pid: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(member_user)):
    payment = get_payment(db, user, pid)
    data, client, errors = read_form(await request.form(), visible_clients(db, user))
    if errors:
        for e in errors:
            flash(request, e, "error")
        return form_page(request, db, user, payment, data)

    changes = apply_changes(payment, {**data, "owner_id": client.owner_id})
    if not changes:
        flash(request, "Nothing changed.", "info")
        return RedirectResponse(f"/payments/{pid}", 303)
    payment.updated_by = user.id
    db.commit()
    record(db, request, user, "update", "payment", pid, payment.owner_id, {"changes": changes})
    flash(request, "Payment updated.")
    return RedirectResponse(f"/payments/{pid}", 303)


@router.post("/{pid}/delete")
def delete_payment(pid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(member_user)):
    payment = get_payment(db, user, pid)
    snapshot = {"client": payment.client.company_name, "direction": payment.direction, "amount": payment.amount,
                "currency": payment.currency, "status": payment.status, "reference": payment.reference,
                "verification": payment.verification_status, "files": [f.original_name for f in payment.files]}
    stored = [f.stored_name for f in payment.files]
    owner_id = payment.owner_id
    db.delete(payment)
    db.commit()
    for name in stored:
        (settings.UPLOAD_DIR / name).unlink(missing_ok=True)
    record(db, request, user, "delete", "payment", pid, owner_id, snapshot)
    flash(request, "Payment deleted.")
    return RedirectResponse("/payments", 303)


@router.post("/{pid}/submit")
async def submit_verification(pid: int, request: Request, background: BackgroundTasks,
                              db: Session = Depends(get_db), user: User = Depends(member_user)):
    payment = get_payment(db, user, pid)
    if not payment.files:
        flash(request, "Upload the receipt before sending it for verification.", "error")
    elif payment.verification_status == "verified":
        flash(request, "This payment is already verified.", "error")
    else:
        form = await request.form()
        submit_for_verification(db, request, background, user, payment, read_follow_up(form),
                                (form.get("submit_note") or "").strip() or None)
        flash(request, f"Sent to CA for verification. Follow-up due on {payment.due_date}.")
    return RedirectResponse(f"/payments/{pid}", 303)


@router.post("/{pid}/verify")
async def verify_payment(pid: int, request: Request, background: BackgroundTasks,
                         db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role not in ("ca", "admin"):
        raise PageError()
    payment = get_payment(db, user, pid, ca_ok=True)
    form = await request.form()
    decision = form.get("decision")
    if not payment.verification_status or decision not in DECISIONS:
        flash(request, "Select whether the payment was received.", "error")
        return RedirectResponse(f"/payments/{pid}", 303)

    verified_amount = None
    if decision == "verified":
        try:
            verified_amount = Decimal(form.get("verified_amount") or payment.amount).quantize(Decimal("0.01"))
        except InvalidOperation:
            flash(request, "Enter a valid received amount.", "error")
            return RedirectResponse(f"/payments/{pid}", 303)

    changes = apply_changes(payment, {"verification_status": decision, "verified_amount": verified_amount,
                                      "verification_note": (form.get("verification_note") or "").strip() or None})
    if decision == "verified" and payment.status != "completed":
        changes["status"] = [payment.status, "completed"]
        payment.status = "completed"
    if not changes:
        flash(request, "Nothing changed.", "info")
        return RedirectResponse(f"/payments/{pid}", 303)

    payment.verified_by = user.id
    payment.verified_at = datetime.now()
    db.commit()
    record(db, request, user, "verify", "payment", pid, payment.owner_id, {"changes": changes})

    background.add_task(send_email, stakeholders(db, payment.owner),
                        f"Verification update: {payment.client.company_name} - {DECISIONS[decision]}",
                        f"{user.name} ({user.role.upper()}) updated payment #{payment.id}.\n\n"
                        f"Client: {payment.client.company_name}\nAmount: {payment.currency} {payment.amount:,.2f}\n"
                        f"Result: {DECISIONS[decision]}\n"
                        f"Received amount: {verified_amount if verified_amount is not None else '-'}\n"
                        f"Note: {payment.verification_note or '-'}")
    flash(request, f"Marked as: {DECISIONS[decision]}.")
    return RedirectResponse(f"/payments/{pid}", 303)


@router.post("/{pid}/files")
async def upload_files(pid: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(member_user)):
    payment = get_payment(db, user, pid)
    form = await request.form()
    kind = form.get("kind") if form.get("kind") in FILE_KINDS else "sent"
    saved, errors = await save_files(payment, form.getlist("files"), kind, user)
    for e in errors:
        flash(request, e, "error")
    if saved:
        payment.updated_by = user.id
        db.commit()
        record(db, request, user, "upload", "payment", pid, payment.owner_id,
               {"kind": kind, "files": [f.original_name for f in saved]})
        flash(request, f"{len(saved)} file(s) uploaded.")
    elif not errors:
        flash(request, "Choose at least one file.", "error")
    return RedirectResponse(f"/payments/{pid}", 303)


def get_file(db: Session, user: User, fid: int, ca_ok: bool = False) -> PaymentFile:
    file = db.get(PaymentFile, fid)
    if not file:
        raise not_found("File")
    get_payment(db, user, file.payment_id, ca_ok)
    return file


@router.get("/files/{fid}")
def download_file(fid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    file = get_file(db, user, fid, ca_ok=True)
    path = settings.UPLOAD_DIR / file.stored_name
    if file.content is None and not path.exists():
        raise not_found("File")
    record(db, request, user, "download", "payment", file.payment_id, file.payment.owner_id,
           {"file": file.original_name, "kind": file.kind})
    if file.content is None:
        return FileResponse(path, filename=file.original_name)
    media_type = mimetypes.guess_type(file.original_name)[0] or "application/octet-stream"
    return Response(file.content, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file.original_name)}"})


@router.post("/files/{fid}/delete")
def delete_file(fid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(member_user)):
    file = get_file(db, user, fid)
    pid, owner_id = file.payment_id, file.payment.owner_id
    details = {"file": file.original_name, "kind": file.kind}
    stored = file.stored_name
    db.delete(file)
    db.commit()
    (settings.UPLOAD_DIR / stored).unlink(missing_ok=True)
    record(db, request, user, "remove_file", "payment", pid, owner_id, details)
    flash(request, f"{details['file']} removed.")
    return RedirectResponse(f"/payments/{pid}", 303)
