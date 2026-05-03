import csv
import json
import os
from calendar import monthrange
import math
import threading
import time
from collections import Counter, defaultdict
from datetime import date as date_cls, datetime, timedelta
from io import StringIO
from pathlib import Path
from uuid import uuid4

from flask import Flask, flash, redirect, render_template, request, url_for
from peewee import JOIN

from models import (
    Allocation,
    Envelope,
    Goal,
    Setting,
    Subscription,
    Transaction,
    Transfer,
    db,
)
from runtime_paths import APP_ROOT, DB_PATH, STATIC_DIR, TEMPLATES_DIR

NAV_ITEMS = [
    {"endpoint": "dashboard", "key": "dashboard", "label": "Dashboard"},
    {"endpoint": "envelopes_page", "key": "envelopes", "label": "Envelopes"},
    {"endpoint": "allocate_funds", "key": "allocate", "label": "Allocate Funds"},
    {"endpoint": "add_expense_income", "key": "add", "label": "Add Expense/Income"},
    {"endpoint": "transaction_history", "key": "history", "label": "Transaction History"},
    {"endpoint": "goals_planning", "key": "goals", "label": "Goals Planning"},
    {"endpoint": "subscriptions_page", "key": "subscriptions", "label": "Subscriptions"},
    {"endpoint": "stats_trends", "key": "stats", "label": "Statistics & Trends"},
    {"endpoint": "settings_page", "key": "settings", "label": "Settings"},
]
GOAL_FREQUENCIES = {
    "day": "Daily",
    "week": "Weekly",
    "biweekly": "Biweekly",
    "month": "Monthly",
    "year": "Yearly",
}
STATS_RANGE_OPTIONS = [
    {"value": "week", "label": "This Week"},
    {"value": "month", "label": "This Month"},
    {"value": "year", "label": "This Year"},
    {"value": "all", "label": "All Time"},
]
TRANSACTION_TYPES = {"EXPENSE", "INCOME"}
GOAL_COUNT_DAYS = {
    "day": 1,
    "week": 7,
    "biweekly": 14,
    "year": 365,
}
SUBSCRIPTION_FREQUENCIES = {
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "yearly": "Yearly",
}
SUBSCRIPTION_ENVELOPE_NAMES = {"subscription", "subscriptions"}
SUBSCRIPTION_KEYWORDS = (
    "subscription",
    "netflix",
    "hulu",
    "spotify",
    "disney",
    "max",
    "hbo",
    "peacock",
    "paramount",
    "youtube",
    "prime",
    "membership",
    "gym",
    "apple music",
    "apple one",
    "audible",
)

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)
app.secret_key = os.environ.get("BUDGET_APP_SECRET_KEY", "dev-secret-key-change-me")
app.config["DEV_TOOLS"] = True
app.config["CAN_CLEAR_DATA"] = True
app.config["DATABASE_PATH"] = str(DB_PATH)
app.config["CAN_CLOSE_APP"] = False
app.config["REQUEST_APP_SHUTDOWN"] = None

SETTINGS_DEFAULTS = {
    "auto_recurring_deposits": "0",
    "auto_retry_recurring_deposits": "0",
    "dashboard_double_click_fund_negative": "1",
    "prevent_negative_envelopes": "0",
}
RECURRING_DUE_DESTINATION_TYPE = "recurring_due"
SUBSCRIPTIONS_ENVELOPE_NAME = "Subscriptions"
IMPORT_PREVIEW_DIR = APP_ROOT / ".csv_import_previews"
IMPORT_READY_STATUSES = {"", "posted", "complete", "completed", "settled"}


def schedule_app_shutdown(callback, delay_seconds=0.35):
    def _delayed_shutdown():
        time.sleep(delay_seconds)
        callback()

    threading.Thread(
        target=_delayed_shutdown,
        name="budget-app-shutdown",
        daemon=True,
    ).start()


# Shared templates
@app.template_filter("currency")
def format_currency(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0

    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


@app.template_filter("number")
def format_number(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


# App startup
def ensure_schema():
    with db:
        db.create_tables(
            [Setting, Envelope, Subscription, Transaction, Allocation, Goal, Transfer],
            safe=True,
        )

        envelope_columns = {
            row[1] for row in db.execute_sql("PRAGMA table_info(envelopes)").fetchall()
        }

        if "active" not in envelope_columns:
            db.execute_sql(
                "ALTER TABLE envelopes "
                "ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
            )
        if "recurring_monthly_amount" not in envelope_columns:
            db.execute_sql(
                "ALTER TABLE envelopes "
                "ADD COLUMN recurring_monthly_amount REAL NOT NULL DEFAULT 0"
            )

        subscription_columns = {
            row[1] for row in db.execute_sql("PRAGMA table_info(subscriptions)").fetchall()
        }
        if "frequency" not in subscription_columns:
            db.execute_sql(
                "ALTER TABLE subscriptions "
                "ADD COLUMN frequency TEXT NOT NULL DEFAULT 'monthly'"
            )
        if "envelope_id" not in subscription_columns:
            db.execute_sql("ALTER TABLE subscriptions ADD COLUMN envelope_id INTEGER")
        if "note" not in subscription_columns:
            db.execute_sql("ALTER TABLE subscriptions ADD COLUMN note TEXT")
        if "active" not in subscription_columns:
            db.execute_sql(
                "ALTER TABLE subscriptions "
                "ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
            )

        goal_columns = {
            row[1] for row in db.execute_sql("PRAGMA table_info(goals)").fetchall()
        }

        if "contribution_frequency" not in goal_columns:
            db.execute_sql(
                "ALTER TABLE goals "
                "ADD COLUMN contribution_frequency TEXT NOT NULL DEFAULT 'month'"
            )
        else:
            missing_frequency_count = db.execute_sql(
                "SELECT COUNT(*) "
                "FROM goals "
                "WHERE contribution_frequency IS NULL OR TRIM(contribution_frequency) = ''"
            ).fetchone()[0]

            if missing_frequency_count:
                db.execute_sql(
                    "UPDATE goals "
                    "SET contribution_frequency = 'month' "
                    "WHERE contribution_frequency IS NULL "
                    "OR TRIM(contribution_frequency) = ''"
                )

        for key, value in SETTINGS_DEFAULTS.items():
            Setting.get_or_create(key=key, defaults={"value": value})


ensure_schema()


@app.before_request
def _db_connect():
    if db.is_closed():
        db.connect()


@app.before_request
def _process_recurring_deposits():
    if request.endpoint == "static":
        return
    process_monthly_recurring_deposits()


@app.teardown_request
def _db_close(exc):
    if not db.is_closed():
        db.close()


@app.context_processor
def inject_navigation():
    return {"nav_items": NAV_ITEMS}


# Misc. Shared helper functions
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_date():
    return date_cls.today()


def parse_iso_date(value):
    try:
        return date_cls.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_stored_date(value):
    if not value:
        return None

    parsed_date = parse_iso_date(value)
    if parsed_date is not None:
        return parsed_date

    try:
        return datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        return None


def flash_errors(errors):
    for message in errors:
        flash(message, "error")


def round_money(value):
    amount = float(value or 0.0)
    epsilon = 1e-9 if amount >= 0 else -1e-9
    return round(amount + epsilon, 2)


def normalize_csv_header(value):
    return "".join(ch for ch in (value or "").strip().lower() if ch.isalnum())


def choose_csv_column(header_map, *candidates):
    for candidate in candidates:
        column_name = header_map.get(candidate)
        if column_name:
            return column_name
    return None


def normalize_payee(value):
    return " ".join((value or "").split()).strip()


def read_uploaded_csv_text(upload):
    source_filename = Path(upload.filename or "").name
    if not source_filename:
        raise ValueError("Please choose a CSV file to import.")

    file_bytes = upload.read()
    if not file_bytes:
        raise ValueError("The selected CSV file is empty.")

    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return source_filename, file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError(
        "This CSV file could not be read. Please export it as UTF-8 if possible."
    )


def detect_transaction_import_profile(fieldnames):
    header_map = {}
    for fieldname in fieldnames or []:
        header_map.setdefault(normalize_csv_header(fieldname), fieldname)

    date_column = choose_csv_column(
        header_map,
        "date",
        "posteddate",
        "transactiondate",
    )
    description_column = choose_csv_column(
        header_map,
        "description",
        "payee",
        "merchant",
        "details",
        "transaction",
        "name",
        "memo",
    )
    amount_column = choose_csv_column(
        header_map,
        "amount",
        "transactionamount",
    )
    time_column = choose_csv_column(
        header_map,
        "time",
        "postedtime",
        "transactiontime",
    )

    if not (date_column and description_column and amount_column):
        raise ValueError(
            "This CSV format is not supported yet. Expected columns like Date, "
            "Description or Payee, and Amount."
        )

    normalized_headers = set(header_map)
    profile_name = (
        "SoFi Statement CSV"
        if {
            "date",
            "description",
            "type",
            "amount",
            "currentbalance",
            "status",
        }.issubset(normalized_headers)
        else (
            "Ally Activity CSV"
            if {"date", "time", "amount", "type", "description"}.issubset(
                normalized_headers
            )
            else "Generic Statement CSV"
        )
    )

    return {
        "name": profile_name,
        "date_column": date_column,
        "time_column": time_column,
        "description_column": description_column,
        "amount_column": amount_column,
        "type_column": choose_csv_column(header_map, "type", "transactiontype"),
        "status_column": choose_csv_column(header_map, "status"),
        "balance_column": choose_csv_column(
            header_map,
            "currentbalance",
            "balance",
            "runningbalance",
        ),
    }


def parse_import_date(value):
    raw_value = (value or "").strip()
    if not raw_value:
        raise ValueError("Date is blank.")

    for date_format in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw_value, date_format).date().isoformat()
        except ValueError:
            continue

    parsed_date = parse_stored_date(raw_value)
    if parsed_date is not None:
        return parsed_date.isoformat()

    raise ValueError(f"Date '{raw_value}' is not in a supported format.")


def parse_signed_amount(value):
    raw_value = (value or "").strip()
    if not raw_value:
        raise ValueError("Amount is blank.")

    normalized_value = (
        raw_value.replace("$", "")
        .replace(",", "")
        .replace("\u2212", "-")
        .strip()
    )
    if normalized_value.startswith("(") and normalized_value.endswith(")"):
        normalized_value = f"-{normalized_value[1:-1]}"

    try:
        return round_money(float(normalized_value))
    except ValueError as exc:
        raise ValueError(f"Amount '{raw_value}' is invalid.") from exc


def parse_import_time(value):
    raw_value = (value or "").strip()
    if not raw_value:
        return ""

    for time_format in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw_value, time_format).time().isoformat()
        except ValueError:
            continue

    raise ValueError(f"Time '{raw_value}' is not in a supported format.")


def extract_import_time_from_note(note):
    note_text = note or ""
    for part in note_text.split(" | "):
        if part.startswith("Time: "):
            return part.removeprefix("Time: ").strip()
    return ""


def transaction_import_fingerprint(
    date_value,
    payee,
    transaction_type,
    amount,
    time_value="",
):
    normalized_payee = normalize_payee(payee).casefold()
    return "|".join(
        [
            date_value,
            time_value or "",
            transaction_type,
            f"{round_money(amount):.2f}",
            normalized_payee,
        ]
    )


def existing_transaction_fingerprints():
    return {
        transaction_import_fingerprint(
            txn.date,
            txn.payee,
            txn.type,
            float(txn.amount),
            extract_import_time_from_note(txn.note),
        )
        for txn in Transaction.select(
            Transaction.date,
            Transaction.payee,
            Transaction.type,
            Transaction.amount,
            Transaction.note,
        )
    }


def build_transaction_import_note(
    source_filename,
    profile_name,
    raw_type,
    status,
    time_value="",
):
    note_parts = [f"Imported from {source_filename}", profile_name]
    if time_value:
        note_parts.append(f"Time: {time_value}")
    if raw_type:
        note_parts.append(f"Bank type: {raw_type}")
    if status:
        note_parts.append(f"Status: {status}")
    return " | ".join(note_parts)


def build_transaction_import_row(row_number, raw_row, profile, source_filename):
    date_value = parse_import_date(raw_row.get(profile["date_column"]))
    time_value = parse_import_time(
        raw_row.get(profile["time_column"]) if profile["time_column"] else ""
    )
    payee = normalize_payee(raw_row.get(profile["description_column"]))
    if not payee:
        raise ValueError("Description or payee is blank.")

    signed_amount = parse_signed_amount(raw_row.get(profile["amount_column"]))
    if round_money(signed_amount) == 0:
        raise ValueError("Zero-amount rows cannot be imported.")

    raw_type = normalize_payee(
        raw_row.get(profile["type_column"]) if profile["type_column"] else ""
    )
    status = normalize_payee(
        raw_row.get(profile["status_column"]) if profile["status_column"] else ""
    )
    running_balance = normalize_payee(
        raw_row.get(profile["balance_column"]) if profile["balance_column"] else ""
    )

    skip_reason = ""
    if status and status.casefold() not in IMPORT_READY_STATUSES:
        skip_reason = f"Status '{status}' is not imported yet."

    transaction_type = "INCOME" if signed_amount > 0 else "EXPENSE"
    amount = abs(round_money(signed_amount))

    return {
        "row_number": row_number,
        "date": date_value,
        "payee": payee,
        "raw_type": raw_type,
        "status": status,
        "running_balance": running_balance,
        "transaction_type": transaction_type,
        "amount": amount,
        "note": build_transaction_import_note(
            source_filename,
            profile["name"],
            raw_type,
            status,
            time_value,
        ),
        "skip_reason": skip_reason,
        "is_existing_duplicate": False,
        "is_file_duplicate": False,
        "can_import": False,
        "fingerprint": transaction_import_fingerprint(
            date_value,
            payee,
            transaction_type,
            amount,
            time_value,
        ),
        "time": time_value,
    }


