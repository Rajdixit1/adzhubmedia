# Adzhubmedia Panel

FastAPI + SQLAlchemy + Jinja2 panel for advertisers, publishers and admin.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # edit SECRET_KEY, admin login, SMTP
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. A default admin is created on first start from `ADMIN_EMAIL` / `ADMIN_PASSWORD`
(default `admin@adzhubmedia.com` / `admin123`, change it).

## Deploy on Render

1. Render dashboard → **New → Blueprint** → select the `Rajdixit1/adzhubmedia` repo.
2. Render reads `render.yaml` and creates the web service and a PostgreSQL database.
3. Fill the prompted values: `ADMIN_PASSWORD` (required), SMTP and WhatsApp settings (optional, can be added later).
4. Click **Apply**. Every push to `main` redeploys automatically.

Uploaded files are stored in the database, so they survive redeploys.
Note: the free Render PostgreSQL expires after 30 days. Upgrade the database plan before using it for real data.

## Features

| Area | What it does |
|---|---|
| Login / Register | Separate tabs for Publisher and Advertiser (and Admin login). New registrations are `pending` until admin approves. |
| Users (admin) | Create, edit, approve, block, delete accounts. Mark an account as **manager** with a max child limit (e.g. 5) and assign child accounts of the same type. |
| My Team (manager) | A manager sees all clients, payments and logs of their child accounts. |
| Clients | Onboard client companies: company name, registration no., tax ID, website, contact person, address, finance/bank details. Add, edit, delete with full change history. |
| Payments | Outgoing / incoming payments per client with status, reference, date, notes. Upload files we send (invoice, proof) and files received back (receipts, remittance). Download and remove files. |
| CA verification | After uploading a receipt, the owner sends it to the CA with a follow-up period (default 30 days). The CA marks it **Payment received** (with received amount) or **Not received**. Not-received receipts can be resent with a new deadline. |
| Verifications | Colour-coded queue: blue = days left, amber = due within 5 days, red = overdue (shows days overdue) or not received, green = received. Filter by state, client, period. |
| Revenue Report | Per period (today, 7/30 days, this/last month, year, custom): totals, by account and by client. Shows revenue, outgoing, amount sent for validation, pending, verified, not received. |
| Logs | Every login, logout, register, approve, create, update (old → new value), delete, upload, download is stored in the DB and in log files. Filter by action, section, record, user, date. |
| Email | Sent on registration, approval, new client onboarded and new payment (to owner, their manager and admins). Approval emails name the approving admin and set Reply-To to their address. If `SMTP_HOST` is empty, emails are written to `logs/emails.log`. |
| WhatsApp | Sent to the registered phone on approval, account creation, password reset OTP, receipt sent to CA (to the CA and the owner) and CA decision (to the owner). Set `WHATSAPP_PROVIDER` to `twilio` or `meta`; if empty, messages are written to `logs/whatsapp.log`. 10-digit numbers get `DEFAULT_COUNTRY_CODE` (91). |
| Forgot password | "Forgot password?" on the login page. Enter email or phone; a 6-digit OTP goes to the registered email and WhatsApp. Valid 10 minutes, 5 attempts, one OTP per minute. |

## Access rules

- **Admin**: sees and manages everything.
- **Manager (parent)**: own data + data of child accounts.
- **Normal advertiser / publisher**: only own data.
- **CA** (created by admin): only the Verifications section. Sees receipts sent for verification and updates their result.

## Files

- `logs/app.log`: all activity (admin can download it from the Logs page)
- `logs/users/user_<id>.log`: activity per user (each user downloads their own)
- `logs/emails.log`: email delivery log
- `logs/whatsapp.log`: WhatsApp delivery log
- `uploads/`: payment attachments (stored with random names)
- `adzhub.db`: SQLite database (set `DATABASE_URL` for PostgreSQL/MySQL)
