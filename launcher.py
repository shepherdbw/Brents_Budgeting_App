import ctypes
import hashlib
import json
import os
import socket
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox

from werkzeug.serving import make_server

from app import app
from models import db
from runtime_paths import APP_ROOT, DB_PATH


HOST = "127.0.0.1"
ERROR_ALREADY_EXISTS = 183
INSTANCE_INFO_PATH = APP_ROOT / "portable_instance.json"


def find_available_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.getsockname()[1]


def instance_mutex_name():
    app_root_hash = hashlib.sha1(str(APP_ROOT).lower().encode("utf-8")).hexdigest()
    return f"Local\\BrentsBudgetingAppPortable_{app_root_hash}"


class BudgetAppLauncher:
    def __init__(self):
        self.mutex_handle = None
        self.server = None
        self.server_thread = None
        self.port = None
        self.url = None
        self.is_shutting_down = False

        if not self.acquire_single_instance():
            self.open_existing_instance()
            raise SystemExit(0)

        self.root = tk.Tk()
        self.root.title("Brent's Budgeting App")
        self.root.geometry("460x260")
        self.root.minsize(460, 260)
        self.root.configure(bg="#f1f5f9")
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

        self.status_var = tk.StringVar(value="Starting local budget server...")
        self.url_var = tk.StringVar(value="")

        self._build_ui()

    def _build_ui(self):
        card = tk.Frame(self.root, bg="white", bd=1, relief="solid", padx=20, pady=20)
        card.pack(fill="both", expand=True, padx=18, pady=18)

        title = tk.Label(
            card,
            text="Brent's Budgeting App",
            font=("Segoe UI", 16, "bold"),
            bg="white",
            fg="#0f172a",
        )
        title.pack(anchor="w")

        subtitle = tk.Label(
            card,
            text="Portable launcher for the local budget app.",
            font=("Segoe UI", 10),
            bg="white",
            fg="#475569",
        )
        subtitle.pack(anchor="w", pady=(6, 16))

        status = tk.Label(
            card,
            textvariable=self.status_var,
            font=("Segoe UI", 10),
            bg="white",
            fg="#0f172a",
            wraplength=380,
            justify="left",
        )
        status.pack(anchor="w")

        url_label = tk.Label(
            card,
            textvariable=self.url_var,
            font=("Segoe UI", 9),
            bg="white",
            fg="#64748b",
            wraplength=380,
            justify="left",
        )
        url_label.pack(anchor="w", pady=(6, 16))

        info = tk.Label(
            card,
            text=f"Data folder: {APP_ROOT}\nDatabase: {DB_PATH.name}",
            font=("Segoe UI", 9),
            bg="white",
            fg="#475569",
            justify="left",
            wraplength=380,
        )
        info.pack(anchor="w")

        button_row = tk.Frame(card, bg="white")
        button_row.pack(fill="x", pady=(20, 0))

        open_button = tk.Button(
            button_row,
            text="Open App",
            command=self.open_browser,
            bg="#059669",
            fg="white",
            activebackground="#047857",
            activeforeground="white",
            relief="flat",
            padx=16,
            pady=8,
            cursor="hand2",
        )
        open_button.pack(side="left")

        folder_button = tk.Button(
            button_row,
            text="Open Data Folder",
            command=self.open_data_folder,
            bg="#e2e8f0",
            fg="#0f172a",
            activebackground="#cbd5e1",
            relief="flat",
            padx=16,
            pady=8,
            cursor="hand2",
        )
        folder_button.pack(side="left", padx=(10, 0))

    def acquire_single_instance(self):
        kernel32 = ctypes.windll.kernel32
        mutex_name = instance_mutex_name()
        self.mutex_handle = kernel32.CreateMutexW(None, False, mutex_name)
        if not self.mutex_handle:
            raise RuntimeError("Unable to create app instance mutex.")

        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = None
            return False

        return True

    def open_existing_instance(self):
        existing_url = None

        if INSTANCE_INFO_PATH.exists():
            try:
                instance_info = json.loads(INSTANCE_INFO_PATH.read_text(encoding="utf-8"))
                existing_url = instance_info.get("url")
            except (OSError, ValueError, json.JSONDecodeError):
                existing_url = None

        if existing_url:
            webbrowser.open(existing_url, new=1)
        else:
            messagebox.showinfo(
                "Brent's Budgeting App",
                "Brent's Budgeting App is already open. Please wait a moment and try again.",
            )

    def start_server(self):
        app.config["DEV_TOOLS"] = False
        app.config["CAN_CLEAR_DATA"] = True
        app.config["CAN_CLOSE_APP"] = True
        app.config["REQUEST_APP_SHUTDOWN"] = self.request_shutdown
        self.port = find_available_port()
        self.url = f"http://{HOST}:{self.port}"
        self.server = make_server(HOST, self.port, app, threaded=True)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            name="budget-app-server",
            daemon=True,
        )
        self.server_thread.start()
        INSTANCE_INFO_PATH.write_text(
            json.dumps({"url": self.url, "pid": os.getpid()}, indent=2),
            encoding="utf-8",
        )
        self.status_var.set("Budget server is running locally.")
        self.url_var.set(self.url)

    def open_browser(self):
        if self.url:
            webbrowser.open(self.url, new=1)

    def open_data_folder(self):
        os.startfile(str(APP_ROOT))

    def request_shutdown(self):
        if self.is_shutting_down:
            return

        self.root.after(0, self.shutdown)

    def shutdown(self):
        if self.is_shutting_down:
            return

        self.is_shutting_down = True
        self.status_var.set("Closing Brent's Budgeting App...")

        try:
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
                self.server = None

            if self.server_thread is not None and self.server_thread.is_alive():
                self.server_thread.join(timeout=3)

            if not db.is_closed():
                db.close()
            if INSTANCE_INFO_PATH.exists():
                INSTANCE_INFO_PATH.unlink()
            if self.mutex_handle is not None:
                ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
                self.mutex_handle = None
        finally:
            self.root.destroy()

    def run(self):
        try:
            self.start_server()
        except Exception as exc:
            messagebox.showerror(
                "Budget App Launcher",
                f"Unable to start the local budget server.\n\n{exc}",
            )
            raise SystemExit(1) from exc

        self.root.after(700, self.open_browser)
        self.root.mainloop()


if __name__ == "__main__":
    BudgetAppLauncher().run()
