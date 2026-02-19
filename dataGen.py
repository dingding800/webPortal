[5.3codex] Perfect — here’s a practical bridge script you can use as a starting point to generate `aml_portal.db` from your existing tables (`client_information`, `address`, `phone`, `ip_log`, `transactions`, `alert`, `case`).

Create: `backend/scripts/bridge_to_portal_db.py`

```python
from datetime import datetime, date
from uuid import uuid4
import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.entities import (
    Client, Transaction, RiskResult,
    ClientAddressHistory, ClientPhoneHistory,
    LoginActivity, Alert, Case
)

# ---------- Config ----------
SOURCE_DB_URL = os.getenv("SOURCE_DB_URL")  # e.g. postgresql+psycopg://user:pass@host:5432/db
TARGET_DB_PATH = os.getenv("TARGET_DB_PATH", "./data/aml_portal.db")
TARGET_DB_URL = f"sqlite:///{TARGET_DB_PATH}"

# ---------- Helpers ----------
def to_date(v, default=date(1990, 1, 1)):
    if v is None:
        return default
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)).date()
    except Exception:
        return default

def to_dt(v, default=None):
    if default is None:
        default = datetime.utcnow()
    if v is None:
        return default
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return default

def norm_client_id(v):
    s = str(v).strip()
    if not s.startswith("C"):
        return f"C{s.zfill(7)}"
    return s

def norm_case_id(v):
    s = str(v).strip() if v is not None else ""
    return s or f"CASE-{uuid4().hex[:20]}"

# ---------- Extract ----------
def extract_clients(src_conn):
    # TODO: adjust column names in your source DB
    sql = text("""
        SELECT
          c.client_id,
          c.full_name,
          c.dob,
          c.gender,
          c.country,
          c.city,
          c.segment,
          c.occupation,
          c.annual_income,
          c.account_open_date,
          c.pep_flag,
          c.sanctions_flag,
          c.profile_text,
          c.risk_rating
        FROM client_information c
    """)
    return src_conn.execute(sql).mappings().all()

def extract_addresses(src_conn):
    sql = text("""
        SELECT
          a.client_id,
          a.address_line,
          a.city,
          a.country,
          a.from_date,
          a.to_date
        FROM address a
    """)
    return src_conn.execute(sql).mappings().all()

def extract_phones(src_conn):
    sql = text("""
        SELECT
          p.client_id,
          p.phone,
          p.from_date,
          p.to_date
        FROM phone p
    """)
    return src_conn.execute(sql).mappings().all()

def extract_ip_logs(src_conn):
    sql = text("""
        SELECT
          l.log_id,
          l.client_id,
          l.ip_address,
          l.ip_country,
          l.status,
          l.channel,
          l.logged_in_at
        FROM ip_log l
    """)
    return src_conn.execute(sql).mappings().all()

def extract_transactions(src_conn):
    sql = text("""
        SELECT
          t.tx_id,
          t.client_id,
          t.counterparty_id,
          t.tx_type,
          t.direction,
          t.amount,
          t.currency,
          t.channel,
          t.country,
          t.timestamp,
          t.typology_tags
        FROM transactions t
    """)
    return src_conn.execute(sql).mappings().all()

def extract_cases(src_conn):
    # case is a reserved word in some DBs; quote if needed in your source
    sql = text("""
        SELECT
          c.case_id,
          c.client_id,
          c.status,
          c.opened_at,
          c.closed_at,
          c.title
        FROM "case" c
    """)
    return src_conn.execute(sql).mappings().all()

def extract_alerts(src_conn):
    sql = text("""
        SELECT
          a.alert_id,
          a.client_id,
          a.case_id,
          a.severity,
          a.status,
          a.created_at,
          a.description
        FROM alert a
    """)
    return src_conn.execute(sql).mappings().all()

# ---------- Load ----------
def main():
    if not SOURCE_DB_URL:
        raise RuntimeError("Set SOURCE_DB_URL first.")

    src_engine = create_engine(SOURCE_DB_URL, pool_pre_ping=True)
    tgt_engine = create_engine(TARGET_DB_URL, pool_pre_ping=True)

    # create target schema
    Base.metadata.create_all(bind=tgt_engine)
    SessionLocal = sessionmaker(bind=tgt_engine, autocommit=False, autoflush=False)
    db = SessionLocal()

    try:
        # clear target (order matters)
        db.query(Alert).delete()
        db.query(Case).delete()
        db.query(LoginActivity).delete()
        db.query(Transaction).delete()
        db.query(ClientPhoneHistory).delete()
        db.query(ClientAddressHistory).delete()
        db.query(RiskResult).delete()
        db.query(Client).delete()
        db.commit()

        with src_engine.connect() as src:
            clients = extract_clients(src)
            addresses = extract_addresses(src)
            phones = extract_phones(src)
            ip_logs = extract_ip_logs(src)
            txs = extract_transactions(src)
            cases = extract_cases(src)
            alerts = extract_alerts(src)

        # clients
        client_objs = []
        risk_objs = []
        for r in clients:
            cid = norm_client_id(r["client_id"])
            c = Client(
                client_id=cid,
                full_name=(r.get("full_name") or "Unknown Client")[:120],
                dob=to_date(r.get("dob")),
                gender=(r.get("gender") or "X")[:16],
                country=(r.get("country") or "Unknown")[:64],
                city=(r.get("city") or "Unknown")[:64],
                segment=(r.get("segment") or "retail")[:40],
                occupation=(r.get("occupation") or "unknown")[:80],
                annual_income=float(r.get("annual_income") or 0),
                account_open_date=to_date(r.get("account_open_date"), default=date.today()),
                pep_flag=int(r.get("pep_flag") or 0),
                sanctions_flag=int(r.get("sanctions_flag") or 0),
                profile_text=(r.get("profile_text") or ""),
                risk_rating=(r.get("risk_rating") or "Standard")[:16],
            )
            client_objs.append(c)

            # minimal risk row so analytics works
            rr = RiskResult(
                client_id=cid,
                risk_score=75.0 if c.risk_rating.lower().startswith("high") else 35.0,
                rule_hits={},
                model_reason="Imported from production source",
                last_updated=datetime.utcnow(),
            )
            risk_objs.append(rr)

        db.bulk_save_objects(client_objs)
        db.bulk_save_objects(risk_objs)
        db.commit()

        # address history
        addr_objs = []
        for r in addresses:
            addr_objs.append(ClientAddressHistory(
                client_id=norm_client_id(r["client_id"]),
                address_line=(r.get("address_line") or "")[:160],
                city=(r.get("city") or "Unknown")[:64],
                country=(r.get("country") or "Unknown")[:64],
                from_date=to_date(r.get("from_date"), default=date.today()),
                to_date=to_date(r.get("to_date")) if r.get("to_date") else None,
            ))
        if addr_objs:
            db.bulk_save_objects(addr_objs)
            db.commit()

        # phone history
        phone_objs = []
        for r in phones:
            phone_objs.append(ClientPhoneHistory(
                client_id=norm_client_id(r["client_id"]),
                phone=(r.get("phone") or "")[:40],
                from_date=to_date(r.get("from_date"), default=date.today()),
                to_date=to_date(r.get("to_date")) if r.get("to_date") else None,
            ))
        if phone_objs:
            db.bulk_save_objects(phone_objs)
            db.commit()

        # login activity
        login_objs = []
        for r in ip_logs:
            login_objs.append(LoginActivity(
                login_id=str(r.get("log_id") or f"LG-{uuid4().hex[:24]}")[:40],
                client_id=norm_client_id(r["client_id"]),
                ip_address=(r.get("ip_address") or "0.0.0.0")[:64],
                ip_country=(r.get("ip_country") or "UNK")[:8],
                status=(r.get("status") or "success")[:16],
                channel=(r.get("channel") or "web")[:24],
                logged_in_at=to_dt(r.get("logged_in_at")),
            ))
        if login_objs:
            db.bulk_save_objects(login_objs)
            db.commit()

        # transactions
        tx_objs = []
        for r in txs:
            tx_objs.append(Transaction(
                tx_id=str(r.get("tx_id") or uuid4().hex)[:32],
                client_id=norm_client_id(r["client_id"]),
                counterparty_id=str(r.get("counterparty_id") or "CP000000")[:24],
                tx_type=(r.get("tx_type") or "wire")[:16],
                direction=(r.get("direction") or "outgoing")[:16],
                amount=float(r.get("amount") or 0),
                currency=(r.get("currency") or "USD")[:8],
                channel=(r.get("channel") or "web")[:24],
                country=(r.get("country") or "Unknown")[:64],
                timestamp=to_dt(r.get("timestamp")),
                typology_tags=r.get("typology_tags") or {},
            ))
        if tx_objs:
            db.bulk_save_objects(tx_objs)
            db.commit()

        # cases
        case_objs = []
        for r in cases:
            case_objs.append(Case(
                case_id=norm_case_id(r.get("case_id")),
                client_id=norm_client_id(r["client_id"]),
                status=(r.get("status") or "Open")[:20],
                opened_at=to_dt(r.get("opened_at")),
                closed_at=to_dt(r.get("closed_at")) if r.get("closed_at") else None,
                title=(r.get("title") or "Imported case")[:180],
            ))
        if case_objs:
            db.bulk_save_objects(case_objs)
            db.commit()

        # alerts
        alert_objs = []
        for r in alerts:
            alert_objs.append(Alert(
                alert_id=str(r.get("alert_id") or f"AL-{uuid4().hex[:22]}")[:40],
                client_id=norm_client_id(r["client_id"]),
                case_id=norm_case_id(r.get("case_id")) if r.get("case_id") else None,
                severity=(r.get("severity") or "Medium")[:16],
                status=(r.get("status") or "Open")[:16],
                created_at=to_dt(r.get("created_at")),
                description=(r.get("description") or "Imported alert"),
            ))
        if alert_objs:
            db.bulk_save_objects(alert_objs)
            db.commit()

        print("Bridge load complete.")
        print(f"Target DB: {TARGET_DB_PATH}")
        print(f"Clients: {len(client_objs)}, Tx: {len(tx_objs)}, Alerts: {len(alert_objs)}, Cases: {len(case_objs)}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
```

Run it like this (from project root):

```cmd
set PYTHONPATH=backend
set SOURCE_DB_URL=postgresql+psycopg://<user>:<pass>@<host>:5432/<db>
set TARGET_DB_PATH=./data/aml_portal.db
python backend\scripts\bridge_to_portal_db.py
```

If you want, next step I can tailor the `SELECT` statements exactly to your real source column names so it runs without edits.
