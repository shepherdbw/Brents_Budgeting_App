from peewee import (
    AutoField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    Model,
    SqliteDatabase,
    TextField,
)

from runtime_paths import DB_PATH

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db = SqliteDatabase(str(DB_PATH))


class BaseModel(Model):
    class Meta:
        database = db


class Setting(BaseModel):
    key = TextField(primary_key=True)
    value = TextField()

    class Meta:
        table_name = "settings"


class Envelope(BaseModel):
    id = AutoField()
    name = TextField(unique=True)
    active = IntegerField(default=1)
    recurring_monthly_amount = FloatField(default=0)
    created_at = TextField()

    class Meta:
        table_name = "envelopes"


class Subscription(BaseModel):
    id = AutoField()
    name = TextField()
    amount = FloatField()
    renewal_date = TextField()
    frequency = TextField(default="monthly")
    envelope = ForeignKeyField(
        Envelope,
        null=True,
        backref="subscriptions",
        column_name="envelope_id",
    )
    note = TextField(null=True)
    active = IntegerField(default=1)
    created_at = TextField()

    class Meta:
        table_name = "subscriptions"


class Transaction(BaseModel):
    id = AutoField()
    date = TextField()
    payee = TextField()
    envelope = ForeignKeyField(
        Envelope,
        null=True,
        backref="transactions",
        column_name="envelope_id",
    )
    amount = FloatField()
    type = TextField()
    note = TextField(null=True)
    created_at = TextField()

    class Meta:
        table_name = "transactions"


class Allocation(BaseModel):
    id = AutoField()
    date = TextField()
    envelope = ForeignKeyField(
        Envelope,
        backref="allocations",
        column_name="envelope_id",
    )
    amount = FloatField()
    created_at = TextField()

    class Meta:
        table_name = "allocations"


class Goal(BaseModel):
    id = AutoField()
    name = TextField()
    target_amount = FloatField()
    target_date = TextField(null=True)
    contribution_frequency = TextField(default="month")
    envelope = ForeignKeyField(
        Envelope,
        null=True,
        backref="goals",
        column_name="envelope_id",
    )
    created_at = TextField()
    active = IntegerField(default=1)

    class Meta:
        table_name = "goals"


class Transfer(BaseModel):
    id = AutoField()
    date = TextField()

    source_type = TextField()
    source_envelope = ForeignKeyField(
        Envelope,
        null=True,
        backref="outgoing_transfers",
        column_name="source_envelope_id",
    )
    source_goal = ForeignKeyField(
        Goal,
        null=True,
        backref="outgoing_transfers",
        column_name="source_goal_id",
    )

    destination_type = TextField()
    destination_envelope = ForeignKeyField(
        Envelope,
        null=True,
        backref="incoming_transfers",
        column_name="destination_envelope_id",
    )
    destination_goal = ForeignKeyField(
        Goal,
        null=True,
        backref="incoming_transfers",
        column_name="destination_goal_id",
    )

    amount = FloatField()
    created_at = TextField()

    class Meta:
        table_name = "transfers"
