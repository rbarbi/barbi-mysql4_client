"""Barbi Mysql4 Client - interface grafica para servidores MySQL 3.23 / 4.0 / 4.1."""
import base64
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont

import exporters
from exporters import sql_literal
from mysql4proto import CHARSETS, Connection, escape_identifier, split_statements

APP_NAME = "Barbi Mysql4 Client"
IS_WINDOWS = sys.platform == "win32"
EDITOR_FONT = ("Consolas", 11) if IS_WINDOWS else ("Monospace", 11)


# ====================================================================== config

def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    path = os.path.join(base, "MySQL4Client")
    os.makedirs(path, exist_ok=True)
    return path


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Criptografa/descriptografa com a DPAPI do Windows (vinculada ao usuario)."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def encode_secret(text):
    if not text:
        return ""
    raw = text.encode("utf-8")
    if IS_WINDOWS:
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode()
        except OSError:
            pass
    return "b64:" + base64.b64encode(raw).decode()


def decode_secret(value):
    if not value:
        return ""
    kind, _, data = value.partition(":")
    try:
        raw = base64.b64decode(data)
        if kind == "dpapi":
            raw = _dpapi(raw, False)
        return raw.decode("utf-8")
    except Exception:
        return ""


class ConnectionStore:
    def __init__(self):
        self.path = os.path.join(config_dir(), "conexoes.json")
        self.profiles = []
        self.last = ""
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.profiles = data.get("profiles", [])
            self.last = data.get("last", "")
        except (OSError, ValueError):
            pass

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"profiles": self.profiles, "last": self.last}, f, indent=2, ensure_ascii=False)

    def get(self, name):
        return next((p for p in self.profiles if p["name"] == name), None)

    def put(self, profile):
        existing = self.get(profile["name"])
        if existing:
            existing.update(profile)
        else:
            self.profiles.append(profile)
            self.profiles.sort(key=lambda p: p["name"].lower())
        self.save()

    def delete(self, name):
        self.profiles = [p for p in self.profiles if p["name"] != name]
        self.save()


# ====================================================================== threads

class DbWorker:
    """Executa todo acesso ao banco numa unica thread, sem travar a interface."""

    def __init__(self, root, on_error, on_busy):
        self.root = root
        self.on_error = on_error
        self.on_busy = on_busy
        self.conn = None
        self.params = None
        self.last_used = 0.0
        self.pending = 0
        self._jobs = queue.Queue()
        self._done = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()
        root.after(40, self._poll)

    def connect(self, params, on_ok, on_err=None):
        def job(_):
            self._close()
            self.conn = Connection(**params)
            self.params = params
            self.last_used = time.time()
            return self.conn
        self._submit(job, on_ok, on_err, raw=True)

    def disconnect(self, on_ok=None):
        def job(_):
            self._close()
            self.params = None
        self._submit(job, on_ok, None, raw=True)

    def run(self, fn, on_ok=None, on_err=None):
        """fn(conn) roda na thread do banco; on_ok(resultado) roda na interface."""
        self._submit(fn, on_ok, on_err, raw=False)

    def _submit(self, fn, on_ok, on_err, raw):
        self.pending += 1
        self.on_busy(self.pending)
        self._jobs.put((fn, on_ok, on_err, raw))

    def _close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def _ensure(self):
        if self.params is None:
            raise ConnectionError("Nao conectado. Use Conexao > Conectar.")
        if self.conn is not None and self.conn.connected and time.time() - self.last_used > 30:
            try:
                self.conn.ping()
            except Exception:
                self._close()
        if self.conn is None or not self.conn.connected:
            database = self.conn.database if self.conn else self.params.get("database", "")
            self._close()
            self.conn = Connection(**dict(self.params, database=database))
        return self.conn

    def _loop(self):
        while True:
            fn, on_ok, on_err, raw = self._jobs.get()
            try:
                result = fn(self.conn) if raw else fn(self._ensure())
                self.last_used = time.time()
                self._done.put((on_ok, result, None))
            except Exception as exc:
                self._done.put((on_err, None, exc))

    def _poll(self):
        try:
            while True:
                callback, result, exc = self._done.get_nowait()
                self.pending -= 1
                self.on_busy(self.pending)
                try:
                    if exc is not None:
                        (callback or self.on_error)(exc)
                    elif callback:
                        callback(result)
                except Exception:
                    traceback.print_exc()
                    messagebox.showerror(APP_NAME, traceback.format_exc())
        except queue.Empty:
            pass
        self.root.after(40, self._poll)


def run_in_thread(widget, fn, on_ok, on_err):
    """Roda fn() numa thread avulsa e entrega o resultado na thread da interface."""
    box = queue.Queue()

    def work():
        try:
            box.put((True, fn()))
        except Exception as exc:
            box.put((False, exc))

    def check():
        try:
            ok, value = box.get_nowait()
        except queue.Empty:
            widget.after(50, check)
            return
        (on_ok if ok else on_err)(value)

    threading.Thread(target=work, daemon=True).start()
    widget.after(50, check)


# ====================================================================== dialogos

def center_on(win, parent):
    win.update_idletasks()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    w, h = win.winfo_width(), win.winfo_height()
    win.geometry(f"+{max(px + (pw - w) // 2, 0)}+{max(py + (ph - h) // 3, 0)}")


