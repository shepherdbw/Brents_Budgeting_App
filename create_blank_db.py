import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Create a blank budget database with the current app schema."
    )
    parser.add_argument("output", help="Path to the SQLite database file to create.")
    args = parser.parse_args()

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    os.environ["BUDGET_APP_DB_PATH"] = str(output_path)

    import app  # noqa: F401

    if not output_path.exists():
        raise SystemExit(f"Failed to create database at {output_path}")

    print(output_path)


if __name__ == "__main__":
    main()
