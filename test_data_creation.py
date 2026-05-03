from datetime import date, timedelta
from models import db, Envelope, Transaction

def seed():
    db.connect(reuse_if_open=True)

    groceries, _ = Envelope.get_or_create(name="Groceries")
    rent, _ = Envelope.get_or_create(name="Rent")
    fun, _ = Envelope.get_or_create(name="Fun")

    today = date.today()

    Transaction.create(date=(today - timedelta(days=1)).isoformat(), payee="Paycheck", envelope=None, amount=1200.00, type="INCOME", note="January Payroll")
    Transaction.create(date=(today - timedelta(days=1)).isoformat(), payee="Bob Smith", envelope=rent, amount=800.00, type="EXPENSE", note="January Rent")
    Transaction.create(date=(today - timedelta(days=2)).isoformat(), payee="Kroger", envelope=groceries, amount=56.42, type="EXPENSE", note="")
    Transaction.create(date=(today - timedelta(days=3)).isoformat(), payee="AMC", envelope=fun, amount=18.00, type="EXPENSE", note="Movie")

    db.close()
    print("Seeded test data.")

if __name__ == "__main__":
    seed()