def confirm_sql(parent, title, sql, note=""):
    """Mostra o SQL que sera executado e pede confirmacao. Retorna True/False."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.geometry("760x380")
    answer = {"ok": False}
    if note:
        ttk.Label(win, text=note, foreground="#b45309", wraplength=720,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 0))
    ttk.Label(win, text="SQL que sera executado:").pack(anchor="w", padx=10, pady=(8, 2))
    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=10)
    text = tk.Text(frame, font=EDITOR_FONT, wrap="none", height=10)
    sy = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    sx = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    text.grid(row=0, column=0, sticky="nsew")
    sy.grid(row=0, column=1, sticky="ns")
    sx.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    text.insert("1.0", sql)
    text.configure(state="disabled")
    buttons = ttk.Frame(win)
    buttons.pack(fill="x", padx=10, pady=10)

    def done(ok):
        answer["ok"] = ok
        win.destroy()

    ttk.Button(buttons, text="Cancelar", command=lambda: done(False)).pack(side="right")
    run_btn = ttk.Button(buttons, text="Executar", command=lambda: done(True))
    run_btn.pack(side="right", padx=6)
    win.bind("<Escape>", lambda e: done(False))
    center_on(win, parent)
    win.grab_set()
    run_btn.focus_set()
    parent.wait_window(win)
    return answer["ok"]


class ConnectionDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.store = app.store
        self.title("Conexoes")
        self.transient(app.root)
        self.resizable(False, False)

        left = ttk.Frame(self, padding=10)
        left.grid(row=0, column=0, sticky="ns")
        ttk.Label(left, text="Conexoes salvas").pack(anchor="w")
        self.listbox = tk.Listbox(left, width=26, height=14, exportselection=False)
        self.listbox.pack(fill="y", expand=True, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Double-1>", lambda e: self._connect())
        lb_buttons = ttk.Frame(left)
        lb_buttons.pack(fill="x")
        ttk.Button(lb_buttons, text="Nova", command=self._new).pack(side="left")
        ttk.Button(lb_buttons, text="Excluir", command=self._delete).pack(side="left", padx=4)

        form = ttk.Frame(self, padding=10)
        form.grid(row=0, column=1, sticky="nsew")
        self.vars = {
            "name": tk.StringVar(), "host": tk.StringVar(value="localhost"),
            "port": tk.StringVar(value="3306"), "user": tk.StringVar(value="root"),
            "password": tk.StringVar(), "database": tk.StringVar(),
            "charset": tk.StringVar(value="latin1"), "timeout": tk.StringVar(value="10"),
        }
        self.save_password = tk.BooleanVar(value=True)
        fields = [("Nome da conexao", "name"), ("Host / IP", "host"), ("Porta", "port"),
                  ("Usuario", "user"), ("Senha", "password"), ("Banco padrao (opcional)", "database"),
                  ("Charset", "charset"), ("Timeout (s)", "timeout")]
        for row, (label, key) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            if key == "charset":
                w = ttk.Combobox(form, textvariable=self.vars[key], values=list(CHARSETS),
                                 state="readonly", width=12)
            else:
                w = ttk.Entry(form, textvariable=self.vars[key], width=34,
                              show="*" if key == "password" else "")
            w.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=3)
            if key == "name":
                self.name_entry = w
        ttk.Checkbutton(form, text="Salvar senha (criptografada para seu usuario do Windows)",
                        variable=self.save_password).grid(row=len(fields), column=0, columnspan=2,
                                                          sticky="w", pady=(6, 0))
        ttk.Label(form, foreground="#666", wraplength=330,
                  text="Dica: dados antigos do MySQL 4 normalmente estao em latin1. "
                       "Se a acentuacao aparecer errada, troque o charset.").grid(
            row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.status = ttk.Label(buttons, text="")
        self.status.pack(side="left")
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Conectar", command=self._connect).pack(side="right", padx=4)
        ttk.Button(buttons, text="Salvar", command=self._save).pack(side="right")
        ttk.Button(buttons, text="Testar", command=self._test).pack(side="right", padx=4)

        self.bind("<Return>", lambda e: self._connect())
        self.bind("<Escape>", lambda e: self.destroy())
        self._refresh_list(self.store.last)
        if not self.store.profiles:
            self._new()
        center_on(self, app.root)
        self.grab_set()

    def _refresh_list(self, select=None):
        self.listbox.delete(0, "end")
        for i, p in enumerate(self.store.profiles):
            self.listbox.insert("end", p["name"])
            if p["name"] == select:
                self.listbox.selection_set(i)
                self.listbox.see(i)
                self._load(p)

    def _on_select(self, _event=None):
        sel = self.listbox.curselection()
        if sel:
            self._load(self.store.profiles[sel[0]])

    def _load(self, p):
        for key, var in self.vars.items():
            if key == "password":
                var.set(decode_secret(p.get("password", "")))
            else:
                var.set(str(p.get(key, var.get() if key in ("charset", "timeout") else "")))
        self.save_password.set(bool(p.get("password")))

    def _new(self):
        self.listbox.selection_clear(0, "end")
        for key, default in (("name", ""), ("host", "localhost"), ("port", "3306"), ("user", "root"),
                             ("password", ""), ("database", ""), ("charset", "latin1"), ("timeout", "10")):
            self.vars[key].set(default)
        self.name_entry.focus_set()

    def _profile(self):
        p = {k: v.get().strip() for k, v in self.vars.items() if k != "password"}
        if not p["name"]:
            p["name"] = f"{p['user']}@{p['host']}"
            self.vars["name"].set(p["name"])
        p["password"] = encode_secret(self.vars["password"].get()) if self.save_password.get() else ""
        return p

    def _params(self):
        try:
            port = int(self.vars["port"].get())
            timeout = float(self.vars["timeout"].get() or 10)
        except ValueError:
            raise ValueError("Porta e timeout devem ser numeros")
        return {
            "host": self.vars["host"].get().strip(), "port": port,
            "user": self.vars["user"].get(), "password": self.vars["password"].get(),
            "database": self.vars["database"].get().strip(),
            "charset": self.vars["charset"].get(), "connect_timeout": timeout,
        }

    def _save(self):
        p = self._profile()
        self.store.put(p)
        self._refresh_list(p["name"])
        self.status.configure(text="Conexao salva.", foreground="#15803d")

    def _delete(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        name = self.store.profiles[sel[0]]["name"]
        if messagebox.askyesno(APP_NAME, f"Excluir a conexao '{name}'?", parent=self):
            self.store.delete(name)
            self._refresh_list()
            self._new()

    def _test(self):
        try:
            params = self._params()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
            return
        self.status.configure(text="Testando...", foreground="#444")

        def work():
            conn = Connection(**params)
            version = conn.server_version
            conn.close()
            return version

        def ok(version):
            self.status.configure(text=f"OK - MySQL {version}", foreground="#15803d")

        def fail(exc):
            self.status.configure(text="Falhou", foreground="#b91c1c")
            messagebox.showerror(APP_NAME, f"Falha na conexao:\n{exc}", parent=self)

        run_in_thread(self, work, ok, fail)

    def _connect(self):
        try:
            params = self._params()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
            return
        p = self._profile()
        if self.store.get(p["name"]) or not self.store.profiles:
            self.store.put(p)
        self.store.last = p["name"]
        self.store.save()
        self.destroy()
        self.app.connect(params, p["name"])


class RowDialog(tk.Toplevel):
    """Formulario para editar/inserir um registro."""

    TEXT_TYPES = ("text", "tinytext", "mediumtext", "longtext", "blob", "tinyblob",
                  "mediumblob", "longblob")

    def __init__(self, parent, title, meta, values=None):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.result = None
        self.meta = meta
        self.values = values
        self.inserting = values is None
        self.fields = []

        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        sy = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        form = ttk.Frame(canvas, padding=10)
        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.configure(yscrollcommand=sy.set)
        canvas.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        ttk.Label(form, text="Coluna", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(form, text="Valor", font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")
        for i, col in enumerate(meta):
            name, ctype, nullable, key, default, extra = (list(col) + [""] * 6)[:6]
            row = i + 1
            label = name + (" (PK)" if key == "PRI" else "")
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="nw", pady=2, padx=(0, 8))
            cell = ttk.Frame(form)
            cell.grid(row=row, column=1, sticky="w", pady=2)
            base_type = ctype.split("(")[0].lower()
            if base_type in self.TEXT_TYPES:
                widget = tk.Text(cell, width=60, height=4, font=EDITOR_FONT, wrap="word")
            else:
                widget = ttk.Entry(cell, width=60, font=EDITOR_FONT)
            widget.pack(side="left")
            is_null = tk.BooleanVar(value=False)
            use_default = tk.BooleanVar(value=self.inserting)
            original = None if self.inserting else values[i]
            if original is None and not self.inserting:
                is_null.set(True)
            elif original is not None:
                self._set(widget, original)
            opts = ttk.Frame(cell)
            opts.pack(side="left", padx=6)
            ttk.Checkbutton(opts, text="NULL", variable=is_null,
                            command=lambda v=use_default: v.set(False)).pack(anchor="w")
            if self.inserting:
                ttk.Checkbutton(opts, text="DEFAULT", variable=use_default).pack(anchor="w")
                widget.bind("<Key>", lambda e, v=use_default: v.set(False), add="+")
            info = ctype + (f"  {extra}" if extra else "") + ("" if nullable == "YES" else "  NOT NULL")
            ttk.Label(form, text=info, foreground="#666").grid(row=row, column=2, sticky="nw", pady=2)
            self.fields.append((name, widget, is_null, use_default, original))

        buttons = ttk.Frame(self, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Salvar", command=self._ok).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())

        self.update_idletasks()
        w = min(form.winfo_reqwidth() + 30, self.winfo_screenwidth() - 100)
        h = min(form.winfo_reqheight() + 70, self.winfo_screenheight() - 150)
        self.geometry(f"{w}x{h}")
        center_on(self, parent)
        self.grab_set()

    @staticmethod
    def _set(widget, value):
        if isinstance(widget, tk.Text):
            widget.insert("1.0", value)
        else:
            widget.insert(0, value)

    @staticmethod
    def _get(widget):
        if isinstance(widget, tk.Text):
            return widget.get("1.0", "end-1c")
        return widget.get()

    def _ok(self):
        changes = {}
        for name, widget, is_null, use_default, original in self.fields:
            if self.inserting and use_default.get():
                continue
            value = None if is_null.get() else self._get(widget)
            if self.inserting or value != original:
                changes[name] = value
        self.result = changes
        self.destroy()


# ====================================================================== grade

def display_value(v):
    if v is None:
        return "(NULL)"
    if len(v) > 300:
        v = v[:300] + "..."
    return v.replace("\r", "").replace("\n", " ↵ ").replace("\t", " ")


class ResultGrid(ttk.Frame):
    """Grade de resultados com copia e exportacao."""

    def __init__(self, master, app, export_name="resultado", on_double_click=None):
        super().__init__(master)
        self.app = app
        self.export_name = export_name
        self.columns = []
        self.rows = []
        self._ctx_col = 0
        self.tree = ttk.Treeview(self, show="headings", selectmode="extended")
        sy = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        sx = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree.tag_configure("odd", background="#f5f7fa")

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Copiar celula", command=self.copy_cell)
        self.menu.add_command(label="Copiar linha(s)", command=lambda: self.copy_rows(False))
        self.menu.add_command(label="Copiar linha(s) com cabecalho", command=lambda: self.copy_rows(True))
        self.menu.add_separator()
        self.menu.add_command(label="Exportar resultado...", command=self.export)
        self.extra_menu_index = self.menu.index("end")
        self.tree.bind("<Button-3>", self._popup)
        self.tree.bind("<Control-c>", lambda e: self.copy_rows(False))
        self.tree.bind("<Control-a>", lambda e: self.tree.selection_set(self.tree.get_children()))
        if on_double_click:
            self.tree.bind("<Double-1>", lambda e: self.tree.identify_row(e.y) and on_double_click())

    def set_result(self, columns, rows):
        self.columns = columns
        self.rows = rows
        tree = self.tree
        tree.delete(*tree.get_children())
        ids = [f"c{i}" for i in range(len(columns))]
        tree.configure(columns=ids)
        measure = tkfont.nametofont("TkDefaultFont").measure
        sample = rows[:200]
        for i, col in enumerate(columns):
            width = measure(col.name) + 24
            for row in sample:
                width = max(width, measure(display_value(row[i])[:60]) + 16)
            tree.heading(ids[i], text=col.name, anchor="w")
            tree.column(ids[i], width=min(max(width, 50), 420), minwidth=40, stretch=False,
                        anchor="e" if col.is_numeric else "w")
        for idx, row in enumerate(rows):
            tree.insert("", "end", iid=str(idx), values=[display_value(v) for v in row],
                        tags=("odd",) if idx % 2 else ())

    def selected_indices(self):
        return sorted(int(i) for i in self.tree.selection())

    def _popup(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        if col:
            self._ctx_col = max(int(col[1:]) - 1, 0)
        self.menu.tk_popup(event.x_root, event.y_root)

    def _clip(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def copy_cell(self):
        sel = self.selected_indices()
        if sel and self._ctx_col < len(self.columns):
            v = self.rows[sel[0]][self._ctx_col]
            self._clip("" if v is None else v)

    def copy_rows(self, header):
        lines = []
        if header:
            lines.append("\t".join(c.name for c in self.columns))
        for i in self.selected_indices():
            lines.append("\t".join("" if v is None else v for v in self.rows[i]))
        self._clip("\r\n".join(lines))

    def export(self):
        self.app.export_rows(self.columns, self.rows, self.export_name)


# ====================================================================== abas

SQL_KEYWORDS = re.compile(
    r"\b(SELECT|FROM|WHERE|AND|OR|NOT|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|ALTER|"
    r"DROP|INDEX|JOIN|LEFT|RIGHT|INNER|OUTER|ON|AS|ORDER|BY|GROUP|HAVING|LIMIT|UNION|ALL|"
    r"DISTINCT|IS|NULL|LIKE|IN|BETWEEN|SHOW|DESCRIBE|DESC|ASC|USE|DATABASES|TABLES|COLUMNS|"
    r"CASE|WHEN|THEN|ELSE|END|EXISTS|PRIMARY|KEY|DEFAULT|REPLACE|TRUNCATE|EXPLAIN|STATUS|"
    r"PROCESSLIST|COUNT|SUM|AVG|MIN|MAX|IF|IFNULL|CONCAT|NOW|LOCK|UNLOCK|TABLES|CREATE)\b",
    re.IGNORECASE)
SQL_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
SQL_COMMENTS = re.compile(r"--[ \t][^\n]*|#[^\n]*|/\*.*?\*/", re.DOTALL)


class QueryTab(ttk.Frame):
    def __init__(self, master, app, database=""):
        super().__init__(master)
        self.app = app
        self.file_path = None
        self._hl_job = None

        bar = ttk.Frame(self, padding=(4, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="▶ Executar (F5)", command=self.execute).pack(side="left")
        ttk.Label(bar, text="  Banco:").pack(side="left")
        self.db_var = tk.StringVar(value=database)
        self.db_combo = ttk.Combobox(bar, textvariable=self.db_var, width=24, state="readonly",
                                     values=app.databases, postcommand=self._update_dbs)
        self.db_combo.pack(side="left", padx=4)
        ttk.Label(bar, text="  Max. linhas:").pack(side="left")
        self.limit_var = tk.StringVar(value="5000")
        ttk.Spinbox(bar, from_=0, to=10_000_000, increment=1000, width=9,
                    textvariable=self.limit_var).pack(side="left", padx=4)
        ttk.Label(bar, text="(0 = sem limite)", foreground="#666").pack(side="left")
        ttk.Button(bar, text="Exportar resultado", command=self.export_current).pack(side="right")
        ttk.Button(bar, text="Salvar .sql", command=self.save_file).pack(side="right", padx=4)
        ttk.Button(bar, text="Abrir .sql", command=self.open_file).pack(side="right")

        paned = ttk.Panedwindow(self, orient="vertical")
        paned.pack(fill="both", expand=True)
        editor_frame = ttk.Frame(paned)
        self.editor = tk.Text(editor_frame, font=EDITOR_FONT, undo=True, wrap="none", height=10)
        sy = ttk.Scrollbar(editor_frame, orient="vertical", command=self.editor.yview)
        self.editor.configure(yscrollcommand=sy.set)
        self.editor.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.editor.tag_configure("kw", foreground="#1d4ed8")
        self.editor.tag_configure("str", foreground="#15803d")
        self.editor.tag_configure("comment", foreground="#888888")
        for seq in ("<F5>", "<Control-Return>"):
            self.editor.bind(seq, lambda e: (self.execute(), "break")[1])
        self.editor.bind("<Control-a>", lambda e: (self.editor.tag_add("sel", "1.0", "end"), "break")[1])
        self.editor.bind("<KeyRelease>", self._schedule_highlight)
        paned.add(editor_frame, weight=1)

        self.results = ttk.Notebook(paned)
        paned.add(self.results, weight=3)
        self.messages = tk.Text(self.results, font=EDITOR_FONT, height=6, wrap="word")
        self.messages.tag_configure("error", foreground="#b91c1c")
        self.messages.tag_configure("ok", foreground="#15803d")
        self.results.add(self.messages, text="Mensagens")
        self.grids = []

        self.status = ttk.Label(self, text="Ctrl+Enter ou F5 executa o texto selecionado (ou tudo).",
                                foreground="#555", padding=(4, 2))
        self.status.pack(fill="x")

    def _update_dbs(self):
        self.db_combo.configure(values=self.app.databases)

    def set_sql(self, sql):
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", sql)
        self.highlight()

    def _schedule_highlight(self, _event=None):
        if self._hl_job:
            self.after_cancel(self._hl_job)
        self._hl_job = self.after(300, self.highlight)

    def highlight(self):
        self._hl_job = None
        text = self.editor.get("1.0", "end-1c")
        for tag in ("kw", "str", "comment"):
            self.editor.tag_remove(tag, "1.0", "end")
        if len(text) > 200_000:
            return
        for tag, pattern in (("kw", SQL_KEYWORDS), ("str", SQL_STRINGS), ("comment", SQL_COMMENTS)):
            for m in pattern.finditer(text):
                self.editor.tag_add(tag, f"1.0+{m.start()}c", f"1.0+{m.end()}c")

    def execute(self):
        try:
            sql = self.editor.get("sel.first", "sel.last")
        except tk.TclError:
            sql = self.editor.get("1.0", "end-1c")
        statements = split_statements(sql)
        if not statements:
            return
        try:
            limit = int(self.limit_var.get() or 0)
        except ValueError:
            limit = 5000
        database = self.db_var.get()
        self.status.configure(text=f"Executando {len(statements)} instrucao(oes)...")

        def job(conn):
            if database and conn.database != database:
                conn.select_db(database)
            results = []
            for stmt in statements:
                try:
                    results.append(conn.query(stmt, max_rows=limit or None))
                except Exception as exc:
                    return results, exc, stmt
            return results, None, None

        def done(value):
            results, error, failed_stmt = value
            self.show_results(results, error, failed_stmt)

        def fail(exc):
            self.show_results([], exc, statements[0])

        self.app.worker.run(job, done, fail)

    def show_results(self, results, error, failed_stmt):
        for grid in self.grids:
            self.results.forget(grid)
            grid.destroy()
        self.grids = []
        self.messages.delete("1.0", "end")
        total_time = 0.0
        for n, res in enumerate(results, start=1):
            total_time += res.elapsed
            first_line = res.sql.strip().splitlines()[0][:100]
            if res.has_rows:
                grid = ResultGrid(self.results, self.app, export_name=f"resultado{n}")
                grid.set_result(res.columns, res.rows)
                self.results.add(grid, text=f"Resultado {n} ({len(res.rows)})")
                self.grids.append(grid)
                info = f"{len(res.rows)} linha(s)"
            else:
                info = f"{res.affected_rows} linha(s) afetada(s)"
                if res.insert_id:
                    info += f", ultimo id inserido: {res.insert_id}"
            if res.message:
                info += f" - {res.message}"
            self.messages.insert("end", f"[{n}] {first_line}\n    OK: {info} ({res.elapsed:.3f}s)\n", "ok")
        if error is not None:
            self.messages.insert("end", f"\nERRO em:\n{failed_stmt}\n\n{error}\n", "error")
            self.results.select(self.messages)
            self.status.configure(text=f"Erro: {error}", foreground="#b91c1c")
        else:
            if self.grids:
                self.results.select(self.grids[-1])
            else:
                self.results.select(self.messages)
            self.status.configure(text=f"{len(results)} instrucao(oes) executada(s) em {total_time:.3f}s",
                                  foreground="#555")
        if any(not r.has_rows for r in results):
            self.app.refresh_databases(quiet=True)

    def export_current(self):
        current = self.results.select()
        grid = next((g for g in self.grids if str(g) == current), None)
        if grid is None and self.grids:
            grid = self.grids[-1]
        if grid is None:
            messagebox.showinfo(APP_NAME, "Nenhum resultado para exportar.")
            return
        grid.export()

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("SQL", "*.sql"), ("Todos", "*.*")])
        if not path:
            return
        with open(path, "rb") as f:
            raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", "replace")
        self.set_sql(text)
        self.file_path = path
        self.app.rename_tab(self, os.path.basename(path))

    def save_file(self):
        path = self.file_path or filedialog.asksaveasfilename(
            defaultextension=".sql", filetypes=[("SQL", "*.sql"), ("Todos", "*.*")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.editor.get("1.0", "end-1c"))
        self.file_path = path
        self.app.rename_tab(self, os.path.basename(path))
        self.status.configure(text=f"Salvo em {path}", foreground="#555")


class TableTab(ttk.Frame):
    """Navegacao numa tabela com filtro, paginacao e edicao de registros."""

    BLOB_OR_FLOAT = re.compile(r"^(tiny|medium|long)?(text|blob)|^(float|double|real)", re.I)

    def __init__(self, master, app, database, table):
        super().__init__(master)
        self.app = app
        self.database = database
        self.table = table
        self.qualified = f"{escape_identifier(database)}.{escape_identifier(table)}"
        self.meta = None
        self.page = 0
        self.total = 0
        self.applied_where = ""
        self.applied_order = ""

        top = ttk.Frame(self, padding=(4, 4))
        top.pack(fill="x")
        ttk.Label(top, text=f"{database}.{table}", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Label(top, text="   WHERE").pack(side="left")
        self.where_var = tk.StringVar()
        where_entry = ttk.Entry(top, textvariable=self.where_var, font=EDITOR_FONT, width=50)
        where_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(top, text="ORDER BY").pack(side="left")
        self.order_var = tk.StringVar()
        order_entry = ttk.Entry(top, textvariable=self.order_var, font=EDITOR_FONT, width=20)
        order_entry.pack(side="left", padx=4)
        ttk.Button(top, text="Filtrar", command=self.apply_filter).pack(side="left")
        for w in (where_entry, order_entry):
            w.bind("<Return>", lambda e: self.apply_filter())

        nav = ttk.Frame(self, padding=(4, 0, 4, 4))
        nav.pack(fill="x")
        ttk.Button(nav, text="|<", width=3, command=lambda: self.goto(0)).pack(side="left")
        ttk.Button(nav, text="<", width=3, command=lambda: self.goto(self.page - 1)).pack(side="left")
        self.page_label = ttk.Label(nav, text="", width=26, anchor="center")
        self.page_label.pack(side="left")
        ttk.Button(nav, text=">", width=3, command=lambda: self.goto(self.page + 1)).pack(side="left")
        ttk.Button(nav, text=">|", width=3, command=lambda: self.goto(self.last_page())).pack(side="left")
        ttk.Label(nav, text="  Linhas/pagina:").pack(side="left")
        self.page_size_var = tk.StringVar(value="200")
        size = ttk.Combobox(nav, textvariable=self.page_size_var, width=6,
                            values=["50", "100", "200", "500", "1000", "5000"])
        size.pack(side="left", padx=4)
        size.bind("<<ComboboxSelected>>", lambda e: self.goto(0))

        export_btn = ttk.Menubutton(nav, text="Exportar")
        export_menu = tk.Menu(export_btn, tearoff=0)
        export_menu.add_command(label="Pagina atual...", command=lambda: self.grid.export())
        export_menu.add_command(label="Todos os registros do filtro...", command=self.export_all)
        export_btn["menu"] = export_menu
        export_btn.pack(side="right")
        ttk.Button(nav, text="Estrutura", command=self.show_structure).pack(side="right", padx=4)
        ttk.Button(nav, text="Atualizar filtrados...", command=self.bulk_update).pack(side="right")
        ttk.Button(nav, text="Excluir", command=self.delete_rows).pack(side="right", padx=4)
        ttk.Button(nav, text="Inserir", command=self.insert_row).pack(side="right")
        ttk.Button(nav, text="Editar", command=self.edit_row).pack(side="right", padx=4)

        self.grid = ResultGrid(self, app, export_name=table, on_double_click=self.edit_row)
        self.grid.pack(fill="both", expand=True)
        self.grid.menu.add_separator()
        self.grid.menu.add_command(label="Editar registro...", command=self.edit_row)
        self.grid.menu.add_command(label="Excluir registro(s)...", command=self.delete_rows)
        self.grid.menu.add_command(label="Filtrar por este valor", command=self.filter_by_cell)

        self.status = ttk.Label(self, text="", foreground="#555", padding=(4, 2))
        self.status.pack(fill="x")
        self.bind_all_keys()
        self.load()

    def bind_all_keys(self):
        self.grid.tree.bind("<F5>", lambda e: self.load())
        self.grid.tree.bind("<Delete>", lambda e: self.delete_rows())
        self.grid.tree.bind("<Return>", lambda e: self.edit_row())

    # --- consulta

    def page_size(self):
        try:
            return max(int(self.page_size_var.get()), 1)
        except ValueError:
            return 200

    def last_page(self):
        return max((self.total - 1) // self.page_size(), 0)

    def _base_sql(self, where=None):
        where = self.applied_where if where is None else where
        return f"FROM {self.qualified}" + (f" WHERE {where}" if where else "")

    def _select_sql(self, with_limit=True):
        sql = f"SELECT * {self._base_sql()}"
        if self.applied_order:
            sql += f" ORDER BY {self.applied_order}"
        if with_limit:
            sql += f" LIMIT {self.page * self.page_size()}, {self.page_size()}"
        return sql

    def apply_filter(self):
        where = self.where_var.get().strip()
        where = re.sub(r"^\s*where\s+", "", where, flags=re.I)
        order = re.sub(r"^\s*order\s+by\s+", "", self.order_var.get().strip(), flags=re.I)
        self.applied_where, self.applied_order = where, order
        self.page = 0
        self.load()

    def goto(self, page):
        self.page = min(max(page, 0), self.last_page())
        self.load()

    def load(self):
        self.status.configure(text="Carregando...", foreground="#555")
        need_meta = self.meta is None
        base, select = self._base_sql(), self._select_sql()
        show_columns = f"SHOW COLUMNS FROM {escape_identifier(self.table)} FROM {escape_identifier(self.database)}"

        def job(conn):
            meta = conn.query(show_columns).rows if need_meta else self.meta
            total = int(conn.query(f"SELECT COUNT(*) {base}").rows[0][0])
            return meta, total, conn.query(select)

        def done(value):
            self.meta, self.total, res = value
            self.grid.set_result(res.columns, res.rows)
            pages = self.last_page() + 1
            self.page_label.configure(text=f"Pagina {self.page + 1} de {pages}")
            self.status.configure(text=f"{self.total} registro(s) no filtro - {select}  ({res.elapsed:.3f}s)",
                                  foreground="#555")
            if not self.pk_columns():
                self.status.configure(text=self.status.cget("text") +
                                      "   |  ATENCAO: tabela sem chave primaria")

        def fail(exc):
            self.status.configure(text=f"Erro: {exc}", foreground="#b91c1c")
            messagebox.showerror(APP_NAME, f"Erro ao consultar:\n{exc}")

        self.app.worker.run(job, done, fail)

    # --- edicao

    def pk_columns(self):
        return [m[0] for m in (self.meta or []) if m[3] == "PRI"]

    def _meta_by_name(self):
        return {m[0]: m for m in self.meta or []}

    def _row_where(self, row):
        names = [c.name for c in self.grid.columns]
        keys = self.pk_columns()
        if not keys:
            meta = self._meta_by_name()
            keys = [n for n in names if n in meta and not self.BLOB_OR_FLOAT.match(meta[n][1])]
        parts = []
        for key in keys:
            value = row[names.index(key)]
            ident = escape_identifier(key)
            parts.append(f"{ident} IS NULL" if value is None else f"{ident} = {sql_literal(value)}")
        return " AND ".join(parts)

    def _ensure_editable(self):
        if self.meta is None:
            return False
        names = [c.name for c in self.grid.columns]
        if [m[0] for m in self.meta] != names:
            messagebox.showerror(APP_NAME, "As colunas exibidas nao conferem com a estrutura da tabela. "
                                           "Recarregue a aba.")
            return False
        return True

    def _no_pk_note(self):
        if self.pk_columns():
            return ""
        return ("Tabela SEM chave primaria: o registro sera localizado comparando todas as colunas "
                "(exceto texto/blob/float) e limitado a 1 linha.")

    def _execute(self, statements, success_text):
        def job(conn):
            return sum(conn.query(s).affected_rows for s in statements)

        def done(affected):
            self.status.configure(text=f"{success_text}: {affected} linha(s) afetada(s)", foreground="#15803d")
            if affected == 0:
                messagebox.showwarning(APP_NAME, "Nenhuma linha foi afetada (valores iguais ou "
                                                 "registro nao encontrado).")
            self.load()

        def fail(exc):
            messagebox.showerror(APP_NAME, f"Erro:\n{exc}")
            self.load()

        self.app.worker.run(job, done, fail)

    def edit_row(self):
        sel = self.grid.selected_indices()
        if not sel or not self._ensure_editable():
            return
        row = self.grid.rows[sel[0]]
        dlg = RowDialog(self.app.root, f"Editar registro - {self.table}", self.meta, row)
        self.app.root.wait_window(dlg)
        if not dlg.result:
            return
        sets = ", ".join(f"{escape_identifier(k)} = {sql_literal(v)}" for k, v in dlg.result.items())
        sql = f"UPDATE {self.qualified} SET {sets} WHERE {self._row_where(row)} LIMIT 1"
        if confirm_sql(self.app.root, "Confirmar alteracao", sql, self._no_pk_note()):
            self._execute([sql], "Registro alterado")

    def insert_row(self):
        if not self._ensure_editable():
            return
        dlg = RowDialog(self.app.root, f"Inserir registro - {self.table}", self.meta)
        self.app.root.wait_window(dlg)
        if dlg.result is None:
            return
        cols = ", ".join(escape_identifier(k) for k in dlg.result)
        values = ", ".join(sql_literal(v) for v in dlg.result.values())
        sql = f"INSERT INTO {self.qualified} ({cols}) VALUES ({values})"
        if confirm_sql(self.app.root, "Confirmar insercao", sql):
            self._execute([sql], "Registro inserido")

    def delete_rows(self):
        sel = self.grid.selected_indices()
        if not sel or not self._ensure_editable():
            return
        statements = [f"DELETE FROM {self.qualified} WHERE {self._row_where(self.grid.rows[i])} LIMIT 1"
                      for i in sel]
        note = f"Serao excluidos {len(statements)} registro(s). {self._no_pk_note()}"
        if confirm_sql(self.app.root, "Confirmar exclusao", ";\n".join(statements) + ";", note):
            self._execute(statements, "Exclusao concluida")

    def filter_by_cell(self):
        sel = self.grid.selected_indices()
        col = self.grid._ctx_col
        if not sel or col >= len(self.grid.columns):
            return
        value = self.grid.rows[sel[0]][col]
        ident = escape_identifier(self.grid.columns[col].name)
        cond = f"{ident} IS NULL" if value is None else f"{ident} = {sql_literal(value)}"
        current = self.where_var.get().strip()
        self.where_var.set(f"{current} AND {cond}" if current else cond)
        self.apply_filter()

    def bulk_update(self):
        if self.meta is None:
            return
        BulkUpdateDialog(self)

    # --- outros

    def export_all(self):
        sql = self._select_sql(with_limit=False)
        self.status.configure(text="Lendo todos os registros do filtro...")

        def done(res):
            self.status.configure(text=f"{len(res.rows)} registro(s) lidos para exportacao", foreground="#555")
            self.app.export_rows(res.columns, res.rows, self.table)

        self.app.worker.run(lambda conn: conn.query(sql), done)

    def show_structure(self):
        tab = self.app.new_query_tab(self.database, title=f"Estrutura {self.table}")
        tab.set_sql(f"SHOW COLUMNS FROM {escape_identifier(self.table)};\n"
                    f"SHOW INDEX FROM {escape_identifier(self.table)};\n"
                    f"SHOW CREATE TABLE {escape_identifier(self.table)};\n")
        tab.execute()


class BulkUpdateDialog(tk.Toplevel):
    """UPDATE de uma coluna em todos os registros que atendem ao filtro aplicado."""

    def __init__(self, tab: TableTab):
        super().__init__(tab.app.root)
        self.tab = tab
        self.title(f"Atualizar registros filtrados - {tab.table}")
        self.transient(tab.app.root)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        where = tab.applied_where or "(sem filtro - TODOS os registros da tabela)"
        ttk.Label(frame, text="Filtro aplicado (WHERE):").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text=where, font=EDITOR_FONT, wraplength=520,
                  foreground="#b91c1c" if not tab.applied_where else "#000").grid(
            row=0, column=1, sticky="w", padx=8)
        ttk.Label(frame, text="Coluna:").grid(row=1, column=0, sticky="w", pady=(10, 2))
        self.column = tk.StringVar()
        names = [m[0] for m in tab.meta]
        ttk.Combobox(frame, textvariable=self.column, values=names, state="readonly",
                     width=30).grid(row=1, column=1, sticky="w", padx=8, pady=(10, 2))
        ttk.Label(frame, text="Novo valor:").grid(row=2, column=0, sticky="w", pady=2)
        self.value = ttk.Entry(frame, width=60, font=EDITOR_FONT)
        self.value.grid(row=2, column=1, sticky="w", padx=8, pady=2)
        self.is_null = tk.BooleanVar()
        self.is_expr = tk.BooleanVar()
        ttk.Checkbutton(frame, text="Definir como NULL", variable=self.is_null).grid(
            row=3, column=1, sticky="w", padx=8)
        ttk.Checkbutton(frame, text="Valor e uma expressao SQL (ex.: preco * 1.1, NOW(), "
                                    "CONCAT(nome, ' X'))", variable=self.is_expr).grid(
            row=4, column=1, sticky="w", padx=8)
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Pre-visualizar...", command=self._preview).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        center_on(self, tab.app.root)
        self.grab_set()

    def _preview(self):
        column = self.column.get()
        if not column:
            messagebox.showwarning(APP_NAME, "Escolha a coluna.", parent=self)
            return
        if self.is_null.get():
            value = "NULL"
        elif self.is_expr.get():
            value = self.value.get()
        else:
            value = sql_literal(self.value.get())
        tab = self.tab
        sql = f"UPDATE {tab.qualified} SET {escape_identifier(column)} = {value}"
        if tab.applied_where:
            sql += f" WHERE {tab.applied_where}"
        count_sql = f"SELECT COUNT(*) {tab._base_sql()}"

        def counted(res):
            count = int(res.rows[0][0])
            note = f"Esta alteracao vai atingir {count} registro(s)."
            if not tab.applied_where:
                note = "ATENCAO: SEM FILTRO! " + note
            self.grab_release()
            if confirm_sql(self, "Confirmar atualizacao em massa", sql, note):
                self.destroy()
                tab._execute([sql], "Atualizacao em massa concluida")
            elif self.winfo_exists():
                self.grab_set()

        tab.app.worker.run(lambda conn: conn.query(count_sql), counted)


# ====================================================================== janela

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1280x780")
        self.root.minsize(900, 500)
        style = ttk.Style()
        if IS_WINDOWS and "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=22)

        self.store = ConnectionStore()
        self.worker = DbWorker(self.root, self._db_error, self._busy)
        self.connection_name = ""
        self.databases = []
        self.tables = {}       # db -> [tabelas]
        self.columns = {}      # (db, tabela) -> [linhas do SHOW COLUMNS]
        self.open_nodes = set()
        self.node_info = {}
        self.query_count = 0

        self._build_menu()
        self._build_layout()
        self.root.after(200, self.open_connection_dialog)

    # --- montagem

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        conn = tk.Menu(menubar, tearoff=0)
        conn.add_command(label="Conectar...", accelerator="Ctrl+O", command=self.open_connection_dialog)
        conn.add_command(label="Desconectar", command=self.disconnect)
        conn.add_separator()
        conn.add_command(label="Sair", command=self.root.destroy)
        menubar.add_cascade(label="Conexao", menu=conn)
        query = tk.Menu(menubar, tearoff=0)
        query.add_command(label="Nova aba SQL", accelerator="Ctrl+T", command=lambda: self.new_query_tab())
        query.add_command(label="Fechar aba", accelerator="Ctrl+W", command=self.close_current_tab)
        menubar.add_cascade(label="Consulta", menu=query)
        self.root.configure(menu=menubar)
        self.root.bind_all("<Control-t>", lambda e: self.new_query_tab())
        self.root.bind_all("<Control-w>", lambda e: self.close_current_tab())
        self.root.bind_all("<Control-o>", lambda e: self.open_connection_dialog())

    def _build_layout(self):
        paned = ttk.Panedwindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned, padding=(4, 4, 0, 4))
        header = ttk.Frame(left)
        header.pack(fill="x")
        ttk.Label(header, text="Bancos de dados", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(header, text="↻", width=3, command=self.refresh_databases).pack(side="right")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.render_tree())
        ttk.Entry(left, textvariable=self.filter_var).pack(fill="x", pady=4)
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        sy = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.tree.tag_configure("db", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("col", foreground="#555")
        self.tree.tag_configure("pk", foreground="#b45309")
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<<TreeviewClose>>", self._on_tree_close)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<Return>", self._on_tree_double)
        self.tree.bind("<Button-3>", self._on_tree_menu)
        paned.add(left, weight=1)

        self.notebook = ttk.Notebook(paned)
        self.notebook.bind("<Button-2>", self._on_tab_middle)
        self.notebook.bind("<Button-3>", self._on_tab_menu)
        paned.add(self.notebook, weight=5)

        status = ttk.Frame(self.root, padding=(6, 2))
        status.pack(fill="x", side="bottom")
        self.conn_label = ttk.Label(status, text="Desconectado")
        self.conn_label.pack(side="left")
        self.busy_label = ttk.Label(status, text="", foreground="#1d4ed8")
        self.busy_label.pack(side="right")

        self.new_query_tab()

    # --- conexao

    def open_connection_dialog(self):
        ConnectionDialog(self)

    def connect(self, params, name):
        self.conn_label.configure(text=f"Conectando em {params['host']}...")

        def ok(conn):
            self.connection_name = name
            self.conn_label.configure(
                text=f"{name}  |  {params['user']}@{params['host']}:{params['port']}  |  "
                     f"MySQL {conn.server_version}  |  charset {params['charset']}")
            self.root.title(f"{APP_NAME} - {name}")
            self.tables.clear()
            self.columns.clear()
            self.open_nodes.clear()
            self.refresh_databases()

        def fail(exc):
            self.conn_label.configure(text="Desconectado")
            messagebox.showerror(APP_NAME, f"Falha na conexao:\n{exc}")
            self.open_connection_dialog()

        self.worker.connect(params, ok, fail)

    def disconnect(self):
        def done(_):
            self.conn_label.configure(text="Desconectado")
            self.root.title(APP_NAME)
            self.databases = []
            self.tables.clear()
            self.columns.clear()
            self.render_tree()
        self.worker.disconnect(done)

    def _db_error(self, exc):
        messagebox.showerror(APP_NAME, str(exc))

    def _busy(self, pending):
        self.busy_label.configure(text="⏳ Executando..." if pending > 0 else "")
        self.root.configure(cursor="watch" if pending > 0 else "")

    # --- arvore

    def refresh_databases(self, quiet=False):
        def job(conn):
            return [r[0] for r in conn.query("SHOW DATABASES").rows]

        def done(dbs):
            self.databases = dbs
            loaded = [db for db in self.tables if db in dbs]
            self.tables = {db: self.tables[db] for db in loaded}
            self.render_tree()
            for db in loaded:
                self.load_tables(db)

        self.worker.run(job, done, (lambda e: None) if quiet else None)

    def load_tables(self, db):
        def done(tables):
            self.tables[db] = tables
            self.render_tree()
        self.worker.run(lambda conn: [r[0] for r in conn.query(f"SHOW TABLES FROM {escape_identifier(db)}").rows],
                        done)

    def load_columns(self, db, table):
        def done(cols):
            self.columns[(db, table)] = cols
            self.render_tree()
        sql = f"SHOW COLUMNS FROM {escape_identifier(table)} FROM {escape_identifier(db)}"
        self.worker.run(lambda conn: conn.query(sql).rows, done)

    def render_tree(self):
        tree = self.tree
        selected = self.node_info.get(tree.focus())
        tree.delete(*tree.get_children())
        self.node_info = {}
        f = self.filter_var.get().strip().lower()
        for db in self.databases:
            tables = self.tables.get(db)
            db_match = f in db.lower()
            table_match = tables is not None and any(f in t.lower() for t in tables)
            if f and not db_match and not table_match:
                continue
            key = ("db", db)
            is_open = key in self.open_nodes or (bool(f) and table_match and not db_match)
            db_iid = tree.insert("", "end", text=db, open=is_open, tags=("db",))
            self.node_info[db_iid] = key
            if tables is None:
                tree.insert(db_iid, "end", text="carregando...")
                continue
            for t in tables:
                if f and not db_match and f not in t.lower():
                    continue
                tkey = ("table", db, t)
                cols = self.columns.get((db, t))
                t_iid = tree.insert(db_iid, "end", text=t, open=tkey in self.open_nodes)
                self.node_info[t_iid] = tkey
                if cols is None:
                    tree.insert(t_iid, "end", text="carregando...")
                    continue
                for c in cols:
                    is_pk = c[3] == "PRI"
                    label = f"{c[0]}  :  {c[1]}" + ("  (PK)" if is_pk else "")
                    c_iid = tree.insert(t_iid, "end", text=label, tags=("pk" if is_pk else "col",))
                    self.node_info[c_iid] = ("col", db, t, c[0])
        if selected:
            for iid, info in self.node_info.items():
                if info == selected:
                    tree.focus(iid)
                    tree.selection_set(iid)
                    tree.see(iid)
                    break

    def _on_tree_open(self, _event):
        info = self.node_info.get(self.tree.focus())
        if not info:
            return
        self.open_nodes.add(info)
        if info[0] == "db" and info[1] not in self.tables:
            self.load_tables(info[1])
        elif info[0] == "table" and (info[1], info[2]) not in self.columns:
            self.load_columns(info[1], info[2])

    def _on_tree_close(self, _event):
        self.open_nodes.discard(self.node_info.get(self.tree.focus()))

    def _on_tree_double(self, event):
        info = self.node_info.get(self.tree.focus())
        if not info:
            return
        if info[0] == "table":
            self.open_table(info[1], info[2])
            return "break"
        if info[0] == "db":
            tab = self.current_tab()
            if isinstance(tab, QueryTab):
                tab.db_var.set(info[1])

    def _on_tree_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.focus(iid)
        self.tree.selection_set(iid)
        info = self.node_info.get(iid)
        if not info:
            return
        menu = tk.Menu(self.root, tearoff=0)
        if info[0] == "db":
            db = info[1]
            menu.add_command(label="Nova consulta neste banco", command=lambda: self.new_query_tab(db))
            menu.add_command(label="Atualizar tabelas", command=lambda: self.load_tables(db))
        elif info[0] in ("table", "col"):
            db, t = info[1], info[2]
            q = f"{escape_identifier(db)}.{escape_identifier(t)}"
            menu.add_command(label="Abrir dados (navegar / editar)", command=lambda: self.open_table(db, t))
            menu.add_command(label="SELECT em nova aba SQL", command=lambda: self._select_in_tab(db, q))
            menu.add_command(label="Estrutura", command=lambda: self._structure(db, t))
            menu.add_command(label="Atualizar colunas", command=lambda: self.load_columns(db, t))
        menu.add_separator()
        menu.add_command(label="Copiar nome", command=lambda: (self.root.clipboard_clear(),
                                                               self.root.clipboard_append(info[-1])))
        menu.tk_popup(event.x_root, event.y_root)

    def _select_in_tab(self, db, qualified):
        tab = self.new_query_tab(db)
        tab.set_sql(f"SELECT * FROM {qualified} LIMIT 1000;")
        tab.execute()

    def _structure(self, db, t):
        tab = self.new_query_tab(db, title=f"Estrutura {t}")
        tab.set_sql(f"SHOW COLUMNS FROM {escape_identifier(t)};\nSHOW INDEX FROM {escape_identifier(t)};\n"
                    f"SHOW CREATE TABLE {escape_identifier(t)};\n")
        tab.execute()

    # --- abas

    def current_tab(self):
        current = self.notebook.select()
        return self.notebook.nametowidget(current) if current else None

    def selected_database(self):
        info = self.node_info.get(self.tree.focus())
        return info[1] if info else ""

    def new_query_tab(self, database=None, title=None):
        self.query_count += 1
        tab = QueryTab(self.notebook, self, database if database is not None else self.selected_database())
        self.notebook.add(tab, text=title or f"SQL {self.query_count}")
        self.notebook.select(tab)
        tab.editor.focus_set()
        return tab

    def open_table(self, db, table):
        tab = TableTab(self.notebook, self, db, table)
        self.notebook.add(tab, text=f"{table}")
        self.notebook.select(tab)
        return tab

    def rename_tab(self, tab, text):
        self.notebook.tab(tab, text=text)

    def close_current_tab(self):
        tab = self.current_tab()
        if tab is not None:
            self.notebook.forget(tab)
            tab.destroy()

    def _tab_at(self, event):
        try:
            index = self.notebook.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None
        return self.notebook.nametowidget(self.notebook.tabs()[index])

    def _on_tab_middle(self, event):
        tab = self._tab_at(event)
        if tab is not None:
            self.notebook.forget(tab)
            tab.destroy()

    def _on_tab_menu(self, event):
        tab = self._tab_at(event)
        if tab is None:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Fechar aba", command=lambda: (self.notebook.forget(tab), tab.destroy()))
        menu.add_command(label="Nova aba SQL", command=lambda: self.new_query_tab())
        menu.tk_popup(event.x_root, event.y_root)

    # --- exportacao

    def export_rows(self, columns, rows, name):
        if not columns:
            messagebox.showinfo(APP_NAME, "Nenhum resultado para exportar.")
            return
        chosen = tk.StringVar()
        path = filedialog.asksaveasfilename(
            title="Exportar resultado", initialfile=re.sub(r"[^\w.-]+", "_", name),
            filetypes=[(label, f"*{ext}") for label, ext in exporters.EXPORT_FORMATS],
            defaultextension=".xlsx", typevariable=chosen)
        if not path:
            return
        if not os.path.splitext(path)[1]:
            ext = next((e for label, e in exporters.EXPORT_FORMATS if label == chosen.get()), ".xlsx")
            path += ext
        self.busy_label.configure(text=f"Exportando {len(rows)} linha(s)...")

        def ok(_):
            self.busy_label.configure(text="")
            if messagebox.askyesno(APP_NAME, f"{len(rows)} linha(s) exportada(s) para:\n{path}\n\n"
                                             "Abrir o arquivo agora?"):
                if IS_WINDOWS:
                    os.startfile(path)

        def fail(exc):
            self.busy_label.configure(text="")
            messagebox.showerror(APP_NAME, f"Erro ao exportar:\n{exc}")

        run_in_thread(self.root, lambda: exporters.export(path, columns, rows, name), ok, fail)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
