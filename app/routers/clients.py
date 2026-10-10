from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit import apply_changes, record
from app.database import get_db
from app.deps import check_access, member_user, pick_owner, scoped, team_ids, team_users
from app.mailer import send_email, stakeholders
from app.models import ActivityLog, Client, User
from app.web import flash, not_found, paginate, render

router = APIRouter(prefix="/clients")

# (field, label, required)
FIELD_GROUPS = [
    ("Company", [("company_name", "Company Name", True), ("registration_no", "Company Registration No.", False),
                 ("tax_id", "Tax ID (GST / VAT / PAN)", False), ("website", "Website", False)]),
    ("Contact Person", [("contact_person", "Contact Person", True), ("email", "Email", True),
                        ("phone", "Phone", False)]),
    ("Address", [("address", "Address", False), ("city", "City", False), ("state", "State", False),
                 ("country", "Country", False), ("postal_code", "Postal Code", False)]),
    ("Finance", [("bank_name", "Bank Name", False), ("account_name", "Account Holder Name", False),
                 ("account_number", "Account Number / IBAN", False), ("swift_ifsc", "SWIFT / IFSC", False),
                 ("currency", "Currency", False), ("payment_terms", "Payment Terms (e.g. Net 30)", False)]),
]
FIELDS = [f for _, group in FIELD_GROUPS for f in group]


def read_form(form) -> tuple[dict, list[str]]:
    data = {name: (form.get(name) or "").strip() or None for name, _, _ in FIELDS}
    errors = [f"{label} is required." for name, label, required in FIELDS if required and not data[name]]
    return data, errors


def get_client(db: Session, user: User, cid: int) -> Client:
    client = db.get(Client, cid)
    if not client:
        raise not_found("Client")
    check_access(db, user, client.owner_id)
    return client


def form_page(request, db, user, client, form, owner_id):
    return render(request, "client_form.html", client=client, form=form, groups=FIELD_GROUPS,
                  owners=team_users(db, user), owner_id=owner_id)


@router.get("")
def list_clients(request: Request, q: str = "", owner: str = "", page: int = 1,
                 db: Session = Depends(get_db), user: User = Depends(member_user)):
    stmt = scoped(select(Client), Client.owner_id, team_ids(db, user))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Client.company_name.ilike(like), Client.contact_person.ilike(like),
                              Client.email.ilike(like), Client.tax_id.ilike(like)))
    if owner.isdigit():
        stmt = stmt.where(Client.owner_id == int(owner))
    clients, pager = paginate(db, stmt.order_by(Client.id.desc()), page)
    return render(request, "clients_list.html", clients=clients, pager=pager,
                  owners=team_users(db, user), filters={"q": q, "owner": owner})


@router.get("/new")
def new_client(request: Request, db: Session = Depends(get_db), user: User = Depends(member_user)):
    return form_page(request, db, user, None, {}, user.id)


@router.post("/new")
async def create_client(request: Request, background: BackgroundTasks,
                        db: Session = Depends(get_db), user: User = Depends(member_user)):
    form = await request.form()
    data, errors = read_form(form)
    owner_id = pick_owner(db, user, form.get("owner_id"))
    if errors:
        for e in errors:
            flash(request, e, "error")
        return form_page(request, db, user, None, data, owner_id)

    client = Client(**data, owner_id=owner_id, created_by=user.id, updated_by=user.id)
    db.add(client)
    db.commit()
    record(db, request, user, "create", "client", client.id, owner_id, data)

    background.add_task(send_email, stakeholders(db, client.owner), f"New client onboarded: {client.company_name}",
                        f"{user.name} ({user.email}) onboarded a new client.\n\n"
                        f"Company: {client.company_name}\nContact: {client.contact_person} <{client.email}>\n"
                        f"Tax ID: {client.tax_id or '-'}\nRegistration: {client.registration_no or '-'}\n"
                        f"Owner: {client.owner.name} ({client.owner.role})")
    flash(request, "Client created.")
    return RedirectResponse(f"/clients/{client.id}", 303)


@router.get("/{cid}")
def client_detail(cid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(member_user)):
    client = get_client(db, user, cid)
    history = db.scalars(select(ActivityLog).where(ActivityLog.entity == "client", ActivityLog.entity_id == cid)
                         .order_by(ActivityLog.id.desc())).all()
    return render(request, "client_detail.html", client=client, groups=FIELD_GROUPS, history=history)


@router.get("/{cid}/edit")
def edit_client(cid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(member_user)):
    client = get_client(db, user, cid)
    return form_page(request, db, user, client, vars(client), client.owner_id)


@router.post("/{cid}/edit")
async def update_client(cid: int, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(member_user)):
    client = get_client(db, user, cid)
    form = await request.form()
    data, errors = read_form(form)
    owner_id = pick_owner(db, user, form.get("owner_id"))
    if errors:
        for e in errors:
            flash(request, e, "error")
        return form_page(request, db, user, client, data, owner_id)

    changes = apply_changes(client, {**data, "owner_id": owner_id})
    if not changes:
        flash(request, "Nothing changed.", "info")
        return RedirectResponse(f"/clients/{cid}", 303)
    client.updated_by = user.id
    db.commit()
    record(db, request, user, "update", "client", cid, client.owner_id, {"changes": changes})
    flash(request, "Client updated.")
    return RedirectResponse(f"/clients/{cid}", 303)


@router.post("/{cid}/delete")
def delete_client(cid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(member_user)):
    client = get_client(db, user, cid)
    if client.payments:
        flash(request, "This client has payments. Delete the payments first.", "error")
        return RedirectResponse(f"/clients/{cid}", 303)
    snapshot = {name: getattr(client, name) for name, _, _ in FIELDS}
    owner_id = client.owner_id
    db.delete(client)
    db.commit()
    record(db, request, user, "delete", "client", cid, owner_id, snapshot)
    flash(request, f"Client {snapshot['company_name']} deleted.")
    return RedirectResponse("/clients", 303)