def parse_transaction_import_csv(upload):
    source_filename, csv_text = read_uploaded_csv_text(upload)
    sample = csv_text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(StringIO(csv_text), dialect=dialect)
    profile = detect_transaction_import_profile(reader.fieldnames)
    known_fingerprints = existing_transaction_fingerprints()
    file_fingerprints = set()
    rows = []

    for row_number, raw_row in enumerate(reader, start=2):
        if not raw_row or not any((value or "").strip() for value in raw_row.values()):
            continue

        try:
            row = build_transaction_import_row(
                row_number,
                raw_row,
                profile,
                source_filename,
            )
        except ValueError as exc:
            rows.append(
                {
                    "row_number": row_number,
                    "date": normalize_payee(raw_row.get(profile["date_column"])),
                    "payee": normalize_payee(
                        raw_row.get(profile["description_column"])
                    ),
                    "raw_type": normalize_payee(
                        raw_row.get(profile["type_column"])
                        if profile["type_column"]
                        else ""
                    ),
                    "status": normalize_payee(
                        raw_row.get(profile["status_column"])
                        if profile["status_column"]
                        else ""
                    ),
                    "running_balance": normalize_payee(
                        raw_row.get(profile["balance_column"])
                        if profile["balance_column"]
                        else ""
                    ),
                    "transaction_type": "",
                    "amount": None,
                    "amount_display": normalize_payee(
                        raw_row.get(profile["amount_column"])
                    ),
                    "note": "",
                    "skip_reason": str(exc),
                    "is_existing_duplicate": False,
                    "is_file_duplicate": False,
                    "can_import": False,
                    "fingerprint": "",
                    "time": normalize_payee(
                        raw_row.get(profile["time_column"])
                        if profile["time_column"]
                        else ""
                    ),
                }
            )
            continue

        row["is_existing_duplicate"] = row["fingerprint"] in known_fingerprints
        row["is_file_duplicate"] = row["fingerprint"] in file_fingerprints
        row["can_import"] = (
            not row["skip_reason"]
            and not row["is_existing_duplicate"]
            and not row["is_file_duplicate"]
        )

        if not row["skip_reason"] and row["fingerprint"] not in file_fingerprints:
            file_fingerprints.add(row["fingerprint"])

        rows.append(row)

    if not rows:
        raise ValueError("No transaction rows were found in that CSV file.")

    ready_rows = [row for row in rows if row["can_import"]]
    duplicate_count = sum(
        1
        for row in rows
        if row["is_existing_duplicate"] or row["is_file_duplicate"]
    )
    skipped_count = sum(1 for row in rows if row["skip_reason"])

    return {
        "source_filename": source_filename,
        "profile_name": profile["name"],
        "created_at": now_iso(),
        "rows": rows,
        "summary": {
            "total_rows": len(rows),
            "ready_count": len(ready_rows),
            "duplicate_count": duplicate_count,
            "skipped_count": skipped_count,
            "income_count": sum(
                1 for row in ready_rows if row["transaction_type"] == "INCOME"
            ),
            "expense_count": sum(
                1 for row in ready_rows if row["transaction_type"] == "EXPENSE"
            ),
            "income_total": round_money(
                sum(
                    row["amount"]
                    for row in ready_rows
                    if row["transaction_type"] == "INCOME"
                )
            ),
            "expense_total": round_money(
                sum(
                    row["amount"]
                    for row in ready_rows
                    if row["transaction_type"] == "EXPENSE"
                )
            ),
        },
        "has_time_values": any(row.get("time") for row in rows),
    }


def ensure_import_preview_dir():
    IMPORT_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def import_preview_path(token):
    return IMPORT_PREVIEW_DIR / f"{token}.json"


def save_transaction_import_preview(payload):
    ensure_import_preview_dir()
    token = uuid4().hex
    import_preview_path(token).write_text(json.dumps(payload), encoding="utf-8")
    return token


