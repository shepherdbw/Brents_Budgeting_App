# Brent's Budgeting App

A local-first envelope budgeting app built with Flask, Peewee, SQLite, and Tailwind CSS.

## Features

- Track income, expenses, envelope balances, goals, subscriptions, and transfers.
- Import and preview CSV transaction data before committing it.
- View dashboard cards, budgeting history, subscription calendars, and trend charts.
- Run as a local Flask app or package as a Windows portable app.

## Run Locally

```powershell
python -m venv venv
.\venv\Scripts\python -m pip install -r requirements.txt
.\venv\Scripts\python app.py
```

The app uses `budget.sqlite` in the project folder by default. This repository copy includes a blank database with the current schema only.

## Build Portable App

```powershell
.\build_portable.ps1
```

The build script creates a local virtual environment if needed, installs dependencies, and outputs a portable Windows build outside this source folder.
