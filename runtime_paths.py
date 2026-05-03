import os
import sys
from pathlib import Path


def _source_root():
    return Path(__file__).resolve().parent


def get_resource_root():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return _source_root()


def get_app_root():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _source_root()


APP_ROOT = get_app_root()
RESOURCE_ROOT = get_resource_root()

_db_path_override = os.environ.get("BUDGET_APP_DB_PATH")
DB_PATH = Path(_db_path_override).expanduser().resolve() if _db_path_override else (
    APP_ROOT / "budget.sqlite"
)

TEMPLATES_DIR = RESOURCE_ROOT / "templates"
STATIC_DIR = RESOURCE_ROOT / "static"
SCHEMA_PATH = RESOURCE_ROOT / "schema.sql"