def load_transaction_import_preview(token):
    normalized_token = (token or "").strip().lower()
    if len(normalized_token) != 32 or any(
        ch not in "0123456789abcdef" for ch in normalized_token
    ):
        return None

    preview_file = import_preview_path(normalized_token)
    if not preview_file.exists():
        return None

    try:
        payload = json.loads(preview_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    payload["token"] = normalized_token
    return payload


def delete_transaction_import_preview(token):
    preview_file = import_preview_path((token or "").strip().lower())
    if preview_file.exists():
        preview_file.unlink()


def month_bounds(reference_date=None):
    reference_date = reference_date or today_date()
    start_date = date_cls(reference_date.year, reference_date.month, 1)
    if reference_date.month == 12:
        next_month = date_cls(reference_date.year + 1, 1, 1)
    else:
        next_month = date_cls(reference_date.year, reference_date.month + 1, 1)
    end_date = next_month - timedelta(days=1)
    return start_date, end_date


def month_key(reference_date=None):
    reference_date = reference_date or today_date()
    return reference_date.strftime("%Y-%m")


def get_setting_value(key, default=None):
    setting = Setting.get_or_none(Setting.key == key)
    if setting is None:
        return default
    return setting.value


def get_setting_bool(key, default=False):
    fallback = "1" if default else "0"
    return get_setting_value(key, fallback) == "1"


def set_setting_value(key, value):
    Setting.insert(key=key, value=str(value)).on_conflict(
        conflict_target=[Setting.key],
        update={Setting.value: str(value)},
    ).execute()


def ordered_envelopes(include_archived=False):
    query = Envelope.select()
    if not include_archived:
        query = query.where(Envelope.active == 1)
    return query.order_by(Envelope.name)


def ordered_archived_envelopes():
    return Envelope.select().where(Envelope.active == 0).order_by(Envelope.name)


def get_or_create_subscriptions_envelope():
    envelope, created = Envelope.get_or_create(
        name=SUBSCRIPTIONS_ENVELOPE_NAME,
        defaults={
            "active": 1,
            "recurring_monthly_amount": 0,
            "created_at": now_iso(),
        },
    )
    if not created and not envelope.active:
        envelope.active = 1
        envelope.save()
    return envelope


def ordered_active_goals():
    return Goal.select().where(Goal.active == 1).order_by(Goal.name)


def envelope_current_balance(envelope):
    incoming_total = sum(
        float(transfer.amount)
        for transfer in Transfer.select(Transfer.amount).where(
            Transfer.destination_envelope == envelope
        )
    )
    outgoing_total = sum(
        float(transfer.amount)
        for transfer in Transfer.select(Transfer.amount).where(
            Transfer.source_envelope == envelope
        )
    )
    spent_total = sum(
        float(transaction.amount)
        for transaction in Transaction.select(Transaction.amount).where(
            (Transaction.type == "EXPENSE") & (Transaction.envelope == envelope)
        )
    )
    return round(incoming_total - outgoing_total - spent_total, 2)


def can_archive_envelope(envelope):
    goal_count = Goal.select().where(Goal.envelope == envelope).count()
    if goal_count > 0:
        return False, "This envelope is still attached to one or more goals."

    balance = envelope_current_balance(envelope)
    if abs(balance) >= 0.01:
        return (
            False,
            "This envelope still has a current balance of "
            f"{format_currency(balance)}. Move or spend that balance before archiving.",
        )

    return True, ""


def has_valid_iso_date(value):
    return parse_iso_date(value) is not None


def parse_positive_amount(value, invalid_message, errors):
    try:
        amount = float(value)
    except ValueError:
        errors.append(invalid_message)
        return None

    if amount <= 0:
        errors.append("Amount must be greater than 0.")

    return amount


def parse_non_negative_amount(value, invalid_message, errors):
    try:
        amount = float(value)
    except ValueError:
        errors.append(invalid_message)
        return None

    if amount < 0:
        errors.append("Amount saved cannot be negative.")

    return amount


def parse_optional_non_negative_amount(value, invalid_message, errors):
    if value is None or str(value).strip() == "":
        return 0.0

    try:
        amount = float(value)
    except ValueError:
        errors.append(invalid_message)
        return None

    if amount < 0:
        errors.append("Recurring monthly deposit cannot be negative.")

    return round_money(amount)


def get_envelope_from_form(envelope_id):
    try:
        envelope_id = int(envelope_id)
    except (TypeError, ValueError):
        return None

    return Envelope.get_or_none(Envelope.id == envelope_id)


def get_goal_from_form(goal_id):
    try:
        goal_id = int(goal_id)
    except (TypeError, ValueError):
        return None

    return Goal.get_or_none(Goal.id == goal_id)


def format_amount_input(value):
    return f"{float(value):.2f}"


def format_currency_compact(value):
    amount = float(value)
    sign = "-" if amount < 0 else ""
    amount_text = f"{abs(amount):,.2f}"
    if amount_text.endswith(".00"):
        amount_text = amount_text[:-3]
    return f"{sign}${amount_text}"


# Form helper functions
def build_transaction_form(transaction=None):
    if transaction is None:
        return {
            "date": "",
            "type": "EXPENSE",
            "amount": "",
            "payee": "",
            "envelope_id": "",
            "note": "",
        }

    return {
        "date": transaction.date,
        "type": transaction.type,
        "amount": str(transaction.amount),
        "payee": transaction.payee,
        "envelope_id": str(transaction.envelope.id) if transaction.envelope else "",
        "note": transaction.note or "",
    }


def build_envelope_form(envelope=None):
    if envelope is None:
        return {"name": "", "recurring_monthly_amount": ""}

    recurring_amount = round_money(envelope.recurring_monthly_amount or 0.0)
    return {
        "name": envelope.name,
        "recurring_monthly_amount": (
            format_amount_input(recurring_amount) if recurring_amount > 0 else ""
        ),
    }


def read_transaction_form(default_type="EXPENSE"):
    return {
        "date": (request.form.get("date") or "").strip(),
        "type": (request.form.get("type") or default_type).strip().upper(),
        "amount": (request.form.get("amount") or "").strip(),
        "payee": (request.form.get("payee") or "").strip(),
        "envelope_id": (request.form.get("envelope_id") or "").strip(),
        "note": (request.form.get("note") or "").strip(),
    }


def validate_transaction_form(form):
    errors = []

    if not has_valid_iso_date(form["date"]):
        errors.append("Please enter a valid date.")

    if form["type"] not in TRANSACTION_TYPES:
        errors.append("Type must be Expense or Income.")

    amount = parse_positive_amount(
        form["amount"],
        "Amount must be a valid number (ex: 10.99).",
        errors,
    )

    if not form["payee"]:
        errors.append("Payee is required.")

    envelope = None
    if form["envelope_id"]:
        envelope = get_envelope_from_form(form["envelope_id"])
        if envelope is None:
            errors.append("Selected envelope does not exist.")

    if form["type"] == "EXPENSE" and envelope is None:
        errors.append("Envelope is required for an Expense.")

    return errors, amount, envelope


def read_envelope_form():
    return {
        "name": (request.form.get("name") or "").strip(),
        "recurring_monthly_amount": (
            request.form.get("recurring_monthly_amount") or ""
        ).strip(),
    }


def build_goal_form(goal=None, amount_saved=None):
    if goal is None:
        return {
            "name": "",
            "target_amount": "",
            "target_date": "",
            "contribution_frequency": "month",
            "amount_saved": "",
        }

    if amount_saved is None:
        amount_saved = 0.0

    return {
        "name": goal.name,
        "target_amount": format_amount_input(goal.target_amount),
        "target_date": goal.target_date or "",
        "contribution_frequency": goal.contribution_frequency or "month",
        "amount_saved": format_amount_input(amount_saved),
    }


def read_goal_form():
    return {
        "name": (request.form.get("name") or "").strip(),
        "target_amount": (request.form.get("target_amount") or "").strip(),
        "target_date": (request.form.get("target_date") or "").strip(),
        "contribution_frequency": (
            request.form.get("contribution_frequency") or "month"
        ).strip(),
        "amount_saved": (request.form.get("amount_saved") or "").strip(),
    }


def find_duplicate_envelope(name, current_envelope_id=None):
    query = Envelope.select().where(Envelope.name == name)

    if current_envelope_id is not None:
        query = query.where(Envelope.id != current_envelope_id)

    return query.get_or_none()


def find_duplicate_goal(name, current_goal_id=None):
    query = Goal.select().where(Goal.name == name)

    if current_goal_id is not None:
        query = query.where(Goal.id != current_goal_id)

    return query.get_or_none()


def validate_envelope_form(form, current_envelope_id=None):
    errors = []

    if not form["name"]:
        errors.append("Envelope name is required.")
    elif find_duplicate_envelope(form["name"], current_envelope_id) is not None:
        if current_envelope_id is None:
            errors.append("An envelope with that name already exists.")
        else:
            errors.append("Another envelope with that name already exists.")

    recurring_monthly_amount = parse_optional_non_negative_amount(
        form["recurring_monthly_amount"],
        "Recurring monthly deposit must be a valid number (ex: 50.00).",
        errors,
    )

    return errors, recurring_monthly_amount


def build_settings_form():
    return {
        "auto_recurring_deposits": get_setting_bool("auto_recurring_deposits"),
        "auto_retry_recurring_deposits": get_setting_bool(
            "auto_retry_recurring_deposits"
        ),
        "prevent_negative_envelopes": get_setting_bool("prevent_negative_envelopes"),
        "dashboard_double_click_fund_negative": get_setting_bool(
            "dashboard_double_click_fund_negative",
            default=True,
        ),
    }


def build_subscription_form(subscription=None):
    if subscription is None:
        return {
            "name": "",
            "amount": "",
            "renewal_date": today_date().isoformat(),
            "frequency": "monthly",
            "note": "",
        }

    amount = round_money(subscription.amount)
    return {
        "name": subscription.name,
        "amount": format_amount_input(amount),
        "renewal_date": subscription.renewal_date,
        "frequency": subscription.frequency,
        "note": subscription.note or "",
    }


def read_subscription_form():
    return {
        "name": (request.form.get("name") or "").strip(),
        "amount": (request.form.get("amount") or "").strip(),
        "renewal_date": (request.form.get("renewal_date") or "").strip(),
        "frequency": (request.form.get("frequency") or "monthly").strip(),
        "note": (request.form.get("note") or "").strip(),
    }


def validate_subscription_form(form):
    errors = []

    if not form["name"]:
        errors.append("Subscription name is required.")

    amount = parse_positive_amount(
        form["amount"],
        "Subscription amount must be a valid number (ex: 12.99).",
        errors,
    )

    renewal_date = parse_iso_date(form["renewal_date"])
    if renewal_date is None:
        errors.append("Please enter a valid renewal date.")

    if form["frequency"] not in SUBSCRIPTION_FREQUENCIES:
        errors.append("Please choose a valid renewal frequency.")

    return errors, amount, renewal_date


def validate_goal_form(form, current_goal_id=None):
    errors = []

    if not form["name"]:
        errors.append("Goal name is required.")
    elif find_duplicate_goal(form["name"], current_goal_id) is not None:
        errors.append("A goal with that name already exists.")

    target_amount = parse_positive_amount(
        form["target_amount"],
        "Target amount must be a valid number (ex: 500.00).",
        errors,
    )

    target_date = parse_iso_date(form["target_date"])
    if target_date is None:
        errors.append("Please enter a valid target date.")

    if form["contribution_frequency"] not in GOAL_FREQUENCIES:
        errors.append("Please choose a valid contribution schedule.")

    return errors, target_amount, target_date


# Goal timing and progress helper functions
def goal_frequency_label(frequency):
    return GOAL_FREQUENCIES.get(frequency, GOAL_FREQUENCIES["month"])


def goal_period_count(target_date, contribution_frequency, reference_date=None):
    if target_date is None:
        return 1

    reference_date = reference_date or today_date()
    days_left = max((target_date - reference_date).days, 0)
    span_days = max(days_left, 1)
    step_days = GOAL_COUNT_DAYS.get(contribution_frequency)

    if step_days is not None:
        return max(math.ceil(span_days / step_days), 1)

    return max(math.ceil(span_days / 30), 1)


def goal_timeline_text(days_left, remaining_amount):
    if remaining_amount <= 0:
        return "Target reached"
    if days_left is None:
        return "No due date"
    if days_left < 0:
        return f"{abs(days_left)} day(s) overdue"
    if days_left == 0:
        return "Due today"
    return f"{days_left} day(s) left"


def add_months(value, months):
    month_index = value.year * 12 + value.month - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date_cls(year, month, day)


def add_years(value, years):
    year = value.year + years
    day = min(value.day, monthrange(year, value.month)[1])
    return date_cls(year, value.month, day)


def subscription_frequency_label(frequency):
    return SUBSCRIPTION_FREQUENCIES.get(
        frequency,
        SUBSCRIPTION_FREQUENCIES["monthly"],
    )


def subscription_frequency_months(frequency):
    if frequency == "quarterly":
        return 3
    if frequency == "yearly":
        return 12
    return 1


def parse_calendar_month(value, reference_date=None):
    reference_date = reference_date or today_date()
    try:
        parsed = datetime.strptime(value or "", "%Y-%m").date()
        return date_cls(parsed.year, parsed.month, 1)
    except (TypeError, ValueError):
        return date_cls(reference_date.year, reference_date.month, 1)


def parse_calendar_day(value, month_start, reference_date=None):
    reference_date = reference_date or today_date()
    selected_day = parse_iso_date(value)
    if selected_day is None:
        if (
            reference_date.year == month_start.year
            and reference_date.month == month_start.month
        ):
            return reference_date
        return month_start
    if selected_day.year != month_start.year or selected_day.month != month_start.month:
        return month_start
    return selected_day


def subscription_renewal_for_month(subscription, month_start):
    base_date = parse_iso_date(subscription.renewal_date)
    if base_date is None:
        return None

    month_end = add_months(month_start, 1) - timedelta(days=1)
    if base_date > month_end:
        return None

    interval_months = subscription_frequency_months(subscription.frequency)
    occurrence_index = 0
    occurrence_date = base_date

    while occurrence_date < month_start:
        occurrence_index += 1
        occurrence_date = add_months(base_date, interval_months * occurrence_index)

    if occurrence_date <= month_end:
        return occurrence_date
    return None


def build_subscription_rows(subscriptions, month_start):
    rows = []
    for subscription in subscriptions:
        next_renewal = subscription_renewal_for_month(subscription, month_start)
        rows.append(
            {
                "id": subscription.id,
                "name": subscription.name,
                "amount": float(subscription.amount),
                "renewal_date": subscription.renewal_date,
                "next_renewal": next_renewal,
                "frequency": subscription.frequency,
                "frequency_label": subscription_frequency_label(subscription.frequency),
                "envelope_name": subscription.envelope.name if subscription.envelope else "",
                "note": subscription.note or "",
            }
        )

    rows.sort(
        key=lambda row: (
            row["next_renewal"] is None,
            row["next_renewal"] or date_cls.max,
            row["name"].lower(),
        )
    )
    return rows


def build_subscription_calendar(
    subscription_rows,
    month_start,
    selected_day=None,
    reference_date=None,
):
    reference_date = reference_date or today_date()
    selected_day = selected_day or parse_calendar_day(None, month_start, reference_date)
    month_end = add_months(month_start, 1) - timedelta(days=1)
    calendar_start = month_start - timedelta(days=(month_start.weekday() + 1) % 7)
    calendar_end = month_end + timedelta(days=(5 - month_end.weekday()) % 7)
    renewals_by_date = defaultdict(list)

    for row in subscription_rows:
        renewal_date = row["next_renewal"]
        if renewal_date is not None:
            renewals_by_date[renewal_date].append(row)

    weeks = []
    cursor = calendar_start
    while cursor <= calendar_end:
        week = []
        for _ in range(7):
            week.append(
                {
                    "date": cursor,
                    "day": cursor.day,
                    "is_current_month": cursor.month == month_start.month,
                    "is_today": cursor == reference_date,
                    "is_selected": cursor == selected_day,
                    "renewals": renewals_by_date.get(cursor, []),
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)

    return {
        "weeks": weeks,
        "month_label": month_start.strftime("%B %Y"),
        "month_value": month_start.strftime("%Y-%m"),
        "year": month_start.year,
        "month": month_start.month,
        "previous_month": add_months(month_start, -1).strftime("%Y-%m"),
        "next_month": add_months(month_start, 1).strftime("%Y-%m"),
        "selected_day": selected_day.isoformat(),
        "year_options": list(range(month_start.year - 3, month_start.year + 4)),
        "month_options": [
            {"value": month, "label": date_cls(2000, month, 1).strftime("%b")}
            for month in range(1, 13)
        ],
        "day_options": [month_start + timedelta(days=offset) for offset in range(month_end.day)],
    }


def offset_period_start(base_date, contribution_frequency, index):
    if contribution_frequency == "day":
        return base_date + timedelta(days=index)
    if contribution_frequency == "week":
        return base_date + timedelta(days=index * 7)
    if contribution_frequency == "biweekly":
        return base_date + timedelta(days=index * 14)
    if contribution_frequency == "year":
        return add_years(base_date, index)
    return add_months(base_date, index)


def goal_period_window(base_date, contribution_frequency, reference_date, target_date=None):
    if reference_date < base_date:
        reference_date = base_date

    period_index = 0
    current_start = base_date
    next_start = offset_period_start(base_date, contribution_frequency, 1)

    while next_start <= reference_date:
        period_index += 1
        current_start = offset_period_start(base_date, contribution_frequency, period_index)
        next_start = offset_period_start(
            base_date,
            contribution_frequency,
            period_index + 1,
        )

    current_end = next_start - timedelta(days=1)
    if target_date is not None and current_end > target_date:
        current_end = target_date

    return current_start, current_end


def format_period_label(start_date, end_date):
    start_text = start_date.strftime("%m/%d")
    end_text = end_date.strftime("%m/%d")
    if start_text == end_text:
        return start_text
    return f"{start_text} - {end_text}"


def goal_transfer_total(goal_id, start_date=None, end_date=None):
    filters = (Transfer.source_goal_id == goal_id) | (Transfer.destination_goal_id == goal_id)

    if start_date is not None and end_date is not None:
        filters &= (Transfer.date >= start_date.isoformat()) & (
            Transfer.date <= end_date.isoformat()
        )

    amount_total = 0.0
    transfer_query = Transfer.select().where(filters)

    for transfer in transfer_query:
        amount = float(transfer.amount)
        if transfer.destination_type == "goal" and transfer.destination_goal_id == goal_id:
            amount_total += amount
        elif transfer.source_type == "goal" and transfer.source_goal_id == goal_id:
            amount_total -= amount

    return amount_total


def goal_period_net_amount(goal_id, period_start, period_end):
    return goal_transfer_total(goal_id, period_start, period_end)


def goal_saved_amount(goal_id):
    return goal_transfer_total(goal_id)


# Transfer and budget helper functions
def location_label(loc_type, envelope_obj=None, goal_obj=None):
    if loc_type == "available":
        return "Available Balance"
    if loc_type == RECURRING_DUE_DESTINATION_TYPE:
        return "Recurring Deposit Due"
    if loc_type == "envelope" and envelope_obj:
        return f"Envelope: {envelope_obj.name}"
    if loc_type == "goal" and goal_obj:
        return f"Goal: {goal_obj.name}"
    return "Unknown"


def build_transfer_state(limit_history=25):
    envelope_transfer_map = defaultdict(float)
    goal_transfer_map = defaultdict(float)
    available_delta = 0.0
    transfer_history_rows = []

    for transfer in Transfer.select():
        amount = float(transfer.amount)

        if transfer.source_type == "available":
            available_delta -= amount
        elif transfer.source_type == "envelope" and transfer.source_envelope_id:
            envelope_transfer_map[transfer.source_envelope_id] -= amount
        elif transfer.source_type == "goal" and transfer.source_goal_id:
            goal_transfer_map[transfer.source_goal_id] -= amount

        if transfer.destination_type == "available":
            available_delta += amount
        elif (
            transfer.destination_type == "envelope"
            and transfer.destination_envelope_id
        ):
            envelope_transfer_map[transfer.destination_envelope_id] += amount
        elif transfer.destination_type == "goal" and transfer.destination_goal_id:
            goal_transfer_map[transfer.destination_goal_id] += amount

    transfer_query = Transfer.select().order_by(Transfer.date.desc(), Transfer.id.desc())
    if limit_history is not None:
        transfer_query = transfer_query.limit(limit_history)

    for transfer in transfer_query:
        transfer_history_rows.append(
            {
                "date": transfer.date,
                "from_label": location_label(
                    transfer.source_type,
                    transfer.source_envelope,
                    transfer.source_goal,
                ),
                "to_label": location_label(
                    transfer.destination_type,
                    transfer.destination_envelope,
                    transfer.destination_goal,
                ),
                "amount": float(transfer.amount),
            }
        )

    return {
        "available_delta": available_delta,
        "envelope_transfer_map": envelope_transfer_map,
        "goal_transfer_map": goal_transfer_map,
        "transfer_history_rows": transfer_history_rows,
    }


def build_recurring_deposit_state(envelopes, reference_date=None):
    reference_date = reference_date or today_date()
    month_start, month_end = month_bounds(reference_date)
    funded_by_envelope = defaultdict(float)
    obligation_by_envelope = defaultdict(float)

    funding_transfer_query = Transfer.select().where(
        (Transfer.destination_type == "envelope")
        & (Transfer.destination_envelope.is_null(False))
        & (Transfer.date >= month_start.isoformat())
        & (Transfer.date <= month_end.isoformat())
    )
    for transfer in funding_transfer_query:
        funded_by_envelope[transfer.destination_envelope_id] += float(transfer.amount)

    obligation_transfer_query = Transfer.select().where(
        (Transfer.source_type == "envelope")
        & (Transfer.source_envelope.is_null(False))
        & (Transfer.destination_type == RECURRING_DUE_DESTINATION_TYPE)
        & (Transfer.date >= month_start.isoformat())
        & (Transfer.date <= month_end.isoformat())
    )
    for transfer in obligation_transfer_query:
        obligation_by_envelope[transfer.source_envelope_id] += float(transfer.amount)

    rows = []
    monthly_target_total = 0.0
    funded_total = 0.0
    due_total = 0.0
    auto_mode_enabled = get_setting_bool("auto_recurring_deposits")
    auto_retry_enabled = get_setting_bool("auto_retry_recurring_deposits")

    for envelope in envelopes:
        configured_amount = round_money(getattr(envelope, "recurring_monthly_amount", 0.0) or 0.0)
        obligation_amount = round_money(obligation_by_envelope[envelope.id])
        monthly_amount = obligation_amount if obligation_amount > 0 else configured_amount
        if monthly_amount <= 0:
            continue

        funded_this_month = round_money(funded_by_envelope[envelope.id])
        remaining_due = round_money(max(monthly_amount - funded_this_month, 0.0))
        is_funded = remaining_due <= 0.0

        rows.append(
            {
                "id": envelope.id,
                "name": envelope.name,
                "monthly_amount": monthly_amount,
                "configured_monthly_amount": configured_amount,
                "obligation_amount": obligation_amount,
                "funded_this_month": funded_this_month,
                "remaining_due": remaining_due,
                "is_funded": is_funded,
                "is_auto_scheduled": obligation_amount > 0,
            }
        )

        monthly_target_total += monthly_amount
        funded_total += funded_this_month
        due_total += remaining_due

    rows.sort(key=lambda row: (-row["remaining_due"], row["name"].lower()))

    return {
        "rows": rows,
        "month_label": reference_date.strftime("%B %Y"),
        "monthly_target_total": round_money(monthly_target_total),
        "funded_total": round_money(funded_total),
        "due_total": round_money(due_total),
        "due_count": sum(1 for row in rows if row["remaining_due"] > 0),
        "auto_mode_enabled": auto_mode_enabled,
        "auto_retry_enabled": auto_retry_enabled,
    }


def create_recurring_due_transfer(envelope, amount, transfer_date=None):
    transfer_date = transfer_date or today_date().isoformat()
    return Transfer.create(
        date=transfer_date,
        source_type="envelope",
        source_envelope=envelope,
        source_goal=None,
        destination_type=RECURRING_DUE_DESTINATION_TYPE,
        destination_envelope=None,
        destination_goal=None,
        amount=round_money(amount),
        created_at=now_iso(),
    )


def apply_available_balance_to_recurring_due(reference_date=None):
    reference_date = reference_date or today_date()
    state = build_budget_state()
    available_remaining = round_money(state["available_balance"])
    if available_remaining <= 0:
        return 0.0, 0

    rows = [
        row
        for row in state["recurring_deposit_state"]["rows"]
        if row["remaining_due"] > 0
    ]
    rows.sort(key=lambda row: row["name"].lower())
    funded_total = 0.0
    funded_count = 0

    for row in rows:
        if available_remaining <= 0:
            break

        funding_amount = round_money(min(row["remaining_due"], available_remaining))
        if funding_amount <= 0:
            continue

        envelope = Envelope.get_or_none(Envelope.id == row["id"])
        if envelope is None:
            continue

        create_available_to_envelope_transfer(
            envelope,
            funding_amount,
            transfer_date=reference_date.isoformat(),
        )
        funded_total = round_money(funded_total + funding_amount)
        available_remaining = round_money(available_remaining - funding_amount)
        funded_count += 1

    return funded_total, funded_count


def process_monthly_recurring_deposits(reference_date=None):
    reference_date = reference_date or today_date()
    if not get_setting_bool("auto_recurring_deposits"):
        return

    if strict_budgeting_enabled():
        apply_available_balance_to_recurring_due(reference_date)
        return

    month_start, month_end = month_bounds(reference_date)
    recurring_envelopes = list(
        Envelope.select().where(
            (Envelope.active == 1) & (Envelope.recurring_monthly_amount > 0)
        )
    )
    if not recurring_envelopes:
        return

    existing_keys = {
        row[0]
        for row in Transfer.select(Transfer.source_envelope_id).where(
            (Transfer.source_type == "envelope")
            & (Transfer.destination_type == RECURRING_DUE_DESTINATION_TYPE)
            & (Transfer.source_envelope.is_null(False))
            & (Transfer.date >= month_start.isoformat())
            & (Transfer.date <= month_end.isoformat())
        ).tuples()
    }

    created_due_rows = False
    with db.atomic():
        for envelope in recurring_envelopes:
            if envelope.id in existing_keys:
                continue
            create_recurring_due_transfer(
                envelope,
                envelope.recurring_monthly_amount,
                transfer_date=reference_date.isoformat(),
            )
            created_due_rows = True

    should_retry = get_setting_bool("auto_retry_recurring_deposits")
    if created_due_rows or should_retry:
        apply_available_balance_to_recurring_due(reference_date)


def build_goal_rows(goals, goal_transfer_map, reference_date=None):
    reference_date = reference_date or today_date()
    goal_rows = []

    for goal in goals:
        target_amount = float(goal.target_amount)
        funded_amount = round_money(goal_transfer_map.get(goal.id, 0.0))
        raw_remaining = target_amount - funded_amount
        remaining_amount = round_money(max(raw_remaining, 0.0))
        target_date = parse_iso_date(goal.target_date)
        created_date = parse_stored_date(goal.created_at) or reference_date
        contribution_frequency = goal.contribution_frequency or "month"
        periods_left = goal_period_count(
            target_date,
            contribution_frequency,
            reference_date,
        )
        contribution_amount = round_money(
            remaining_amount / periods_left if remaining_amount > 0 else 0.0
        )
        days_left = None
        if target_date is not None:
            days_left = (target_date - reference_date).days

        progress_percent = 0.0
        if target_amount > 0:
            progress_percent = min(max((funded_amount / target_amount) * 100, 0.0), 100.0)

        total_periods = goal_period_count(
            target_date,
            contribution_frequency,
            created_date,
        )
        baseline_contribution_amount = round_money(
            target_amount / total_periods if total_periods > 0 else target_amount
        )
        periods_elapsed = max(total_periods - periods_left, 0)
        expected_funded = min(
            baseline_contribution_amount * periods_elapsed,
            target_amount,
        )
        shortfall_amount = round_money(max(expected_funded - funded_amount, 0.0))
        behind_schedule = (
            remaining_amount > 0
            and shortfall_amount > 0.01
            and contribution_amount > baseline_contribution_amount + 0.01
        )

        progress_tooltip = ""
        if behind_schedule:
            progress_tooltip = (
                "Behind schedule. "
                f"New amount: {format_currency_compact(contribution_amount)} "
                f"per {contribution_frequency.lower()}. "
                f"Short by {format_currency_compact(shortfall_amount)}."
            )

        period_start, period_end = goal_period_window(
            created_date,
            contribution_frequency,
            reference_date,
            target_date,
        )
        period_contributed_amount = round_money(
            goal_period_net_amount(
                goal.id,
                period_start,
                period_end,
            )
        )
        current_period_remaining_amount = round_money(
            max(
                contribution_amount - period_contributed_amount,
                0.0,
            )
        )

        goal_rows.append(
            {
                "id": goal.id,
                "name": goal.name,
                "target_amount": target_amount,
                "funded": funded_amount,
                "remaining": raw_remaining,
                "remaining_amount": remaining_amount,
                "raw_remaining": raw_remaining,
                "target_date": goal.target_date,
                "days_left": days_left,
                "contribution_frequency": contribution_frequency,
                "contribution_frequency_label": goal_frequency_label(
                    contribution_frequency
                ),
                "periods_left": periods_left,
                "contribution_amount": contribution_amount,
                "baseline_contribution_amount": baseline_contribution_amount,
                "progress_percent": progress_percent,
                "behind_schedule": behind_schedule,
                "progress_tooltip": progress_tooltip,
                "shortfall_amount": shortfall_amount,
                "current_period_label": format_period_label(period_start, period_end),
                "current_period_remaining_amount": current_period_remaining_amount,
                "timeline_text": goal_timeline_text(days_left, remaining_amount),
                "active": bool(goal.active),
            }
        )

    return goal_rows


def build_goal_progress_chart(goal_rows):
    return {
        "labels": [row["name"] for row in goal_rows],
        "funded": [round(row["funded"], 2) for row in goal_rows],
        "funded_hover": [format_currency_compact(row["funded"]) for row in goal_rows],
        "remaining": [round(row["remaining_amount"], 2) for row in goal_rows],
        "remaining_hover": [
            format_currency_compact(row["remaining_amount"]) for row in goal_rows
        ],
        "progress": [round(row["progress_percent"], 1) for row in goal_rows],
    }


def build_budget_state():
    envelopes = list(ordered_envelopes())
    goals = list(ordered_active_goals())
    recurring_deposit_state = build_recurring_deposit_state(envelopes)

    income_total = round_money(
        sum(
            float(txn.amount)
            for txn in Transaction.select().where(Transaction.type == "INCOME")
        )
    )

    expense_map = defaultdict(float)
    expense_query = Transaction.select().where(
        (Transaction.type == "EXPENSE") & (Transaction.envelope.is_null(False))
    )
    for txn in expense_query:
        expense_map[txn.envelope_id] = round_money(
            expense_map[txn.envelope_id] + float(txn.amount)
        )

    transfer_state = build_transfer_state()
    available_balance = round_money(income_total + transfer_state["available_delta"])

    envelope_rows = []
    envelope_balance_map = {}
    for envelope in envelopes:
        funded_amount = round_money(transfer_state["envelope_transfer_map"][envelope.id])
        spent_amount = round_money(expense_map[envelope.id])
        available_amount = round_money(funded_amount - spent_amount)
        funding_needed_amount = round_money(max(0.0, -available_amount))

        envelope_balance_map[envelope.id] = available_amount
        envelope_rows.append(
            {
                "id": envelope.id,
                "name": envelope.name,
                "funded": funded_amount,
                "spent": spent_amount,
                "available": available_amount,
                "fund_to_zero_amount": funding_needed_amount,
                "can_fund_to_zero": (
                    funding_needed_amount > 0
                    and funding_needed_amount <= round_money(available_balance)
                ),
            }
        )

    negative_envelope_rows = [row for row in envelope_rows if row["available"] < 0]
    envelope_summary = {
        "count": len(envelope_rows),
        "negative_count": len(negative_envelope_rows),
        "negative_available_total": round_money(
            sum(row["available"] for row in negative_envelope_rows)
        ),
        "negative_funding_needed_total": round_money(
            sum(row["fund_to_zero_amount"] for row in negative_envelope_rows)
        ),
        "net_available_total": round_money(
            sum(row["available"] for row in envelope_rows)
        ),
    }

    goal_rows = build_goal_rows(goals, transfer_state["goal_transfer_map"])
    goal_balance_map = {
        goal.id: round_money(transfer_state["goal_transfer_map"].get(goal.id, 0.0))
        for goal in goals
    }

    return {
        "envelopes": envelopes,
        "goals": goals,
        "available_balance": available_balance,
        "envelope_rows": envelope_rows,
        "envelope_summary": envelope_summary,
        "goal_rows": goal_rows,
        "envelope_balance_map": envelope_balance_map,
        "goal_balance_map": goal_balance_map,
        "recurring_deposit_state": recurring_deposit_state,
        "transfer_history_rows": transfer_state["transfer_history_rows"],
    }


def strict_budgeting_enabled():
    return get_setting_bool("prevent_negative_envelopes")


def available_for_expense(envelope, exclude_transaction=None):
    if envelope is None:
        return 0.0

    state = build_budget_state()
    available = round_money(state["envelope_balance_map"].get(envelope.id, 0.0))

    if (
        exclude_transaction is not None
        and exclude_transaction.type == "EXPENSE"
        and exclude_transaction.envelope_id == envelope.id
    ):
        available = round_money(available + float(exclude_transaction.amount))

    return available


def validate_strict_expense_balance(
    errors,
    transaction_type,
    envelope,
    amount,
    exclude_transaction=None,
    incoming_adjustment=0.0,
):
    if (
        not strict_budgeting_enabled()
        or transaction_type != "EXPENSE"
        or envelope is None
        or amount is None
    ):
        return

    available = round_money(
        available_for_expense(envelope, exclude_transaction) + incoming_adjustment
    )
    if round_money(amount) > available:
        errors.append(
            f"Strict budgeting is on: {envelope.name} only has "
            f"{format_currency(available)} available."
        )


# Stats helper functions
def normalize_stats_range(value):
    allowed_values = {option["value"] for option in STATS_RANGE_OPTIONS}
    if value in allowed_values:
        return value
    return "month"


def stats_range_label(range_key):
    for option in STATS_RANGE_OPTIONS:
        if option["value"] == range_key:
            return option["label"]
    return "Selected Range"


def stats_range_phrase(range_key):
    phrases = {
        "week": "this week",
        "month": "this month",
        "year": "this year",
        "all": "all recorded activity",
    }
    return phrases.get(range_key, "this range")


def stats_range_start(range_key, reference_date=None):
    reference_date = reference_date or today_date()

    if range_key == "week":
        return reference_date - timedelta(days=reference_date.weekday())
    if range_key == "month":
        return date_cls(reference_date.year, reference_date.month, 1)
    if range_key == "year":
        return date_cls(reference_date.year, 1, 1)
    return None


def transaction_matches_stats_range(txn_date, range_key, reference_date=None):
    reference_date = reference_date or today_date()

    if txn_date is None:
        return False

    if range_key == "week":
        week_start = reference_date - timedelta(days=reference_date.weekday())
        return week_start <= txn_date <= reference_date

    if range_key == "year":
        return txn_date.year == reference_date.year

    if range_key == "all":
        return True

    return (
        txn_date.year == reference_date.year and txn_date.month == reference_date.month
    )


def build_stats_period_totals(transactions, reference_date=None):
    reference_date = reference_date or today_date()
    totals = {"week": 0.0, "month": 0.0, "year": 0.0}

    for txn in transactions:
        if txn.type != "EXPENSE":
            continue

        txn_date = parse_iso_date(txn.date)
        if txn_date is None:
            continue

        if transaction_matches_stats_range(txn_date, "week", reference_date):
            totals["week"] += float(txn.amount)
        if transaction_matches_stats_range(txn_date, "month", reference_date):
            totals["month"] += float(txn.amount)
        if transaction_matches_stats_range(txn_date, "year", reference_date):
            totals["year"] += float(txn.amount)

    return totals


def build_envelope_spending_rows(expense_transactions):
    totals = defaultdict(float)
    counts = defaultdict(int)

    for txn in expense_transactions:
        label = txn.envelope.name if txn.envelope else "No Envelope"
        totals[label] += float(txn.amount)
        counts[label] += 1

    rows = []
    for label, amount in totals.items():
        rows.append(
            {
                "name": label,
                "amount": amount,
                "count": counts[label],
            }
        )

    rows.sort(key=lambda row: (-row["amount"], row["name"].lower()))
    return rows


def append_envelope_balance_event(events, event_date, envelope_id, amount):
    events.append(
        {
            "date": event_date,
            "order": len(events),
            "envelope_id": envelope_id,
            "amount": amount,
        }
    )


def build_cash_flow_rows(transactions, range_key, reference_date=None):
    reference_date = reference_date or today_date()

    if range_key == "week":
        week_start = reference_date - timedelta(days=reference_date.weekday())
        bucket_keys = [
            week_start + timedelta(days=offset)
            for offset in range((reference_date - week_start).days + 1)
        ]
        labels = {
            bucket_date: bucket_date.strftime("%a")
            for bucket_date in bucket_keys
        }
        totals = {bucket_date: {"income": 0.0, "expense": 0.0} for bucket_date in bucket_keys}

        for txn in transactions:
            txn_date = parse_iso_date(txn.date)
            if txn_date not in totals:
                continue

            if txn.type == "INCOME":
                totals[txn_date]["income"] += float(txn.amount)
            elif txn.type == "EXPENSE":
                totals[txn_date]["expense"] += float(txn.amount)

        return [
            {
                "label": labels[bucket_date],
                "income": totals[bucket_date]["income"],
                "expense": totals[bucket_date]["expense"],
            }
            for bucket_date in bucket_keys
        ]

    if range_key == "month":
        month_start = date_cls(reference_date.year, reference_date.month, 1)
        bucket_ranges = []
        bucket_start = month_start

        while bucket_start <= reference_date:
            bucket_end = min(bucket_start + timedelta(days=6), reference_date)
            bucket_ranges.append((bucket_start, bucket_end))
            bucket_start = bucket_end + timedelta(days=1)

        totals = {
            index: {"income": 0.0, "expense": 0.0}
            for index in range(len(bucket_ranges))
        }

        for txn in transactions:
            txn_date = parse_iso_date(txn.date)
            if txn_date is None or txn_date < month_start or txn_date > reference_date:
                continue

            bucket_index = (txn_date - month_start).days // 7

            if txn.type == "INCOME":
                totals[bucket_index]["income"] += float(txn.amount)
            elif txn.type == "EXPENSE":
                totals[bucket_index]["expense"] += float(txn.amount)

        rows = []
        for index, (bucket_start, bucket_end) in enumerate(bucket_ranges):
            if bucket_start == bucket_end:
                label = bucket_start.strftime("%b %d")
            else:
                label = f"{bucket_start.strftime('%b')} {bucket_start.day}-{bucket_end.day}"

            rows.append(
                {
                    "label": label,
                    "income": totals[index]["income"],
                    "expense": totals[index]["expense"],
                }
            )

        return rows

    dated_transactions = []
    for txn in transactions:
        txn_date = parse_iso_date(txn.date)
        if txn_date is not None:
            dated_transactions.append((txn, txn_date))

    if range_key == "year":
        month_keys = [
            (reference_date.year, month)
            for month in range(1, reference_date.month + 1)
        ]
    else:
        if not dated_transactions:
            return []

        first_date = min(txn_date for _, txn_date in dated_transactions)
        last_date = max(txn_date for _, txn_date in dated_transactions)
        current_month = date_cls(first_date.year, first_date.month, 1)
        final_month = date_cls(last_date.year, last_date.month, 1)
        month_keys = []

        while current_month <= final_month:
            month_keys.append((current_month.year, current_month.month))
            current_month = add_months(current_month, 1)

    totals = {
        key: {"income": 0.0, "expense": 0.0}
        for key in month_keys
    }

    for txn, txn_date in dated_transactions:
        key = (txn_date.year, txn_date.month)
        if key not in totals:
            continue

        if txn.type == "INCOME":
            totals[key]["income"] += float(txn.amount)
        elif txn.type == "EXPENSE":
            totals[key]["expense"] += float(txn.amount)

    return [
        {
            "label": date_cls(year, month, 1).strftime("%b %Y"),
            "income": totals[(year, month)]["income"],
            "expense": totals[(year, month)]["expense"],
        }
        for year, month in month_keys
    ]


def build_negative_envelope_rows(transactions, transfers, range_key, reference_date=None):
    reference_date = reference_date or today_date()
    range_start = stats_range_start(range_key, reference_date)
    envelope_names = {
        envelope.id: envelope.name for envelope in ordered_envelopes(include_archived=True)
    }
    balances = defaultdict(float)
    negative_hits = defaultdict(int)
    lowest_balances = defaultdict(float)
    events = []

    for txn in transactions:
        txn_date = parse_iso_date(txn.date)
        if txn.type != "EXPENSE" or txn.envelope_id is None or txn_date is None:
            continue

        append_envelope_balance_event(events, txn_date, txn.envelope_id, -float(txn.amount))

    for transfer in transfers:
        transfer_date = parse_iso_date(transfer.date)
        if transfer_date is None:
            continue

        if transfer.source_type == "envelope" and transfer.source_envelope_id:
            append_envelope_balance_event(
                events,
                transfer_date,
                transfer.source_envelope_id,
                -float(transfer.amount),
            )

        if transfer.destination_type == "envelope" and transfer.destination_envelope_id:
            append_envelope_balance_event(
                events,
                transfer_date,
                transfer.destination_envelope_id,
                float(transfer.amount),
            )

    events.sort(key=lambda row: (row["date"], row["order"]))

    for event in events:
        envelope_id = event["envelope_id"]
        if range_start is not None and event["date"] < range_start:
            balances[envelope_id] += event["amount"]
            continue

        if not transaction_matches_stats_range(event["date"], range_key, reference_date):
            continue

        previous_balance = balances[envelope_id]
        current_balance = previous_balance + event["amount"]
        balances[envelope_id] = current_balance

        if current_balance < lowest_balances[envelope_id]:
            lowest_balances[envelope_id] = current_balance

        if previous_balance >= -0.01 and current_balance < -0.01:
            negative_hits[envelope_id] += 1

    rows = []
    for envelope_id, hit_count in negative_hits.items():
        rows.append(
            {
                "name": envelope_names.get(envelope_id, f"Envelope #{envelope_id}"),
                "hit_count": hit_count,
                "lowest_balance": lowest_balances[envelope_id],
            }
        )

    rows.sort(key=lambda row: (-row["hit_count"], row["lowest_balance"], row["name"].lower()))
    return rows


def looks_like_subscription(txn):
    envelope_name = ""
    if txn.envelope:
        envelope_name = (txn.envelope.name or "").strip().lower()
        if envelope_name in SUBSCRIPTION_ENVELOPE_NAMES:
            return True

    haystack = " ".join(
        part for part in [txn.payee or "", txn.note or "", envelope_name] if part
    ).lower()
    return any(keyword in haystack for keyword in SUBSCRIPTION_KEYWORDS)


def largest_expense_row(expense_transactions):
    if not expense_transactions:
        return None

    largest = max(expense_transactions, key=lambda txn: float(txn.amount))
    return {
        "payee": largest.payee,
        "amount": float(largest.amount),
        "date": largest.date,
        "envelope_name": largest.envelope.name if largest.envelope else "No Envelope",
    }


def most_common_payee_row(expense_transactions):
    payee_counter = Counter(
        txn.payee for txn in expense_transactions if (txn.payee or "").strip()
    )
    if not payee_counter:
        return None

    payee, count = payee_counter.most_common(1)[0]
    return {"name": payee, "count": count}


def build_stats_recommendations(
    range_phrase,
    negative_envelope_rows,
    subscription_total,
    recurring_deposit_state,
    top_envelope,
    top_envelope_share,
    most_common_payee,
):
    recommendations = []

    if negative_envelope_rows:
        top_negative = negative_envelope_rows[0]
        recommendations.append(
            {
                "title": f"{top_negative['name']} needs more cushion",
                "detail": (
                    f"{top_negative['name']} went negative "
                    f"{top_negative['hit_count']} time"
                    f"{'' if top_negative['hit_count'] == 1 else 's'} in {range_phrase}. "
                    "Consider allocating a little more there before spending from it."
                ),
            }
        )

    if recurring_deposit_state["due_total"] > 0:
        recommendations.append(
            {
                "title": "Recurring deposits still need funding",
                "detail": (
                    f"{format_currency_compact(recurring_deposit_state['due_total'])} "
                    f"is still due across {recurring_deposit_state['due_count']} "
                    f"recurring envelope deposit"
                    f"{'' if recurring_deposit_state['due_count'] == 1 else 's'} this month."
                ),
            }
        )

    if subscription_total >= 20:
        recommendations.append(
            {
                "title": "Review recurring subscriptions",
                "detail": (
                    f"You spent {format_currency_compact(subscription_total)} on "
                    f"subscription-style charges in {range_phrase}. Removing unused "
                    "services would be an easy way to lower spending."
                ),
            }
        )

    if top_envelope and top_envelope_share >= 0.35:
        recommendations.append(
            {
                "title": f"{top_envelope['name']} is your biggest spending area",
                "detail": (
                    f"{top_envelope['name']} made up {top_envelope_share:.0%} of "
                    f"spending in {range_phrase}. Tightening that category would likely "
                    "have the biggest impact."
                ),
            }
        )

    if most_common_payee and most_common_payee["count"] >= 3:
        recommendations.append(
            {
                "title": f"{most_common_payee['name']} shows up often",
                "detail": (
                    f"{most_common_payee['name']} appeared "
                    f"{most_common_payee['count']} times in {range_phrase}. "
                    "It may help to budget for it as a recurring expense."
                ),
            }
        )

    if recommendations:
        return recommendations[:3]

    return [
        {
            "title": "Spending looks fairly steady",
            "detail": (
                "No major pressure points stood out in the selected range. "
                "Keep comparing your top categories to the amounts you planned."
            ),
        }
    ]


def build_stats_insights(
    expense_transactions,
    envelope_spending_rows,
    negative_envelope_rows,
    recurring_deposit_state,
    range_key,
):
    largest_expense = largest_expense_row(expense_transactions)
    most_common_payee = most_common_payee_row(expense_transactions)
    top_envelope = envelope_spending_rows[0] if envelope_spending_rows else None
    range_phrase = stats_range_phrase(range_key)
    total_spending = sum(float(txn.amount) for txn in expense_transactions)
    top_envelope_share = 0.0
    if top_envelope and total_spending > 0:
        top_envelope_share = top_envelope["amount"] / total_spending

    subscription_transactions = [
        txn for txn in expense_transactions if looks_like_subscription(txn)
    ]
    subscription_total = sum(float(txn.amount) for txn in subscription_transactions)
    recurring_due_rows = [
        row for row in recurring_deposit_state["rows"] if row["remaining_due"] > 0
    ]
    recurring_top_due = recurring_due_rows[0] if recurring_due_rows else None

    return {
        "largest_expense": largest_expense,
        "most_common_payee": most_common_payee,
        "top_envelope": top_envelope,
        "top_envelope_share": top_envelope_share,
        "negative_envelopes": negative_envelope_rows[:3],
        "recurring_deposits": {
            "due_total": recurring_deposit_state["due_total"],
            "due_count": recurring_deposit_state["due_count"],
            "funded_total": recurring_deposit_state["funded_total"],
            "monthly_target_total": recurring_deposit_state["monthly_target_total"],
            "top_due": recurring_top_due,
        },
        "recommendations": build_stats_recommendations(
            range_phrase,
            negative_envelope_rows,
            subscription_total,
            recurring_deposit_state,
            top_envelope,
            top_envelope_share,
            most_common_payee,
        ),
        "subscription_total": subscription_total,
        "subscription_count": len(subscription_transactions),
    }


# Allocation helper functions
def resolve_location(loc_type, envelope_id, goal_id):
    if loc_type == "available":
        return {
            "type": "available",
            "envelope": None,
            "goal": None,
            "label": "Available Balance",
        }

    if loc_type == "envelope":
        if not envelope_id:
            return {"error": "Please select an envelope for that location."}

        envelope = get_envelope_from_form(envelope_id)
        if envelope is None:
            return {"error": "Selected envelope does not exist."}

        return {
            "type": "envelope",
            "envelope": envelope,
            "goal": None,
            "label": f"Envelope: {envelope.name}",
        }

    if loc_type == "goal":
        if not goal_id:
            return {"error": "Please select a goal for that location."}

        goal = get_goal_from_form(goal_id)
        if goal is None:
            return {"error": "Selected goal does not exist."}

        return {
            "type": "goal",
            "envelope": None,
            "goal": goal,
            "label": f"Goal: {goal.name}",
        }

    return {"error": "Please choose a valid location type."}


def is_same_location(source, destination):
    if source["type"] != destination["type"]:
        return False

    if source["type"] == "available":
        return True

    if source["type"] == "envelope":
        return (
            source["envelope"] is not None
            and destination["envelope"] is not None
            and source["envelope"].id == destination["envelope"].id
        )

    if source["type"] == "goal":
        return (
            source["goal"] is not None
            and destination["goal"] is not None
            and source["goal"].id == destination["goal"].id
        )

    return False


# Dashboard helper functions
def build_dashboard_summary(transactions, state, reference_date=None):
    reference_date = reference_date or today_date()
    month_income = 0.0
    month_spending = 0.0

    for txn in transactions:
        txn_date = parse_iso_date(txn.date)
        if not transaction_matches_stats_range(txn_date, "month", reference_date):
            continue

        if txn.type == "INCOME":
            month_income += float(txn.amount)
        elif txn.type == "EXPENSE":
            month_spending += float(txn.amount)

    return {
        "available_balance": state["available_balance"],
        "month_income": month_income,
        "month_spending": month_spending,
        "active_goal_count": len(state["goal_rows"]),
    }


# Dashboard route
@app.route("/")
def dashboard():
    reference_date = today_date()
    state = build_budget_state()
    transactions = list(
        Transaction.select().order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    summary = build_dashboard_summary(transactions, state, reference_date)

    return render_template(
        "dashboard.html",
        page_title="Dashboard",
        nav="dashboard",
        summary=summary,
        envelope_rows=state["envelope_rows"],
        goal_rows=state["goal_rows"],
        dashboard_double_click_fund_negative=get_setting_bool(
            "dashboard_double_click_fund_negative",
            default=True,
        ),
    )


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        auto_recurring_deposits = "1" if request.form.get("auto_recurring_deposits") else "0"
        prevent_negative_envelopes = (
            "1" if request.form.get("prevent_negative_envelopes") else "0"
        )
        dashboard_double_click_fund_negative = (
            "1" if request.form.get("dashboard_double_click_fund_negative") else "0"
        )

        set_setting_value("auto_recurring_deposits", auto_recurring_deposits)
        set_setting_value("prevent_negative_envelopes", prevent_negative_envelopes)
        if auto_recurring_deposits == "1":
            auto_retry_recurring_deposits = (
                "1" if request.form.get("auto_retry_recurring_deposits") else "0"
            )
        else:
            auto_retry_recurring_deposits = "0"

        set_setting_value(
            "auto_retry_recurring_deposits",
            auto_retry_recurring_deposits,
        )
        set_setting_value(
            "dashboard_double_click_fund_negative",
            dashboard_double_click_fund_negative,
        )

        flash("Settings updated successfully.", "success")
        return redirect(url_for("settings_page"))

    return render_template(
        "settings.html",
        page_title="Settings",
        nav="settings",
        form=build_settings_form(),
    )


# Subscription routes
@app.route("/subscriptions", methods=["GET", "POST"])
def subscriptions_page():
    calendar_month = parse_calendar_month(request.args.get("month"))
    selected_day = parse_calendar_day(request.args.get("day"), calendar_month)
    form = build_subscription_form()

    if request.method == "POST":
        form = read_subscription_form()
        errors, amount, renewal_date = validate_subscription_form(form)

        if errors:
            flash_errors(errors)
            calendar_month = parse_calendar_month(
                renewal_date.strftime("%Y-%m") if renewal_date else request.args.get("month")
            )
            selected_day = parse_calendar_day(
                renewal_date.isoformat() if renewal_date else request.args.get("day"),
                calendar_month,
            )
        else:
            envelope = get_or_create_subscriptions_envelope()
            Subscription.create(
                name=form["name"],
                amount=amount,
                renewal_date=renewal_date.isoformat(),
                frequency=form["frequency"],
                envelope=envelope,
                note=form["note"] or None,
                active=1,
                created_at=now_iso(),
            )
            flash("Subscription saved.", "success")
            return redirect(
                url_for(
                    "subscriptions_page",
                    month=renewal_date.strftime("%Y-%m"),
                )
            )

    subscriptions = list(
        Subscription.select(Subscription, Envelope)
        .join(Envelope, JOIN.LEFT_OUTER)
        .where(Subscription.active == 1)
        .order_by(Subscription.name.asc())
    )
    subscription_rows = build_subscription_rows(subscriptions, calendar_month)
    calendar_data = build_subscription_calendar(
        subscription_rows,
        calendar_month,
        selected_day,
    )
    selected_month_rows = [
        row for row in subscription_rows if row["next_renewal"] is not None
    ]
    selected_month_total = round_money(sum(row["amount"] for row in selected_month_rows))

    return render_template(
        "subscriptions.html",
        page_title="Subscriptions",
        nav="subscriptions",
        form=form,
        frequency_options=SUBSCRIPTION_FREQUENCIES.items(),
        subscription_rows=subscription_rows,
        calendar=calendar_data,
        summary={
            "active_count": len(subscription_rows),
            "selected_month_count": len(selected_month_rows),
            "selected_month_total": selected_month_total,
        },
    )


@app.post("/subscriptions/<int:subscription_id>/delete")
def delete_subscription(subscription_id):
    subscription = Subscription.get_or_none(Subscription.id == subscription_id)
    redirect_month = request.form.get("month") or today_date().strftime("%Y-%m")

    if subscription is None:
        flash("Subscription not found.", "error")
    else:
        subscription.delete_instance()
        flash("Subscription removed.", "success")

    return redirect(url_for("subscriptions_page", month=redirect_month))


# Envelope routes
@app.route("/envelopes", methods=["GET", "POST"])
def envelopes_page():
    form = build_envelope_form()

    if request.method == "POST":
        form = read_envelope_form()
        errors, recurring_monthly_amount = validate_envelope_form(form)

        if errors:
            flash_errors(errors)
        else:
            Envelope.create(
                name=form["name"],
                recurring_monthly_amount=recurring_monthly_amount,
                created_at=now_iso(),
            )
            flash("Envelope created successfully.", "success")
            return redirect(url_for("envelopes_page"))

    return render_template(
        "envelopes.html",
        page_title="Envelope Management",
        nav="envelopes",
        envelopes=list(ordered_envelopes()),
        archived_envelopes=list(ordered_archived_envelopes()),
        form=form,
    )


@app.route("/envelopes/<int:envelope_id>/edit", methods=["GET", "POST"])
def edit_envelope(envelope_id):
    envelope = Envelope.get_or_none(Envelope.id == envelope_id)
    if envelope is None:
        flash("Envelope not found.", "error")
        return redirect(url_for("envelopes_page"))

    form = build_envelope_form(envelope)

    if request.method == "POST":
        form = read_envelope_form()
        errors, recurring_monthly_amount = validate_envelope_form(form, envelope.id)

        if errors:
            flash_errors(errors)
        else:
            envelope.name = form["name"]
            envelope.recurring_monthly_amount = recurring_monthly_amount
            envelope.save()

            flash("Envelope updated successfully.", "success")
            return redirect(url_for("envelopes_page"))

    return render_template(
        "edit_envelope.html",
        page_title="Edit Envelope",
        nav="envelopes",
        envelope=envelope,
        form=form,
    )


@app.route("/envelopes/<int:envelope_id>/delete", methods=["POST"])
def delete_envelope(envelope_id):
    envelope = Envelope.get_or_none(Envelope.id == envelope_id)
    if envelope is None:
        flash("Envelope not found.", "error")
        return redirect(url_for("envelopes_page"))

    transaction_count = Transaction.select().where(Transaction.envelope == envelope).count()
    allocation_count = Allocation.select().where(Allocation.envelope == envelope).count()
    goal_count = Goal.select().where(Goal.envelope == envelope).count()
    transfer_out_count = Transfer.select().where(Transfer.source_envelope == envelope).count()
    transfer_in_count = Transfer.select().where(
        Transfer.destination_envelope == envelope
    ).count()

    if (
        transaction_count > 0
        or allocation_count > 0
        or goal_count > 0
        or transfer_out_count > 0
        or transfer_in_count > 0
    ):
        flash(
            "This envelope cannot be deleted because it is still referenced by "
            "transactions, transfers, goals, or other history. Archive it instead "
            "if you just want it hidden from the active budgeting screens.",
            "error",
        )
        return redirect(url_for("envelopes_page"))

    envelope.delete_instance()
    flash("Envelope deleted successfully.", "success")
    return redirect(url_for("envelopes_page"))


@app.route("/envelopes/<int:envelope_id>/archive", methods=["POST"])
def archive_envelope(envelope_id):
    envelope = Envelope.get_or_none(Envelope.id == envelope_id)
    if envelope is None:
        flash("Envelope not found.", "error")
        return redirect(url_for("envelopes_page"))

    if not envelope.active:
        flash("Envelope is already archived.", "success")
        return redirect(url_for("envelopes_page"))

    can_archive, message = can_archive_envelope(envelope)
    if not can_archive:
        flash(message, "error")
        return redirect(url_for("envelopes_page"))

    envelope.active = 0
    envelope.save()
    flash("Envelope archived.", "success")
    return redirect(url_for("envelopes_page"))


@app.route("/envelopes/<int:envelope_id>/restore", methods=["POST"])
def restore_envelope(envelope_id):
    envelope = Envelope.get_or_none(Envelope.id == envelope_id)
    if envelope is None:
        flash("Envelope not found.", "error")
        return redirect(url_for("envelopes_page"))

    if envelope.active:
        flash("Envelope is already active.", "success")
        return redirect(url_for("envelopes_page"))

    envelope.active = 1
    envelope.save()
    flash("Envelope restored.", "success")
    return redirect(url_for("envelopes_page"))


def recurring_deposit_row_for_envelope(envelope, reference_date=None):
    recurring_state = build_recurring_deposit_state([envelope], reference_date)
    if recurring_state["rows"]:
        return recurring_state["rows"][0]
    return None


def create_available_to_envelope_transfer(envelope, amount, transfer_date=None):
    transfer_date = transfer_date or today_date().isoformat()
    return Transfer.create(
        date=transfer_date,
        source_type="available",
        source_envelope=None,
        source_goal=None,
        destination_type="envelope",
        destination_envelope=envelope,
        destination_goal=None,
        amount=round_money(amount),
        created_at=now_iso(),
    )


def create_available_to_goal_transfer(goal, amount, transfer_date=None):
    transfer_date = transfer_date or today_date().isoformat()
    return Transfer.create(
        date=transfer_date,
        source_type="available",
        source_envelope=None,
        source_goal=None,
        destination_type="goal",
        destination_envelope=None,
        destination_goal=goal,
        amount=round_money(amount),
        created_at=now_iso(),
    )


@app.route("/allocate/recurring/<int:envelope_id>", methods=["POST"])
def apply_recurring_deposit(envelope_id):
    envelope = Envelope.get_or_none(
        (Envelope.id == envelope_id) & (Envelope.active == 1)
    )
    if envelope is None:
        flash("Recurring deposit envelope not found.", "error")
        return redirect(url_for("allocate_funds"))

    recurring_row = recurring_deposit_row_for_envelope(envelope)
    if recurring_row is None:
        flash("This envelope does not have a recurring monthly deposit set.", "error")
        return redirect(url_for("allocate_funds"))

    amount_due = recurring_row["remaining_due"]
    if amount_due <= 0:
        flash(f"{envelope.name} is already fully funded for this month.", "success")
        return redirect(url_for("allocate_funds"))

    state = build_budget_state()
    if round_money(amount_due) > round_money(state["available_balance"]):
        flash(
            "Not enough available balance to apply that recurring deposit. "
            f"You still need {format_currency(amount_due)} for {envelope.name}.",
            "error",
        )
        return redirect(url_for("allocate_funds"))

    create_available_to_envelope_transfer(envelope, amount_due)
    flash(
        f"Applied {format_currency(amount_due)} recurring deposit to {envelope.name}.",
        "success",
    )
    return redirect(url_for("allocate_funds"))


@app.route("/allocate/recurring/apply-all", methods=["POST"])
def apply_all_recurring_deposits():
    state = build_budget_state()
    recurring_rows = [
        row
        for row in state["recurring_deposit_state"]["rows"]
        if row["remaining_due"] > 0
    ]

    if not recurring_rows:
        flash("All recurring deposits are already fully funded for this month.", "success")
        return redirect(url_for("allocate_funds"))

    total_due = round_money(sum(row["remaining_due"] for row in recurring_rows))
    if round_money(total_due) > round_money(state["available_balance"]):
        flash(
            "Not enough available balance to apply all recurring deposits. "
            f"You need {format_currency(total_due)} but only have "
            f"{format_currency(state['available_balance'])} available.",
            "error",
        )
        return redirect(url_for("allocate_funds"))

    with db.atomic():
        for row in recurring_rows:
            envelope = get_envelope_from_form(row["id"])
            if envelope is None:
                continue
            create_available_to_envelope_transfer(envelope, row["remaining_due"])

    flash(
        f"Applied {format_currency(total_due)} across {len(recurring_rows)} recurring deposits.",
        "success",
    )
    return redirect(url_for("allocate_funds"))


@app.route("/allocate/envelopes/<int:envelope_id>/fund-negative", methods=["POST"])
def fund_negative_envelope(envelope_id):
    return_endpoint = (
        "dashboard" if request.form.get("return_to") == "dashboard" else "allocate_funds"
    )
    envelope = Envelope.get_or_none(
        (Envelope.id == envelope_id) & (Envelope.active == 1)
    )
    if envelope is None:
        flash("Envelope not found.", "error")
        return redirect(url_for(return_endpoint))

    state = build_budget_state()
    envelope_row = next(
        (row for row in state["envelope_rows"] if row["id"] == envelope_id),
        None,
    )
    if envelope_row is None:
        flash("Envelope balance could not be found.", "error")
        return redirect(url_for(return_endpoint))

    funding_needed = round_money(envelope_row["fund_to_zero_amount"])
    if funding_needed <= 0:
        flash(f"{envelope.name} is already at or above zero.", "success")
        return redirect(url_for(return_endpoint))

    available_balance = round_money(state["available_balance"])
    if funding_needed > available_balance:
        flash(
            f"Not enough available balance to fully fund {envelope.name}. "
            f"You need {format_currency(funding_needed)} but only have "
            f"{format_currency(available_balance)} available.",
            "error",
        )
        return redirect(url_for(return_endpoint))

    create_available_to_envelope_transfer(envelope, funding_needed)
    flash(
        f"Funded {format_currency(funding_needed)} to bring {envelope.name} back to zero.",
        "success",
    )
    return redirect(url_for(return_endpoint))


# Allocation route
@app.route("/allocate", methods=["GET", "POST"])
def allocate_funds():
    current_date = today_date().isoformat()
    state = build_budget_state()
    form = {
        "date": current_date,
        "amount": "",
        "source_type": "available",
        "source_envelope_id": "",
        "source_goal_id": "",
        "destination_type": "envelope",
        "destination_envelope_id": "",
        "destination_goal_id": "",
    }

    if request.method == "POST":
        form = {
            "date": current_date,
            "amount": (request.form.get("amount") or "").strip(),
            "source_type": (request.form.get("source_type") or "").strip(),
            "source_envelope_id": (request.form.get("source_envelope_id") or "").strip(),
            "source_goal_id": (request.form.get("source_goal_id") or "").strip(),
            "destination_type": (request.form.get("destination_type") or "").strip(),
            "destination_envelope_id": (
                request.form.get("destination_envelope_id") or ""
            ).strip(),
            "destination_goal_id": (request.form.get("destination_goal_id") or "").strip(),
        }
        errors = []

        amount = parse_positive_amount(
            form["amount"],
            "Amount must be a valid number (ex: 50.00).",
            errors,
        )

        source = resolve_location(
            form["source_type"],
            form["source_envelope_id"],
            form["source_goal_id"],
        )
        if "error" in source:
            errors.append(source["error"])

        destination = resolve_location(
            form["destination_type"],
            form["destination_envelope_id"],
            form["destination_goal_id"],
        )
        if "error" in destination:
            errors.append(destination["error"])

        if "error" not in source and "error" not in destination:
            if is_same_location(source, destination):
                errors.append("Source and destination cannot be the same.")

        if amount is not None and "error" not in source:
            if (
                source["type"] == "available"
                and round_money(amount) > round_money(state["available_balance"])
            ):
                errors.append("You cannot move more than the available balance.")

            if source["type"] == "envelope":
                envelope_available = round_money(
                    state["envelope_balance_map"].get(
                        source["envelope"].id,
                        0.0,
                    )
                )
                if round_money(amount) > envelope_available:
                    errors.append(
                        "You cannot move more than that envelope currently has available."
                    )

            if source["type"] == "goal":
                goal_funded = round_money(
                    state["goal_balance_map"].get(source["goal"].id, 0.0)
                )
                if round_money(amount) > goal_funded:
                    errors.append(
                        "You cannot move more than that goal currently has funded."
                    )

        if errors:
            flash_errors(errors)
            state = build_budget_state()
        else:
            Transfer.create(
                date=current_date,
                source_type=source["type"],
                source_envelope=source["envelope"],
                source_goal=source["goal"],
                destination_type=destination["type"],
                destination_envelope=destination["envelope"],
                destination_goal=destination["goal"],
                amount=round_money(amount),
                created_at=now_iso(),
            )
            flash("Transfer saved successfully.", "success")
            return redirect(url_for("allocate_funds"))

    return render_template(
        "allocate.html",
        page_title="Allocate Funds",
        nav="allocate",
        envelopes=state["envelopes"],
        goals=state["goals"],
        envelope_rows=state["envelope_rows"],
        envelope_summary=state["envelope_summary"],
        goal_rows=state["goal_rows"],
        recurring_deposit_state=state["recurring_deposit_state"],
        transfer_history_rows=state["transfer_history_rows"],
        summary={"available_to_allocate": state["available_balance"]},
        current_date=current_date,
        form=form,
    )


# Transaction routes
@app.route("/add", methods=["GET", "POST"])
def add_expense_income():
    envelopes = ordered_envelopes()
    form = build_transaction_form()
    import_preview = None
    preview_token = (request.args.get("import_preview") or "").strip()
    if preview_token:
        import_preview = load_transaction_import_preview(preview_token)
        if import_preview is None:
            flash("That CSV import preview is no longer available.", "error")

    if request.method == "POST":
        form = read_transaction_form()
        errors, amount, envelope = validate_transaction_form(form)
        validate_strict_expense_balance(errors, form["type"], envelope, amount)

        if errors:
            flash_errors(errors)
        else:
            if form["type"] == "INCOME":
                envelope = None

            Transaction.create(
                date=form["date"],
                payee=form["payee"],
                envelope=envelope,
                amount=amount,
                type=form["type"],
                note=form["note"] or None,
                created_at=now_iso(),
            )

            flash("Transaction saved.", "success")
            return redirect(url_for("add_expense_income"))

    return render_template(
        "add_txn.html",
        page_title="Add Expense/Income",
        nav="add",
        envelopes=envelopes,
        form=form,
        import_preview=import_preview,
    )


@app.post("/transactions/import/preview")
def preview_transaction_import():
    upload = request.files.get("csv_file")
    if upload is None or not (upload.filename or "").strip():
        flash("Please choose a CSV file to import.", "error")
        return redirect(url_for("add_expense_income"))

    try:
        payload = parse_transaction_import_csv(upload)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("add_expense_income"))

    preview_token = save_transaction_import_preview(payload)
    return redirect(url_for("add_expense_income", import_preview=preview_token))


@app.post("/transactions/import/commit")
def commit_transaction_import():
    preview_token = (request.form.get("preview_token") or "").strip()
    payload = load_transaction_import_preview(preview_token)
    if payload is None:
        flash("That CSV import preview is no longer available.", "error")
        return redirect(url_for("add_expense_income"))

    known_fingerprints = existing_transaction_fingerprints()
    imported_count = 0
    duplicate_count = 0
    skipped_count = 0

    with db.atomic():
        for row in payload["rows"]:
            if row.get("skip_reason"):
                skipped_count += 1
                continue

            fingerprint = row.get("fingerprint", "")
            if not fingerprint:
                skipped_count += 1
                continue

            if fingerprint in known_fingerprints:
                duplicate_count += 1
                continue

            Transaction.create(
                date=row["date"],
                payee=row["payee"],
                envelope=None,
                amount=row["amount"],
                type=row["transaction_type"],
                note=row["note"] or None,
                created_at=now_iso(),
            )
            known_fingerprints.add(fingerprint)
            imported_count += 1

    delete_transaction_import_preview(preview_token)

    if imported_count:
        summary_message = (
            f"Imported {imported_count} transaction"
            f"{'' if imported_count == 1 else 's'} from {payload['source_filename']}."
        )
        if duplicate_count or skipped_count:
            summary_message += (
                f" Skipped {duplicate_count} duplicate"
                f"{'' if duplicate_count == 1 else 's'} and {skipped_count} unsupported row"
                f"{'' if skipped_count == 1 else 's'}."
            )
        flash(summary_message, "success")
    else:
        flash(
            "No transactions were imported. Every row was either a duplicate or unsupported.",
            "error",
        )

    return redirect(url_for("add_expense_income"))


@app.post("/transactions/import/cancel")
def cancel_transaction_import():
    preview_token = (request.form.get("preview_token") or "").strip()
    delete_transaction_import_preview(preview_token)
    flash("CSV import preview dismissed.", "success")
    return redirect(url_for("add_expense_income"))


@app.route("/history")
def transaction_history():
    tx_type = (request.args.get("type") or "").strip().upper()
    envelope_id = (request.args.get("envelope_id") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()

    envelopes = ordered_envelopes(include_archived=True)
    query = (
        Transaction.select(Transaction, Envelope)
        .join(Envelope, JOIN.LEFT_OUTER)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(200)
    )

    if tx_type in TRANSACTION_TYPES:
        query = query.where(Transaction.type == tx_type)
    else:
        tx_type = ""

    if envelope_id:
        try:
            query = query.where(Transaction.envelope == int(envelope_id))
        except ValueError:
            envelope_id = ""

    if start_date:
        if has_valid_iso_date(start_date):
            query = query.where(Transaction.date >= start_date)
        else:
            start_date = ""

    if end_date:
        if has_valid_iso_date(end_date):
            query = query.where(Transaction.date <= end_date)
        else:
            end_date = ""

    transactions = list(query)
    income_total = 0.0
    spending_total = 0.0

    for transaction in transactions:
        if transaction.type == "INCOME":
            income_total += float(transaction.amount)
        else:
            spending_total += float(transaction.amount)

    return render_template(
        "history.html",
        page_title="Transaction History",
        nav="history",
        transactions=transactions,
        envelopes=envelopes,
        summary={
            "count": len(transactions),
            "income": income_total,
            "spending": spending_total,
        },
        filters={
            "type": tx_type,
            "envelope_id": envelope_id,
            "start_date": start_date,
            "end_date": end_date,
        },
    )


@app.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
def edit_transaction(txn_id):
    txn = Transaction.get_or_none(Transaction.id == txn_id)
    if txn is None:
        flash("Transaction not found.", "error")
        return redirect(url_for("transaction_history"))

    envelopes = ordered_envelopes(include_archived=True)
    form = build_transaction_form(txn)

    if request.method == "POST":
        form = read_transaction_form(default_type="")
        errors, amount, envelope = validate_transaction_form(form)
        pending_envelope_transfer_amount = 0.0
        if (
            amount is not None
            and txn.type == "EXPENSE"
            and form["type"] == "EXPENSE"
            and txn.envelope_id is not None
            and envelope is not None
            and txn.envelope_id != envelope.id
        ):
            pending_envelope_transfer_amount = round_money(
                min(float(txn.amount), amount)
            )
        validate_strict_expense_balance(
            errors,
            form["type"],
            envelope,
            amount,
            exclude_transaction=txn,
            incoming_adjustment=pending_envelope_transfer_amount,
        )

        if errors:
            flash_errors(errors)
        else:
            old_type = txn.type
            old_amount = float(txn.amount)
            old_envelope = txn.envelope

            txn.date = form["date"]
            txn.type = form["type"]
            txn.amount = amount
            txn.payee = form["payee"]
            txn.envelope = None if form["type"] == "INCOME" else envelope
            txn.note = form["note"] or None

            with db.atomic():
                txn.save()

                if (
                    old_type == "EXPENSE"
                    and form["type"] == "EXPENSE"
                    and old_envelope is not None
                    and envelope is not None
                    and old_envelope.id != envelope.id
                ):
                    transfer_amount = round_money(min(old_amount, amount))
                    if transfer_amount > 0:
                        Transfer.create(
                            date=form["date"],
                            source_type="envelope",
                            source_envelope=old_envelope,
                            source_goal=None,
                            destination_type="envelope",
                            destination_envelope=envelope,
                            destination_goal=None,
                            amount=transfer_amount,
                            created_at=now_iso(),
                        )

            flash("Transaction updated.", "success")
            return redirect(url_for("transaction_history"))

    return render_template(
        "edit_txn.html",
        page_title="Edit Transaction",
        nav="history",
        envelopes=envelopes,
        form=form,
    )


@app.route("/transactions/<int:txn_id>/delete", methods=["POST"])
def delete_transaction(txn_id):
    txn = Transaction.get_or_none(Transaction.id == txn_id)
    if txn is None:
        flash("Transaction not found.", "error")
        return redirect(url_for("transaction_history"))

    txn.delete_instance()
    flash("Transaction deleted.", "success")
    return redirect(url_for("transaction_history"))


@app.post("/dev/reset")
def dev_reset():
    if not app.config.get("CAN_CLEAR_DATA"):
        return ("Not found", 404)

    if request.form.get("confirm") != "yes":
        flash("Reset cancelled.", "error")
        return redirect(url_for("settings_page"))

    with db.atomic():
        Transfer.delete().execute()
        Transaction.delete().execute()
        Allocation.delete().execute()
        Subscription.delete().execute()
        Goal.delete().execute()
        Envelope.delete().execute()

    flash("All application data cleared.", "success")
    return redirect(url_for("settings_page"))


@app.post("/dev/clear/<clear_target>")
def clear_data_section(clear_target):
    if not app.config.get("CAN_CLEAR_DATA"):
        return ("Not found", 404)

    if request.form.get("confirm") != "yes":
        flash("Clear cancelled.", "error")
        return redirect(url_for("settings_page"))

    with db.atomic():
        if clear_target == "transactions":
            Transaction.delete().execute()
            flash("All transactions cleared.", "success")
        elif clear_target == "subscriptions":
            Subscription.delete().execute()
            flash("All subscriptions cleared.", "success")
        elif clear_target == "goals":
            Transfer.delete().where(
                (Transfer.source_type == "goal")
                | (Transfer.destination_type == "goal")
                | (Transfer.source_goal.is_null(False))
                | (Transfer.destination_goal.is_null(False))
            ).execute()
            Goal.delete().execute()
            flash("All goals and goal transfers cleared.", "success")
        elif clear_target == "envelopes":
            Transfer.delete().where(
                (Transfer.source_type == "envelope")
                | (Transfer.destination_type == "envelope")
                | (Transfer.destination_type == RECURRING_DUE_DESTINATION_TYPE)
                | (Transfer.source_envelope.is_null(False))
                | (Transfer.destination_envelope.is_null(False))
            ).execute()
            Allocation.delete().execute()
            Transaction.update(envelope=None).execute()
            Goal.update(envelope=None).execute()
            Subscription.update(envelope=None).execute()
            Envelope.delete().execute()
            flash(
                "All envelopes cleared. Related envelope transfers were removed, and "
                "transactions, goals, and subscriptions were detached from envelopes.",
                "success",
            )
        else:
            flash("Unknown clear action.", "error")

    return redirect(url_for("settings_page"))


@app.post("/app/close")
def close_app():
    if not app.config.get("CAN_CLOSE_APP"):
        return ("Not found", 404)

    if request.form.get("confirm") != "yes":
        return {"ok": False, "message": "Confirmation required."}, 400

    shutdown_callback = app.config.get("REQUEST_APP_SHUTDOWN")
    if shutdown_callback is None:
        return {"ok": False, "message": "Shutdown is unavailable."}, 500

    schedule_app_shutdown(shutdown_callback)
    return {"ok": True}


# Goal routes
@app.route("/goals", methods=["GET", "POST"])
def goals_planning():
    form = build_goal_form()

    if request.method == "POST":
        form = read_goal_form()
        errors, target_amount, target_date = validate_goal_form(form)

        if errors:
            flash_errors(errors)
        else:
            Goal.create(
                name=form["name"],
                target_amount=target_amount,
                target_date=target_date.isoformat(),
                contribution_frequency=form["contribution_frequency"],
                created_at=now_iso(),
                active=1,
            )
            flash("Goal created successfully.", "success")
            return redirect(url_for("goals_planning"))

    state = build_budget_state()
    active_goals = list(ordered_active_goals())
    archived_goals = list(
        Goal.select()
        .where(Goal.active == 0)
        .order_by(Goal.name.asc())
    )
    transfer_state = build_transfer_state(limit_history=200)
    active_goal_rows = build_goal_rows(active_goals, transfer_state["goal_transfer_map"])
    archived_goal_rows = build_goal_rows(
        archived_goals,
        transfer_state["goal_transfer_map"],
    )
    available_balance = state["available_balance"]
    recommended_goal_count = sum(
        1
        for row in active_goal_rows
        if round_money(row["current_period_remaining_amount"]) > 0
    )
    recommended_goal_total = round_money(
        sum(row["current_period_remaining_amount"] for row in active_goal_rows)
    )
    can_fund_all_recommended = (
        recommended_goal_count > 0
        and round_money(recommended_goal_total)
        <= round_money(available_balance)
    )

    return render_template(
        "goals.html",
        page_title="Goals Planning",
        nav="goals",
        form=form,
        goal_frequency_options=GOAL_FREQUENCIES.items(),
        active_goal_rows=active_goal_rows,
        archived_goal_rows=archived_goal_rows,
        available_balance=available_balance,
        recommended_goal_count=recommended_goal_count,
        recommended_goal_total=recommended_goal_total,
        can_fund_all_recommended=can_fund_all_recommended,
    )


@app.route("/goals/<int:goal_id>/fund-recommended", methods=["POST"])
def fund_recommended_goal(goal_id):
    goal = Goal.get_or_none((Goal.id == goal_id) & (Goal.active == 1))
    if goal is None:
        flash("Goal not found.", "error")
        return redirect(url_for("goals_planning"))

    transfer_state = build_transfer_state(limit_history=200)
    goal_row = build_goal_rows([goal], transfer_state["goal_transfer_map"])[0]
    recommended_amount = round_money(goal_row["current_period_remaining_amount"])

    if recommended_amount <= 0:
        flash(f"{goal.name} is already funded for the current period.", "success")
        return redirect(url_for("goals_planning"))

    available_balance = build_budget_state()["available_balance"]
    if recommended_amount > round_money(available_balance):
        flash(
            f"Not enough available balance to fund {goal.name}. "
            f"You need {format_currency(recommended_amount)} but only have "
            f"{format_currency(available_balance)} available.",
            "error",
        )
        return redirect(url_for("goals_planning"))

    create_available_to_goal_transfer(goal, recommended_amount)
    flash(
        f"Funded {format_currency(recommended_amount)} toward {goal.name} for "
        f"{goal_row['current_period_label']}.",
        "success",
    )
    return redirect(url_for("goals_planning"))


@app.route("/goals/fund-recommended/apply-all", methods=["POST"])
def fund_all_recommended_goals():
    active_goals = list(ordered_active_goals())
    if not active_goals:
        flash("No active goals are available to fund.", "success")
        return redirect(url_for("goals_planning"))

    transfer_state = build_transfer_state(limit_history=200)
    goal_rows = build_goal_rows(active_goals, transfer_state["goal_transfer_map"])
    recommended_rows = [
        row for row in goal_rows if round_money(row["current_period_remaining_amount"]) > 0
    ]

    if not recommended_rows:
        flash("All active goals are already funded for the current period.", "success")
        return redirect(url_for("goals_planning"))

    total_recommended = round_money(
        sum(row["current_period_remaining_amount"] for row in recommended_rows)
    )
    available_balance = build_budget_state()["available_balance"]
    if total_recommended > round_money(available_balance):
        flash(
            "Not enough available balance to fund all recommended goal amounts. "
            f"You need {format_currency(total_recommended)} but only have "
            f"{format_currency(available_balance)} available.",
            "error",
        )
        return redirect(url_for("goals_planning"))

    goal_map = {goal.id: goal for goal in active_goals}
    with db.atomic():
        for row in recommended_rows:
            goal = goal_map.get(row["id"])
            if goal is None:
                continue
            create_available_to_goal_transfer(
                goal,
                row["current_period_remaining_amount"],
            )

    flash(
        f"Funded {format_currency(total_recommended)} across "
        f"{len(recommended_rows)} goal recommendations.",
        "success",
    )
    return redirect(url_for("goals_planning"))


@app.route("/goals/<int:goal_id>")
def goal_detail(goal_id):
    goal = Goal.get_or_none(Goal.id == goal_id)
    if goal is None:
        flash("Goal not found.", "error")
        return redirect(url_for("goals_planning"))

    transfer_state = build_transfer_state(limit_history=100)
    goal_row = build_goal_rows([goal], transfer_state["goal_transfer_map"])[0]

    return render_template(
        "goal_detail.html",
        page_title=goal.name,
        nav="goals",
        goal=goal,
        goal_row=goal_row,
        goal_chart=build_goal_progress_chart([goal_row]),
    )


@app.route("/goals/<int:goal_id>/edit", methods=["GET", "POST"])
def edit_goal(goal_id):
    goal = Goal.get_or_none(Goal.id == goal_id)
    if goal is None:
        flash("Goal not found.", "error")
        return redirect(url_for("goals_planning"))

    state = build_budget_state()
    available_balance = state["available_balance"]
    current_saved_amount = goal_saved_amount(goal.id)
    form = build_goal_form(goal, current_saved_amount)

    if request.method == "POST":
        form = read_goal_form()
        errors, target_amount, target_date = validate_goal_form(form, goal.id)
        desired_saved_amount = parse_non_negative_amount(
            form["amount_saved"],
            "Amount saved must be a valid number (ex: 250.00).",
            errors,
        )
        adjustment_amount = None

        if desired_saved_amount is not None:
            adjustment_amount = round(desired_saved_amount - current_saved_amount, 2)
            if adjustment_amount > available_balance + 0.01:
                errors.append(
                    "You cannot increase amount saved by more than the available balance."
                )

        if errors:
            flash_errors(errors)
        else:
            goal.name = form["name"]
            goal.target_amount = target_amount
            goal.target_date = target_date.isoformat()
            goal.contribution_frequency = form["contribution_frequency"]
            goal.save()

            if adjustment_amount and adjustment_amount > 0.01:
                create_available_to_goal_transfer(goal, adjustment_amount)
            elif adjustment_amount and adjustment_amount < -0.01:
                Transfer.create(
                    date=today_date().isoformat(),
                    source_type="goal",
                    source_goal=goal,
                    destination_type="available",
                    amount=abs(adjustment_amount),
                    created_at=now_iso(),
                )

            flash("Goal updated successfully.", "success")
            return redirect(url_for("goal_detail", goal_id=goal.id))

    return render_template(
        "edit_goal.html",
        page_title="Edit Goal",
        nav="goals",
        goal=goal,
        form=form,
        available_balance=available_balance,
        goal_frequency_options=GOAL_FREQUENCIES.items(),
    )


@app.route("/goals/<int:goal_id>/archive", methods=["POST"])
def archive_goal(goal_id):
    goal = Goal.get_or_none(Goal.id == goal_id)
    if goal is None:
        flash("Goal not found.", "error")
        return redirect(url_for("goals_planning"))

    goal.active = 0
    goal.save()

    flash("Goal archived.", "success")
    return redirect(url_for("goals_planning"))


@app.route("/goals/<int:goal_id>/restore", methods=["POST"])
def restore_goal(goal_id):
    goal = Goal.get_or_none(Goal.id == goal_id)
    if goal is None:
        flash("Goal not found.", "error")
        return redirect(url_for("goals_planning"))

    goal.active = 1
    goal.save()

    flash("Goal restored.", "success")
    return redirect(url_for("goals_planning"))


# Stats route
@app.route("/stats")
def stats_trends():
    range_key = normalize_stats_range((request.args.get("range") or "").strip())
    reference_date = today_date()
    state = build_budget_state()

    transactions = list(
        Transaction.select(Transaction, Envelope)
        .join(Envelope, JOIN.LEFT_OUTER)
        .order_by(Transaction.date.asc(), Transaction.id.asc())
    )
    transfers = list(
        Transfer.select().order_by(Transfer.date.asc(), Transfer.id.asc())
    )
    filtered_transactions = []
    for txn in transactions:
        txn_date = parse_iso_date(txn.date)
        if transaction_matches_stats_range(txn_date, range_key, reference_date):
            filtered_transactions.append(txn)

    filtered_expenses = [
        txn for txn in filtered_transactions if txn.type == "EXPENSE"
    ]
    filtered_income = [
        txn for txn in filtered_transactions if txn.type == "INCOME"
    ]

    spending_total = sum(float(txn.amount) for txn in filtered_expenses)
    income_total = sum(float(txn.amount) for txn in filtered_income)
    average_expense = 0.0
    if filtered_expenses:
        average_expense = spending_total / len(filtered_expenses)

    envelope_spending_rows = build_envelope_spending_rows(filtered_expenses)
    cash_flow_rows = build_cash_flow_rows(transactions, range_key, reference_date)
    negative_envelope_rows = build_negative_envelope_rows(
        transactions,
        transfers,
        range_key,
        reference_date,
    )
    recurring_deposit_state = state["recurring_deposit_state"]
    insights = build_stats_insights(
        filtered_expenses,
        envelope_spending_rows,
        negative_envelope_rows,
        recurring_deposit_state,
        range_key,
    )

    return render_template(
        "stats.html",
        page_title="Statistics & Trends",
        nav="stats",
        range_options=STATS_RANGE_OPTIONS,
        current_range=range_key,
        current_range_label=stats_range_label(range_key),
        period_totals=build_stats_period_totals(transactions, reference_date),
        summary={
            "selected_income": income_total,
            "selected_net": income_total - spending_total,
            "average_expense": average_expense,
            "transaction_count": len(filtered_transactions),
            "available_balance": state["available_balance"],
            "recurring_monthly_target": recurring_deposit_state["monthly_target_total"],
            "recurring_funded": recurring_deposit_state["funded_total"],
            "recurring_due": recurring_deposit_state["due_total"],
            "recurring_due_count": recurring_deposit_state["due_count"],
        },
        envelope_spending_rows=envelope_spending_rows,
        insights=insights,
        charts={
            "spending_by_envelope": {
                "labels": [row["name"] for row in envelope_spending_rows],
                "values": [round(row["amount"], 2) for row in envelope_spending_rows],
            },
            "spending_share": {
                "labels": [row["name"] for row in envelope_spending_rows],
                "values": [round(row["amount"], 2) for row in envelope_spending_rows],
            },
            "cash_flow": {
                "labels": [row["label"] for row in cash_flow_rows],
                "income": [round(row["income"], 2) for row in cash_flow_rows],
                "expense": [round(row["expense"], 2) for row in cash_flow_rows],
            },
        },
    )


if __name__ == "__main__":
    from werkzeug.serving import make_server

    host = "127.0.0.1"
    port = 5000
    server = make_server(host, port, app, threaded=True)

    def request_direct_shutdown():
        server.shutdown()

    app.config["CAN_CLOSE_APP"] = True
    app.config["REQUEST_APP_SHUTDOWN"] = request_direct_shutdown

    print(f"Brent's Budgeting App is running at http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if not db.is_closed():
            db.close()
