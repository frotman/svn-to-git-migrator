"""Interfaccia desktop italiana. Nessuna dipendenza Python di terze parti."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = None
    messagebox = None
    ttk = None

from migration_core import (
    Analysis,
    MigrationCancelled,
    MigrationConfig,
    MigrationError,
    MigrationResult,
    Migrator,
    validate_config,
)
from platform_runtime import discover_runtime


APP_VERSION = "1.2.0"
BG = "#eef2f6"
NAVY = "#152b42"
INK = "#203347"
MUTED = "#637589"
BLUE = "#1666d3"
GREEN = "#187957"
RED = "#b83a3a"


def config_dict(config: MigrationConfig) -> dict:
    return {
        "svn_url": config.svn_url,
        "revision_max": config.revision_max,
        "git_url": config.git_url,
        "git_branch": config.git_branch,
    }


def workspace_for(config: MigrationConfig) -> Path:
    raw = json.dumps({"config": config_dict(config), "history_scope": "lineage"},
                     sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", config.git_branch).strip(".-")[:45]
    return Path.home() / "SVN_Git_Migrations" / f"{slug or 'branch'}-{key}"


def settings_file() -> Path:
    if os.name == "nt":
        folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        folder = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return folder / "svn-git-migrator" / "settings.json"


def reject_embedded_secrets(value: str) -> None:
    if any(ord(character) < 32 for character in value):
        raise ValueError("Gli URL non possono contenere caratteri di controllo.")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Usa URL senza password, token, query o frammenti. "
                "Le credenziali vanno configurate con SVN/Git o SSH."
            )
        if parsed.scheme in {"http", "https"} and parsed.username:
            raise ValueError(
                "Usa un URL HTTP/HTTPS senza credenziali incorporate. "
                "Configura l'accesso prima con SVN/Git."
            )


def save_settings(config: MigrationConfig) -> None:
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(config_dict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def dependency_check() -> int:
    failures = []
    print(f"SVN → Git {APP_VERSION} | Python {sys.version.split()[0]}")
    try:
        runtime = discover_runtime()
    except ValueError as exc:
        print(f"Configurazione client non valida: {exc}")
        return 1
    for note in runtime.notes:
        print(note)
    commands = [["git", "--version"], ["svn", "--version", "--quiet"]]
    print("Motore SVN + Git nativi: git-svn non è richiesto.")
    if runtime.windows:
        print("WSL non è richiesto; uso del profilo Windows.")
    for logical_args in commands:
        args = runtime.command(logical_args)
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, errors="replace", timeout=15, shell=False,
                                    env=runtime.environment)
            output = (result.stdout or result.stderr).strip()
            print(f"{' '.join(args)}: {output.splitlines()[0] if output else result.returncode}")
            if result.returncode:
                failures.append(" ".join(args))
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"{' '.join(args)}: NON DISPONIBILE ({exc})")
            failures.append(" ".join(args))
    print("Tkinter:", "disponibile" if tk is not None else "NON DISPONIBILE")
    print("Controllo locale: nessuna connessione ai repository e nessuna modifica remota.")
    return 1 if failures or tk is None else 0


class MigrationApp:
    def __init__(self, root):
        self.root = root
        platform_label = "Windows nativo" if os.name == "nt" else "Importazione del ramo"
        self.root.title(f"SVN → Git {APP_VERSION} · {platform_label}")
        self.root.geometry("1050x840")
        self.root.minsize(930, 760)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self.on_callback_error
        self.events = queue.Queue(maxsize=2400)
        self.cancel_event = threading.Event()
        self.worker = None
        self.busy = False
        self.closing = False
        self.analysis = None
        self.import_result = None
        self.active_config = None
        self.workspace = None
        self.started_at = 0.0
        self.progress_indeterminate = False
        self.current_action = None
        self._building = True
        self._entries = []
        self._vars = {
            "svn_url": tk.StringVar(),
            "revision_max": tk.StringVar(),
            "git_url": tk.StringVar(),
            "git_branch": tk.StringVar(value="main"),
        }
        self.stage_var = tk.StringVar(value="Compila i quattro campi, poi verifica la migrazione.")
        self.counter_var = tk.StringVar(value="In attesa")
        self.elapsed_var = tk.StringVar(value="")
        self.scope_var = tk.StringVar(
            value="Storia ereditata inclusa: seguiamo anche le copie e gli spostamenti precedenti del ramo."
        )
        self.stat_vars = {key: tk.StringVar(value="—") for key in ("start", "revisions", "authors", "status")}
        self.stat_vars["status"].set("Pronto")
        self._style()
        self._build()
        self._load_settings()
        for variable in self._vars.values():
            variable.trace_add("write", self._on_edit)
        self._building = False
        self._append_log(
            f"SVN → Git {APP_VERSION} · "
            + ("Windows nativo: uso dei client Git/SVN e del profilo Windows.\n" if os.name == "nt" else "Motore SVN + Git nativi.\n")
            + "Pronto. L'URL SVN deve indicare il ramo del repository Subversion originale.\n"
            "La revisione iniziale viene calcolata seguendo anche la storia ereditata dalle copie.\n"
            "«Importa / riprendi» prepara il repository locale; «Pubblica su Git» invia il branch."
        )
        self.root.after(80, self._poll)
        self.root.after(1000, self._clock)

    def _style(self):
        self.font = "Segoe UI" if os.name == "nt" else "DejaVu Sans"
        self.mono = "Consolas" if os.name == "nt" else "DejaVu Sans Mono"
        self.root.option_add("*Font", (self.font, 10))
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Page.TFrame", background=BG)
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Card.TLabel", background="#ffffff", foreground=INK)
        style.configure("Hint.TLabel", background="#ffffff", foreground=MUTED, font=(self.font, 9))
        style.configure("Input.TEntry", padding=(10, 9), fieldbackground="#fbfcfe",
                        foreground=INK, bordercolor="#cad4df", lightcolor="#cad4df", darkcolor="#cad4df")
        style.map("Input.TEntry", bordercolor=[("focus", BLUE)],
                  fieldbackground=[("disabled", "#f0f3f6")])
        style.configure("Action.TButton", padding=(13, 10), background="#edf2f8", foreground=INK,
                        borderwidth=0, font=(self.font, 10, "bold"))
        style.map("Action.TButton", background=[("active", "#dbe6f3"), ("disabled", "#f1f3f5")],
                  foreground=[("disabled", "#9ba7b4")])
        style.configure("Primary.TButton", padding=(14, 10), background=BLUE, foreground="#ffffff",
                        borderwidth=0, font=(self.font, 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#1156b7"), ("disabled", "#cbd9eb")],
                  foreground=[("disabled", "#f6f8fc")])
        style.configure("Small.TButton", padding=(8, 5), background="#ffffff", foreground=BLUE,
                        borderwidth=0, font=(self.font, 9))
        style.configure("Import.Horizontal.TProgressbar", background=BLUE, troughcolor="#e8eef5",
                        borderwidth=0, lightcolor=BLUE, darkcolor=BLUE)

    def _build(self):
        header = tk.Frame(self.root, bg=NAVY)
        header.pack(fill="x")
        tk.Label(header, text="MIGRAZIONE REPOSITORY", bg=NAVY, fg="#93b5d7",
                 font=(self.font, 9, "bold")).pack(anchor="w", padx=26, pady=(19, 5))
        title = tk.Frame(header, bg=NAVY)
        title.pack(fill="x", padx=26)
        tk.Label(title, text="Da SVN a Git", bg=NAVY, fg="#ffffff",
                 font=(self.font, 25, "bold")).pack(side="left")
        tk.Label(title, text="4 PARAMETRI", bg="#28435f", fg="#d6e7f8", padx=10, pady=6,
                 font=(self.font, 9, "bold")).pack(side="right")
        tk.Label(header, text="Importa il ramo fino a una revisione precisa, con la sua cronologia.",
                 bg=NAVY, fg="#c7d6e5", font=(self.font, 10)).pack(anchor="w", padx=26, pady=(7, 20))

        body = ttk.Frame(self.root, style="Page.TFrame", padding=(22, 18, 22, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)

        fields = ttk.Frame(body, style="Card.TFrame", padding=18)
        fields.grid(row=0, column=0, sticky="ew")
        fields.columnconfigure(0, weight=4)
        fields.columnconfigure(1, weight=2)
        self._field(fields, 0, 0, "01  URL SVN del ramo", "svn_url",
                    "Esempio: https://svn.example.org/repo/branches/release", (0, 16))
        self._field(fields, 0, 1, "02  Revisione massima", "revision_max",
                    "Numero SVN, per esempio 3500000", (0, 0))
        self._field(fields, 3, 0, "03  URL repository Git", "git_url",
                    "Repository di destinazione già creato · HTTPS o SSH", (0, 16))
        self._field(fields, 3, 1, "04  Nome branch Git", "git_branch",
                    "Per esempio main oppure import/release", (0, 0))
        ttk.Label(fields, textvariable=self.scope_var, style="Hint.TLabel",
                  wraplength=935).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

        actions = ttk.Frame(body, style="Page.TFrame")
        actions.grid(row=1, column=0, sticky="ew", pady=14)
        self.check_button = ttk.Button(actions, text="1 · Verifica", style="Action.TButton",
                                       command=lambda: self.start("analyze"))
        self.check_button.pack(side="left")
        self.import_button = ttk.Button(actions, text="2 · Importa / riprendi", style="Primary.TButton",
                                        command=lambda: self.start("migrate"))
        self.import_button.pack(side="left", padx=9)
        self.publish_button = ttk.Button(actions, text="3 · Pubblica su Git", style="Action.TButton",
                                         state="disabled", command=lambda: self.start("publish"))
        self.publish_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Interrompi", style="Action.TButton", state="disabled",
                                      command=self.stop)
        self.stop_button.pack(side="right")

        stats = ttk.Frame(body, style="Card.TFrame", padding=(17, 11))
        stats.grid(row=2, column=0, sticky="ew")
        labels = (("start", "INIZIO STORIA"), ("revisions", "REVISIONI PERTINENTI"),
                  ("authors", "AUTORI SVN"), ("status", "STATO"))
        for column, (key, label) in enumerate(labels):
            stats.columnconfigure(column, weight=1, uniform="stats")
            item = ttk.Frame(stats, style="Card.TFrame")
            item.grid(row=0, column=column, sticky="w", padx=(0, 12))
            ttk.Label(item, text=label, style="Hint.TLabel", font=(self.font, 8, "bold")).pack(anchor="w")
            ttk.Label(item, textvariable=self.stat_vars[key], style="Card.TLabel",
                      font=(self.font, 16, "bold")).pack(anchor="w", pady=(4, 0))

        progress = ttk.Frame(body, style="Page.TFrame")
        progress.grid(row=3, column=0, sticky="ew", pady=(12, 10))
        progress.columnconfigure(0, weight=1)
        tk.Label(progress, textvariable=self.stage_var, bg=BG, fg=INK, anchor="w",
                 wraplength=810, font=(self.font, 9)).grid(row=0, column=0, sticky="w")
        tk.Label(progress, textvariable=self.elapsed_var, bg=BG, fg=MUTED,
                 font=(self.font, 9)).grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.progress = ttk.Progressbar(progress, maximum=100, style="Import.Horizontal.TProgressbar")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        tk.Label(progress, textvariable=self.counter_var, bg=BG, fg=MUTED,
                 font=(self.font, 8)).grid(row=2, column=0, sticky="w")

        log_card = ttk.Frame(body, style="Card.TFrame", padding=12)
        log_card.grid(row=4, column=0, sticky="nsew")
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        log_head = ttk.Frame(log_card, style="Card.TFrame")
        log_head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(log_head, text="Attività", style="Card.TLabel", font=(self.font, 10, "bold")).pack(side="left")
        ttk.Button(log_head, text="Copia log", style="Small.TButton", command=self.copy_log).pack(side="right")
        ttk.Button(log_head, text="Apri cartella", style="Small.TButton", command=self.open_folder).pack(side="right", padx=6)
        self.log = tk.Text(log_card, wrap="word", bg="#f7f9fc", fg=INK, relief="flat",
                           borderwidth=0, font=(self.mono, 9), padx=10, pady=9,
                           height=9, state="disabled", highlightthickness=0)
        self.log.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_card, orient="vertical", command=self.log.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        footer = tk.Frame(body, bg=BG)
        footer.grid(row=5, column=0, sticky="ew", pady=(9, 0))
        tk.Label(footer, text="Pubblicazione separata · Nessuna sovrascrittura di branch esistenti",
                 bg=BG, fg=MUTED, font=(self.font, 8)).pack(side="left")
        ttk.Button(footer, text="Guida e requisiti", style="Small.TButton", command=self.show_help).pack(side="right")

    def _field(self, parent, row, column, label, key, hint, padx):
        ttk.Label(parent, text=label, style="Card.TLabel", font=(self.font, 10, "bold")).grid(
            row=row, column=column, sticky="w", padx=padx, pady=(0 if row == 0 else 12, 6))
        entry = ttk.Entry(parent, textvariable=self._vars[key], style="Input.TEntry")
        entry.grid(row=row + 1, column=column, sticky="ew", padx=padx)
        ttk.Label(parent, text=hint, style="Hint.TLabel").grid(
            row=row + 2, column=column, sticky="w", padx=padx, pady=(5, 0))
        self._entries.append(entry)

    def _load_settings(self):
        try:
            data = json.loads(settings_file().read_text(encoding="utf-8"))
            for key in ("svn_url", "git_url"):
                reject_embedded_secrets(str(data.get(key, "")))
            for key, variable in self._vars.items():
                if key in data:
                    variable.set(str(data[key]))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _read_config(self):
        values = {key: variable.get().strip() for key, variable in self._vars.items()}
        if any(not value for value in values.values()):
            raise ValueError("Compila tutti e quattro i campi.")
        if not re.fullmatch(r"[1-9][0-9]*", values["revision_max"]):
            raise ValueError("La revisione massima deve essere un numero intero positivo, senza prefisso r.")
        for key in ("svn_url", "git_url"):
            reject_embedded_secrets(values[key])
        return validate_config(MigrationConfig(values["svn_url"], int(values["revision_max"]),
                                               values["git_url"], values["git_branch"]))

    def _on_edit(self, *_):
        if self._building or self.busy:
            return
        self.analysis = None
        self.import_result = None
        self.active_config = None
        self.workspace = None
        self.publish_button.configure(state="disabled")
        for key in ("start", "revisions", "authors"):
            self.stat_vars[key].set("—")
        self.stat_vars["status"].set("Pronto")
        self.scope_var.set("Storia ereditata inclusa: seguiamo anche le copie e gli spostamenti precedenti del ramo.")
        self.stage_var.set("Configurazione modificata: verifica o avvia l'importazione.")
        self.counter_var.set("In attesa")
        self.progress["value"] = 0

    def _emit(self, event):
        while not self.closing:
            try:
                self.events.put(event, timeout=0.15)
                return
            except queue.Full:
                if self.cancel_event.is_set() and event.get("type") == "log":
                    return

    def _set_busy(self, busy):
        self.busy = busy
        for entry in self._entries:
            entry.configure(state="disabled" if busy else "normal")
        for button in (self.check_button, self.import_button):
            button.configure(state="disabled" if busy else "normal")
        self.publish_button.configure(state="normal" if not busy and self.import_result else "disabled")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def start(self, action):
        if self.busy:
            return
        try:
            config = self._read_config()
            if action == "publish" and (not self.import_result or config != self.active_config):
                raise ValueError("Completa l'importazione di questa configurazione prima di pubblicare.")
        except (ValueError, MigrationError) as exc:
            messagebox.showerror("Configurazione da completare", str(exc), parent=self.root)
            return
        self.workspace = workspace_for(config)
        self.active_config = config
        self.cancel_event = threading.Event()
        self.current_action = action
        self.started_at = time.monotonic()
        self._set_busy(True)
        self.stat_vars["status"].set({"analyze": "Verifica", "migrate": "Importazione", "publish": "Invio"}[action])
        self.stage_var.set("Controllo dei requisiti e della configurazione…")
        self.counter_var.set("Operazione in corso")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.progress_indeterminate = True
        self._append_log("\n" + {"analyze": "VERIFICA", "migrate": "IMPORTAZIONE / RIPRESA", "publish": "PUBBLICAZIONE"}[action])

        def work():
            try:
                migrator = Migrator(config, self.workspace, emit=self._emit, cancel=self.cancel_event)
                if action in {"analyze", "migrate"}:
                    analysis = migrator.analyze()
                    self._emit({"type": "analysis_result", "result": analysis})
                    try:
                        save_settings(config)
                    except OSError:
                        self._emit({"type": "log", "message": "Impossibile ricordare i campi per il prossimo avvio; l'operazione continua."})
                    result = analysis if action == "analyze" else migrator.migrate()
                else:
                    result = migrator.publish()
                self._emit({"type": "success", "action": action, "result": result})
            except MigrationCancelled:
                self._emit({"type": "cancelled", "action": action})
            except Exception as exc:
                self._emit({"type": "failure", "message": str(exc), "action": action,
                            "unexpected": not isinstance(exc, (MigrationError, OSError, ValueError))})
            finally:
                self._emit({"type": "idle"})

        self.worker = threading.Thread(target=work, name="svn-git-worker", daemon=True)
        self.worker.start()

    def _show_analysis(self, analysis):
        self.analysis = analysis
        self.stat_vars["start"].set(f"r{analysis.start_revision}")
        self.stat_vars["revisions"].set(f"{len(analysis.revisions):,}".replace(",", "."))
        self.stat_vars["authors"].set(str(len(analysis.authors)))
        if analysis.history_scope == "lineage":
            current_start = analysis.branch_start_revision
            self.scope_var.set(
                f"Storia da r{analysis.start_revision}; percorso attuale creato a r{current_start}. "
                f"Storia ereditata inclusa attraverso {analysis.copy_boundaries} copie/spostamenti."
            )
        elif analysis.copy_from_path:
            self.scope_var.set(
                f"Inizio a r{analysis.start_revision}: copia da {analysis.copy_from_path}"
                f"@{analysis.copy_from_revision}. La storia precedente alla copia è esclusa."
            )
        else:
            self.scope_var.set(
                f"Storia del percorso da r{analysis.start_revision} a r{self.active_config.revision_max}. "
                "Gli altri rami sono esclusi."
            )

    def _poll(self):
        if self.closing:
            return
        log_lines = []
        for _ in range(350):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type")
            if kind == "log":
                log_lines.append(str(event.get("message", "")))
            elif kind == "stage":
                self.stage_var.set(str(event.get("message", "")))
            elif kind == "progress":
                total = int(event.get("total", 0))
                current = int(event.get("current", 0))
                if total > 0:
                    self.progress.stop()
                    self.progress_indeterminate = False
                    self.progress.configure(mode="determinate", maximum=total)
                    self.progress["value"] = min(current, total)
                    self.counter_var.set(f"{current:,} / {total:,} revisioni del ramo".replace(",", "."))
            elif kind == "analysis_result":
                self._show_analysis(event["result"])
            elif kind == "success":
                action, result = event["action"], event["result"]
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=100)
                self.progress["value"] = 100
                self.progress_indeterminate = False
                if action == "analyze":
                    self.stat_vars["status"].set("Verificato")
                    self.stage_var.set("Analisi completata. Puoi avviare l'importazione.")
                    self.counter_var.set(f"{len(result.revisions)} revisioni pertinenti individuate")
                elif action == "migrate":
                    self.import_result = result
                    self.stat_vars["status"].set("Importato")
                    self.stage_var.set("Repository locale pronto. Pubblica il branch quando vuoi.")
                    self.counter_var.set(f"{result.commit_count} commit Git · HEAD {result.head_sha[:12]}")
                    log_lines.append(f"Repository locale: {result.repo_path}")
                else:
                    self.stat_vars["status"].set("Pubblicato")
                    self.stage_var.set("Il branch è già pubblicato con gli stessi commit." if result.already_present
                                       else "Pubblicazione completata sul branch Git indicato.")
                    self.counter_var.set(f"Branch {result.git_branch} · HEAD {result.head_sha[:12]}")
                    log_lines.append(f"Git: {result.git_url} · branch: {result.git_branch}")
            elif kind == "cancelled":
                self.stat_vars["status"].set("Interrotto")
                self.stage_var.set("Operazione interrotta. I dati locali sono conservati per la ripresa.")
                self.counter_var.set("Per continuare: Importa / riprendi")
                log_lines.append("Interruzione completata. Nessun dato importato è stato cancellato.")
            elif kind == "failure":
                self.stat_vars["status"].set("Da verificare")
                self.stage_var.set("Operazione non completata. Leggi il dettaglio nel log.")
                self.counter_var.set("Correggi il problema e riprova con gli stessi parametri")
                log_lines.append("ERRORE: " + event["message"])
                if event.get("unexpected"):
                    log_lines.append("Errore imprevisto: conserva il log e la cartella di lavoro per la diagnosi.")
            elif kind == "idle":
                self.progress.stop()
                self.progress_indeterminate = False
                self._set_busy(False)
        if log_lines:
            self._append_log("\n".join(log_lines))
        self.root.after(80, self._poll)

    def _clock(self):
        if self.closing:
            return
        if self.busy and self.started_at:
            seconds = int(time.monotonic() - self.started_at)
            hours, rest = divmod(seconds, 3600)
            minutes, seconds = divmod(rest, 60)
            self.elapsed_var.set(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        self.root.after(1000, self._clock)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 4500:
            self.log.delete("1.0", f"{lines - 3500}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def stop(self):
        if self.busy:
            self.cancel_event.set()
            self.stop_button.configure(state="disabled")
            self.stage_var.set("Interruzione richiesta: attendo la chiusura del processo…")
            self._append_log("Interruzione richiesta. Attendere prima di chiudere la finestra.")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))

    def open_folder(self):
        path = self.workspace
        if not path or not path.exists():
            messagebox.showinfo("Cartella di lavoro", "La cartella viene creata alla prima verifica della migrazione.", parent=self.root)
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)], shell=False)
            elif os.environ.get("WSL_DISTRO_NAME") and shutil.which("explorer.exe"):
                converted = subprocess.run(["wslpath", "-w", str(path)], capture_output=True,
                                           text=True, check=True, timeout=5).stdout.strip()
                subprocess.Popen(["explorer.exe", converted], shell=False)
            else:
                subprocess.Popen(["xdg-open", str(path)], shell=False)
        except (OSError, subprocess.SubprocessError):
            messagebox.showinfo("Cartella di lavoro", str(path), parent=self.root)

    def show_help(self):
        text = (
            "WINDOWS NATIVO\nAvvia AVVIA_WINDOWS.cmd. Servono Python 3.10+ con Tkinter, "
            "Git for Windows e svn.exe. Con TortoiseSVN abilita Command line client tools. "
            "Git-svn e WSL non sono necessari. Le installazioni standard vengono riconosciute automaticamente.\n\n"
            "URL SVN\nIl primo campo richiede il ramo Subversion originale, non la pagina GitLab dei commit. "
            "Dalla copia di lavoro SVN: svn info --show-item url.\n\n"
            "ACCESSO AI REPOSITORY\nConfigura prima autenticazione SVN, credenziali Git o chiavi SSH "
            "nello stesso ambiente in cui esegui l'app. Su Windows si usa anche la configurazione "
            "%APPDATA%\\Subversion, condivisa con TortoiseSVN. Le credenziali salvate solo "
            "nel browser o nell'IDE potrebbero non essere disponibili ai client.\n\n"
            "CRONOLOGIA\nSi importa la storia del ramo fino alla revisione massima, seguendo anche "
            "gli antenati della directory attraverso copie e spostamenti. Inizio storia e creazione "
            "del percorso attuale possono essere revisioni diverse. I merge SVN non diventano merge Git. "
            "Gli username SVN restano gli autori; le email login@UUID sono identificativi tecnici.\n\n"
            "DESTINAZIONE\nIl repository Git deve già esistere. La pubblicazione crea il branch richiesto "
            "e rifiuta un branch esistente con commit diversi.\n\n"
            "RIPRESA\nUsa gli stessi quattro parametri. I dati sono in ~/SVN_Git_Migrations. "
            "La versione 1.2 usa una cartella distinta dalle vecchie importazioni senza storia ereditata.\n\n"
            "LINUX / MAC / WSL\nServono Python con Tkinter, Git e Subversion. "
            "Su Ubuntu: sudo apt install python3-tk git subversion.\n\n"
            "Il file LEGGIMI.md contiene la guida completa."
        )
        messagebox.showinfo("Guida e requisiti", text, parent=self.root)

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno("Operazione in corso", "Interrompere l'operazione e chiudere quando il processo è terminato?",
                                       parent=self.root):
                return
            self.stop()
            self.root.after(150, self._close_when_idle)
        else:
            self.closing = True
            self.root.destroy()

    def _close_when_idle(self):
        if self.worker and self.worker.is_alive():
            self.root.after(150, self._close_when_idle)
        else:
            self.closing = True
            self.root.destroy()

    def on_callback_error(self, exc_type, exc_value, exc_traceback):
        self._append_log("Errore dell'interfaccia: " + str(exc_value))
        traceback.print_exception(exc_type, exc_value, exc_traceback)


def main():
    if "--self-check" in sys.argv[1:]:
        return dependency_check()
    if tk is None:
        if os.name == "nt":
            print("Tkinter non è disponibile nel Python Windows. Installa/aggiungi Tcl/Tk "
                  "nell'installazione Python per Windows.", file=sys.stderr)
        else:
            print("Tkinter non è installato. Su Ubuntu/WSL: sudo apt install python3-tk", file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Impossibile aprire l'interfaccia grafica: {exc}", file=sys.stderr)
        print("Su Windows usa AVVIA_WINDOWS.cmd con Python nativo e Tkinter. "
              "Su Linux/WSL deve essere disponibile una sessione grafica.", file=sys.stderr)
        return 1
    MigrationApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
