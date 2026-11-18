"""Backend, solo libreria standard, per SVN → Git.

La sorgente è il percorso SVN alla revisione indicata, con la storia ereditata
attraverso copie e spostamenti. La modalità path è conservata per compatibilità.
Nessuna operazione pubblica modifica il remoto, salvo ``publish()``.
"""

from __future__ import annotations

import bisect
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import posixpath
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from platform_runtime import discover_runtime
from history_lineage import HistoryLocation, HistoryLineageError, analyze_lineage, historical_url


FORMAT_VERSION = 1
LOG_WINDOW = 100_000
EventCallback = Callable[[dict], None]


class MigrationError(RuntimeError):
    """Errore leggibile dall'interfaccia; i dati locali restano disponibili."""


class MigrationCancelled(MigrationError):
    """Operazione arrestata dall'utente; può essere ripresa."""


@dataclass(frozen=True)
class MigrationConfig:
    svn_url: str
    revision_max: int
    git_url: str
    git_branch: str


@dataclass(frozen=True)
class Analysis:
    start_revision: int
    last_revision: int
    revisions: tuple[int, ...]
    authors: tuple[str, ...]
    source_uuid: str
    branch_path: str
    copy_from_path: str | None = None
    copy_from_revision: int | None = None
    ancestor_history_excluded: bool = False
    destination_head: str | None = None
    history_scope: str = "path"
    repository_root_url: str = ""
    locations: tuple[HistoryLocation, ...] = ()
    branch_start_revision: int | None = None
    copy_boundaries: int = 0

    @property
    def revision_count(self) -> int:
        return len(self.revisions)


@dataclass(frozen=True)
class MigrationResult:
    repo_path: Path
    commit_count: int
    imported_revisions: tuple[int, ...]
    head_sha: str


@dataclass(frozen=True)
class PublishResult:
    git_url: str
    git_branch: str
    head_sha: str
    already_present: bool = False


def _clean_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise MigrationError(f"{label}: inserire un testo valido.")
    value = value.strip()
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MigrationError(f"{label}: valore vuoto o caratteri di controllo non ammessi.")
    if value.startswith("-"):
        raise MigrationError(f"{label}: il valore non può iniziare con '-'.")
    return value


def _validate_url(value: str, label: str, *, svn: bool) -> str:
    value = _clean_text(value, label)
    # Il percorso assoluto del remoto Git è utile anche per repository locali.
    if not svn and (os.path.isabs(value) or re.match(r"^[A-Za-z]:[\\/]", value)):
        return value
    try:
        parsed = urlsplit(value)
        password = parsed.password
    except ValueError as exc:
        raise MigrationError(f"{label}: URL non valido.") from exc
    allowed = {"https", "http", "svn", "svn+ssh", "file"} if svn else {
        "https", "http", "ssh", "git", "file"
    }
    if parsed.scheme.lower() not in allowed:
        # Sintassi Git SSH user@host:repository.git.
        if (not svn and "://" not in value and "::" not in value
                and re.fullmatch(r"(?:[A-Za-z0-9_.+-]+@)?[A-Za-z0-9.-]+:[^:\s][^\s]*", value)):
            return value
        raise MigrationError(
            f"{label}: usare un URL completo supportato"
            + (" (https://, svn://, svn+ssh:// o file://)." if svn else
               " (https://, ssh://, user@host:repo.git o percorso assoluto).")
        )
    if password is not None or (parsed.scheme in {"http", "https"} and parsed.username):
        raise MigrationError(
            f"{label}: non inserire credenziali nell'URL. Usare la cache credenziali "
            "di SVN/Git o l'agente SSH del sistema."
        )
    if parsed.query or parsed.fragment:
        raise MigrationError(f"{label}: query string e frammenti non sono ammessi.")
    if parsed.scheme != "file" and not parsed.hostname:
        raise MigrationError(f"{label}: manca il nome del server.")
    if parsed.scheme == "file" and not parsed.path:
        raise MigrationError(f"{label}: manca il percorso del repository.")
    if svn and re.search(r"/-/(?:commits?|tree|blob|branches|tags)(?:/|$)", unquote(parsed.path)):
        raise MigrationError(
            "URL SVN: questo è un indirizzo della pagina web di GitLab. "
            "Serve l'URL del ramo nel repository Subversion originale, per esempio "
            "https://svn.example.org/repo/branches/release.\n"
            "Puoi ricavarlo dalla copia di lavoro SVN con: svn info --show-item url. "
            "L'URL di clonazione Git va nel terzo campo, URL repository Git."
        )
    return value.rstrip("/")


def validate_config(config: MigrationConfig) -> MigrationConfig:
    """Valida senza rete e senza scrivere file, restituendo valori normalizzati."""
    svn_url = _validate_url(config.svn_url, "URL SVN", svn=True)
    git_url = _validate_url(config.git_url, "URL Git", svn=False)
    branch = _clean_text(config.git_branch, "Branch Git")
    if branch == "HEAD" or branch.startswith("refs/"):
        raise MigrationError("Branch Git: indicare il nome semplice, per esempio main o import/svn.")
    if (branch.endswith(("/", ".")) or ".." in branch or "@{" in branch or "//" in branch
            or re.search(r"[ ~^:?*\[\\]", branch)
            or any(p.startswith(".") or p.endswith(".lock") for p in branch.split("/"))):
        raise MigrationError("Branch Git: nome non valido per un riferimento Git.")
    if isinstance(config.revision_max, bool):
        raise MigrationError("Revisione massima: inserire un intero positivo.")
    try:
        revision = int(config.revision_max)
    except (TypeError, ValueError) as exc:
        raise MigrationError("Revisione massima: inserire un intero positivo.") from exc
    if str(config.revision_max).strip() != str(revision) or revision < 1:
        raise MigrationError("Revisione massima: inserire un intero positivo, senza decimali.")
    return MigrationConfig(svn_url, revision, git_url, branch)


def plan_revision_ranges(revisions: Iterable[int], max_window: int = LOG_WINDOW) -> tuple[tuple[int, int], ...]:
    """Raggruppa revisioni vicine; salta i grandi intervalli senza modifiche.

    Il numero di iterazioni dipende dalle revisioni pertinenti, mai dal massimo
    numero di revisione globale. Dentro ogni gruppo git-svn usa finestre ampie.
    """
    if max_window < 1:
        raise ValueError("max_window deve essere positivo")
    ordered = sorted(set(revisions))
    if not ordered:
        return ()
    result: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for revision in ordered[1:]:
        if revision - previous > max_window:
            result.append((start, previous))
            start = revision
        previous = revision
    result.append((start, previous))
    return tuple(result)


def _redact(text: str) -> str:
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[credenziali]@", text, flags=re.I)
    return re.sub(r"(?i)(password|token|access_token|secret)(\s*[=:]\s*)[^\s&]+", r"\1\2[nascosto]", text)


def _canonical_repository_url(value: str) -> tuple[str, str, int | None, str]:
    """Confronta URL metadata senza userinfo, encoding percentuale e slash finali."""
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme == "file" and host == "localhost":
        host = ""
    defaults = {"http": 80, "https": 443, "ssh": 22, "svn+ssh": 22, "svn": 3690}
    port = parsed.port or defaults.get(scheme)
    return scheme, host, port, posixpath.normpath(unquote(parsed.path))


def _process_is_alive(pid: int) -> bool:
    """Il probe POSIX kill(pid, 0) non è sicuro su Windows."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID non esistente
            return False
        if error == 5:  # ERROR_ACCESS_DENIED: trattare come processo attivo
            return True
        raise ctypes.WinError(error)
    try:
        status = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return status.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


@dataclass
class _CommandResult:
    returncode: int
    stdout: str


class _Runner:
    def __init__(self, emit: EventCallback, cancel: threading.Event):
        self.emit = emit
        self.cancel = cancel
        self.runtime = discover_runtime()

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise MigrationCancelled("Operazione interrotta. I dati locali sono conservati per la ripresa.")

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass
            process.wait(timeout=5)

    def run(self, args: list[str], *, cwd: Path | None = None,
            stream: bool = False, output_file: Path | None = None,
            input_file: Path | None = None, output_bytes_file: Path | None = None,
            on_line: Callable[[str], None] | None = None,
            timeout: float | None = None, allowed_codes: tuple[int, ...] = (0,),
            log_command: bool = True) -> _CommandResult:
        self.check_cancelled()
        if output_file is not None and output_bytes_file is not None:
            raise ValueError("Scegliere output_file oppure output_bytes_file.")
        args = self.runtime.command(args)
        if log_command:
            self.emit({"type": "log", "message": "$ " + _redact(shlex.join(str(a) for a in args))})
        env = self.runtime.environment.copy()
        env.update({
            "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
            # Git::prompt (Perl/git-svn) non interpreta GIT_TERMINAL_PROMPT.
            "GIT_ASKPASS": "", "SSH_ASKPASS": "",
            # Niente helper custom/ext:: eseguibili a partire da un URL.
            "GIT_ALLOW_PROTOCOL": "file:https:http:ssh:git",
        })
        if not self.runtime.windows:
            # La working copy SVN deve poter rappresentare i nomi Unicode.
            # Su Windows si conserva la locale nativa del client installato.
            env["LC_ALL"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
        if not self.runtime.windows and shutil.which("ssh"):
            env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes -oStrictHostKeyChecking=yes")
            env.setdefault("SVN_SSH", "ssh -oBatchMode=yes -oStrictHostKeyChecking=yes")
        kwargs: dict = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                             | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        }
        source = None
        binary_destination = None
        try:
            if input_file is not None:
                source = Path(input_file).open("rb")
            if output_bytes_file is not None:
                binary_destination = Path(output_bytes_file).open("wb")
            process = subprocess.Popen(
                [str(a) for a in args], cwd=cwd, env=env, shell=False,
                stdin=source if source is not None else subprocess.DEVNULL,
                stdout=binary_destination if binary_destination is not None else subprocess.PIPE,
                stderr=subprocess.PIPE if binary_destination is not None else subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, **kwargs,
            )
        except OSError as exc:
            raise MigrationError(f"Impossibile avviare {Path(args[0]).name}: {exc}") from exc
        finally:
            if source is not None:
                source.close()
            if binary_destination is not None:
                binary_destination.close()
        command_output = process.stderr if output_bytes_file is not None else process.stdout
        events: queue.Queue[str | None] = queue.Queue(maxsize=512)
        reader_stop = threading.Event()

        def reader() -> None:
            assert command_output is not None
            try:
                for line in command_output:
                    while True:
                        try:
                            events.put(line, timeout=.1)
                            break
                        except queue.Full:
                            if self.cancel.is_set() or reader_stop.is_set():
                                return
            finally:
                # Non attendere indefinitamente se il thread principale è uscito.
                try:
                    events.put(None, timeout=.2)
                except queue.Full:
                    pass

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        started = time.monotonic()
        parts: list[str] = []
        tail: deque[str] = deque(maxlen=25)
        destination = None
        try:
            if output_file:
                destination = output_file.open("w", encoding="utf-8", newline="")
            done = False
            while not done:
                if self.cancel.is_set():
                    self._stop(process)
                    self.check_cancelled()
                if timeout is not None and time.monotonic() - started > timeout:
                    self._stop(process)
                    raise MigrationError(f"Tempo massimo superato per {Path(args[0]).name}; verificare rete e autenticazione.")
                try:
                    line = events.get(timeout=.1)
                except queue.Empty:
                    if process.poll() is not None and not thread.is_alive():
                        break
                    continue
                if line is None:
                    done = True
                    continue
                tail.append(line)
                if destination:
                    destination.write(line)
                elif not stream:
                    parts.append(line)
                if stream:
                    self.emit({"type": "log", "message": _redact(line.rstrip())})
                if on_line:
                    on_line(line)
            code = process.wait()
            self.check_cancelled()
            output = "".join(parts) if not stream else "".join(tail)
            if code not in allowed_codes:
                detail = _redact("".join(tail).strip())
                raise MigrationError(
                    f"{Path(args[0]).name} è terminato con codice {code}.\n{detail}\n\n"
                    "Se l'errore riguarda credenziali o certificati, configurare prima l'accesso "
                    "con i normali client SVN/Git nello stesso ambiente, poi riprovare."
                )
            return _CommandResult(code, output)
        finally:
            reader_stop.set()
            if destination:
                destination.close()
            if process.poll() is None:
                self._stop(process)
            thread.join(timeout=1)
            if command_output and not thread.is_alive():
                command_output.close()


class Migrator:
    """Importazione riprendibile, con eventi serializzabili per una GUI a thread.

    Il chiamante invoca i metodi in un worker; ``emit`` deve soltanto inserire
    eventi in una coda. Per fermare il worker impostare l'Event ``cancel``.
    Una nuova operazione richiede un Event non impostato.
    """

    def __init__(self, config: MigrationConfig, workspace: Path,
                 emit: EventCallback | None = None, cancel: threading.Event | None = None,
                 *, engine: str | None = None, history_scope: str | None = None):
        self.config = validate_config(config)
        self.engine = engine or "svn-cli"
        self.history_scope = history_scope or ("path" if engine == "git-svn" else "lineage")
        self._engine_explicit = engine is not None
        if self.engine not in {"svn-cli", "git-svn"}:
            raise MigrationError("Motore di importazione non supportato.")
        if self.history_scope not in {"path", "lineage"}:
            raise MigrationError("Ambito della cronologia non supportato.")
        if self.engine == "git-svn" and self.history_scope != "path":
            raise MigrationError("La cronologia ereditata richiede il motore SVN CLI + Git.")
        self.workspace = Path(workspace).expanduser().resolve()
        self.repo_path = self.workspace / "repository"
        self._event_callback = emit or (lambda event: None)
        self._log_handle = None
        self._log_flush_at = 0.0
        self.emit = self._deliver_event
        self.cancel = cancel or threading.Event()
        self.runner = _Runner(self.emit, self.cancel)
        self._analysis: Analysis | None = None
        self._dependencies_checked = False

    def _deliver_event(self, event: dict) -> None:
        event = dict(event)
        if "message" in event:
            event["message"] = _redact(str(event["message"]))
        if self._log_handle and event.get("type") in {"log", "stage"}:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._log_handle.write(f"{stamp} [{event['type']}] {event.get('message', '')}\n")
            now = time.monotonic()
            if event["type"] == "stage" or now - self._log_flush_at > .5:
                self._log_handle.flush()
                self._log_flush_at = now
        self._event_callback(event)

    def _stage(self, message: str) -> None:
        self.emit({"type": "stage", "message": message})

    def _log(self, message: str) -> None:
        self.emit({"type": "log", "message": message})

    def _git(self, *args: str, **kwargs) -> _CommandResult:
        return self.runner.run(["git", "-C", str(self.repo_path), *args], **kwargs)

    def _svn(self, *args: str, **kwargs) -> _CommandResult:
        return self.runner.run(["svn", *args, "--non-interactive"], **kwargs)

    def _atomic_json(self, name: str, value: dict) -> None:
        target = self.workspace / name
        temporary = self.workspace / (name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    @contextmanager
    def _locked(self):
        self.runner.check_cancelled()
        self.workspace.mkdir(parents=True, exist_ok=True)
        lock = self.workspace / ".migration.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                owner = json.loads(lock.read_text(encoding="utf-8"))
                pid = int(owner["pid"])
                if pid <= 0:
                    raise ValueError("pid non valido")
                if not _process_is_alive(pid):
                    raise ProcessLookupError(pid)
            except ProcessLookupError:
                lock.unlink()
                try:
                    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError as exc:
                    raise MigrationError("Un'altra importazione usa questa cartella.") from exc
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise MigrationError(
                    f"Impossibile verificare il blocco della cartella: {lock}. "
                    "Controllare che non vi siano altre importazioni attive."
                ) from exc
            else:
                raise MigrationError("Un'altra operazione usa già questa cartella. Attendere o interromperla.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "created": time.time()}, handle)
            self._ensure_manifest()
            self._log_handle = (self.workspace / "migration.log").open("a", encoding="utf-8", newline="\n")
            yield
        except Exception as exc:
            if self._log_handle:
                self._log(f"Operazione terminata: {exc}")
            raise
        finally:
            if self._log_handle:
                self._log_handle.close()
                self._log_handle = None
            try:
                lock.unlink()
            except FileNotFoundError:
                pass

    def _ensure_manifest(self) -> None:
        manifest_path = self.workspace / "migration.json"
        expected = {"format_version": FORMAT_VERSION, "config": asdict(self.config),
                    "engine": self.engine, "history_scope": self.history_scope}
        if manifest_path.exists():
            try:
                actual = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MigrationError("Il file migration.json non è leggibile; la cartella non viene modificata.") from exc
            saved_engine = actual.get("engine", "git-svn") if isinstance(actual, dict) else None
            saved_scope = actual.get("history_scope", "path") if isinstance(actual, dict) else None
            if (not isinstance(actual, dict) or saved_engine not in {"svn-cli", "git-svn"}
                    or saved_scope not in {"path", "lineage"}
                    or (saved_engine == "git-svn" and saved_scope != "path")
                    or actual.get("format_version") != FORMAT_VERSION
                    or actual.get("config") != asdict(self.config)
                    or set(actual) - {"format_version", "config", "engine", "history_scope"}):
                raise MigrationError("La cartella contiene una migrazione con parametri differenti. Usare una nuova cartella.")
            if saved_scope != self.history_scope:
                raise MigrationError(
                    "Questa cartella contiene un'importazione con un ambito storico diverso. "
                    "La storia ereditata richiede una nuova cartella: i vecchi commit non vengono modificati."
                )
            if self._engine_explicit and saved_engine != self.engine:
                raise MigrationError("La cartella usa un motore di importazione diverso. Usare una nuova cartella per cambiare motore.")
            self.engine = saved_engine
        else:
            allowed = {".migration.lock"}
            if any(path.name not in allowed for path in self.workspace.iterdir()):
                raise MigrationError("La cartella di lavoro non è vuota e non appartiene a questa migrazione.")
            self._atomic_json("migration.json", expected)

    def _dependencies(self) -> None:
        if self._dependencies_checked:
            return
        self._stage("Controllo dei client Git e SVN…")
        for note in self.runner.runtime.notes:
            self._log(note)
        self._log("Motore: SVN e Git nativi (senza git-svn)." if self.engine == "svn-cli" else "Motore: git-svn.")
        missing = self.runner.runtime.missing_tools
        if missing:
            if self.runner.runtime.windows:
                raise MigrationError(
                    "Client Windows non trovati: " + ", ".join(missing) + ".\n"
                    "Servono Git for Windows e svn.exe. Con TortoiseSVN abilita la funzione "
                    "Command line client tools nell'installazione.\n"
                    "I percorsi standard vengono riconosciuti automaticamente; per installazioni "
                    "personalizzate consulta LEGGIMI.md."
                )
            raise MigrationError(
                "Programmi non trovati nel PATH: " + ", ".join(missing)
                + ". Servono Git e Subversion. Su Ubuntu/WSL: "
                  "sudo apt install git subversion python3-tk"
            )
        self.runner.run(["git", "--version"], timeout=30)
        self._svn("--version", "--quiet", timeout=30)
        if self.engine == "git-svn":
            try:
                self.runner.run(["git", "svn", "--version"], timeout=30)
            except MigrationError as exc:
                raise MigrationError("Questa cartella usa git-svn, ma il comando non è disponibile. "
                                     "Le nuove migrazioni native Windows usano solo svn.exe e git.exe. "
                                     "Per riprendere una migrazione precedente, usa il suo ambiente originale.\n" + str(exc)) from exc
        self.runner.run(["git", "check-ref-format", "--branch", self.config.git_branch], timeout=30)
        self._dependencies_checked = True

    def _remote_head(self) -> str | None:
        reference = "refs/heads/" + self.config.git_branch
        result = self.runner.run(
            ["git", "ls-remote", "--exit-code", "--heads", "--", self.config.git_url, reference],
            allowed_codes=(0, 2), timeout=120,
        )
        if result.returncode == 2:
            return None
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == reference and re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
                return fields[0]
        raise MigrationError("Risposta Git inattesa durante la verifica del branch remoto.")

    def _ref_sha(self, reference: str) -> str | None:
        if not (self.repo_path / ".git").is_dir():
            return None
        result = self._git("rev-parse", "--verify", "--quiet", reference, allowed_codes=(0, 1), log_command=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def _source_info(self, peg: str, operative: str) -> ET.Element:
        result = self._svn("info", "--xml", "-r", operative, self.config.svn_url + "@" + peg)
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError as exc:
            raise MigrationError("SVN non ha restituito informazioni XML valide.") from exc
        entry = root.find("entry")
        if entry is None or entry.attrib.get("kind") != "dir":
            raise MigrationError("L'URL SVN deve indicare una directory/branch, non un singolo file.")
        return entry

    def analyze(self) -> Analysis:
        with self._locked():
            return self._analyze_unlocked()

    def _analyze_unlocked(self) -> Analysis:
        self._dependencies()
        self._stage("Verifica della sorgente SVN e della destinazione Git…")
        end = str(self.config.revision_max)
        info = self._source_info(end, end)
        source_uuid = info.findtext("repository/uuid") or ""
        relative = info.findtext("relative-url") or ""
        repository_root_url = info.findtext("repository/root") or ""
        if not source_uuid or not relative.startswith("^"):
            raise MigrationError("SVN non ha restituito UUID e percorso relativo del repository.")
        branch_path = unquote(relative[1:]) or "/"
        # git-svn non ha una selezione universale della peg revision per il suo
        # URL di tracking: rifiutare in modo esplicito i percorsi scomparsi.
        if self.engine == "git-svn":
            try:
                self._source_info("HEAD", "HEAD")
                historical = self._source_info("HEAD", end)
            except MigrationError as exc:
                if isinstance(exc, MigrationCancelled):
                    raise
                raise MigrationError(
                    "Il percorso SVN non è utilizzabile anche dalla sua posizione attuale. "
                    "Il motore git-svn precedente richiede un branch ancora esistente "
                    "e riconducibile alla revisione massima.\n" + str(exc)
                ) from exc
            if historical.findtext("relative-url") != relative:
                raise MigrationError("Il branch ha cambiato percorso o identità: la migrazione git-svn è sospesa.")
        destination_head = self._remote_head()
        local_head = self._ref_sha("refs/heads/" + self.config.git_branch)
        if destination_head and destination_head != local_head:
            raise MigrationError(
                f"Il branch Git '{self.config.git_branch}' esiste già nella destinazione. "
                "Scegliere un nuovo nome: l'app non sostituisce né unisce una storia esistente."
            )
        self._stage("Lettura della storia SVN, comprese le copie dei percorsi precedenti…"
                    if self.history_scope == "lineage" else "Lettura delle revisioni del percorso SVN…")
        log_path = self.workspace / "branch-log.xml"
        temp_log = self.workspace / "branch-log.xml.partial"
        log_options = ["--stop-on-copy"] if self.history_scope == "path" else []
        self._svn(
            "log", "--xml", "--verbose" if self.engine == "svn-cli" else "--quiet",
            *log_options, "-r", f"{end}:1",
            self.config.svn_url + "@" + end, output_file=temp_log,
        )
        revisions: set[int] = set()
        authors: set[str] = set()
        try:
            for _, node in ET.iterparse(temp_log, events=("end",)):
                if node.tag == "logentry":
                    revisions.add(int(node.attrib["revision"]))
                    authors.add(node.findtext("author") or "(no author)")
                    node.clear()
        except (ET.ParseError, ValueError, KeyError) as exc:
            raise MigrationError("Il log SVN non è un XML valido e completo; non è stato avviato l'import.") from exc
        if not revisions:
            raise MigrationError("Nessuna revisione pertinente trovata per questo percorso e limite.")
        ordered = tuple(sorted(revisions))
        if ordered[0] < 1 or ordered[-1] > self.config.revision_max:
            raise MigrationError("Il log SVN contiene revisioni esterne all'intervallo richiesto.")
        copy_from_path = None
        copy_from_revision = None
        locations = ()
        branch_start_revision = ordered[0]
        copy_boundaries = 0
        if self.history_scope == "lineage":
            if not repository_root_url:
                raise MigrationError("SVN non ha restituito l'URL della radice del repository.")
            try:
                plan = analyze_lineage(temp_log, branch_path, repository_root_url,
                                       revision_max=self.config.revision_max)
            except HistoryLineageError as exc:
                raise MigrationError(f"Impossibile ricostruire la storia ereditata del ramo: {exc}") from exc
            if plan.revisions != ordered:
                raise MigrationError("La storia ereditata non coincide con il log SVN. Importazione sospesa.")
            locations = plan.locations
            branch_start_revision = plan.branch_start_revision
            copy_from_path = plan.copy_from_path
            copy_from_revision = plan.copy_from_revision
            copy_boundaries = plan.copy_boundaries
        else:
            first = str(ordered[0])
            copy_log = self._svn(
                "log", "--xml", "--verbose", "--stop-on-copy", "-r", f"{first}:{first}",
                self.config.svn_url + "@" + end,
            )
            try:
                copy_root = ET.fromstring(copy_log.stdout)
                candidates = []
                for item in copy_root.findall(".//path"):
                    target = item.text or ""
                    if (target == branch_path or branch_path.startswith(target.rstrip("/") + "/")) and item.attrib.get("copyfrom-path"):
                        candidates.append((len(target), target, item))
                if candidates:
                    _, target, item = max(candidates, key=lambda item: item[0])
                    copy_from_path = item.attrib["copyfrom-path"] + branch_path[len(target):]
                    copy_from_revision = int(item.attrib["copyfrom-rev"])
            except (ET.ParseError, KeyError, ValueError) as exc:
                raise MigrationError("Impossibile verificare l'origine del branch SVN.") from exc
        analysis = Analysis(
            ordered[0], ordered[-1], ordered, tuple(sorted(authors)), source_uuid,
            branch_path, copy_from_path, copy_from_revision,
            self.history_scope == "path" and copy_from_path is not None, destination_head,
            history_scope=self.history_scope, repository_root_url=repository_root_url,
            locations=locations, branch_start_revision=branch_start_revision,
            copy_boundaries=copy_boundaries,
        )
        os.replace(temp_log, log_path)
        (self.workspace / "revisions.txt").write_text("".join(f"{r}\n" for r in ordered), encoding="ascii")
        self._atomic_json("analysis.json", asdict(analysis))
        self._analysis = analysis
        self._log(f"Trovate {len(ordered):,} revisioni pertinenti: r{ordered[0]} → r{ordered[-1]}.")
        self._log(f"Autori SVN: {len(authors)}. Le email Git saranno username@UUID, senza inventare indirizzi reali.")
        if self.history_scope == "lineage":
            self._log(f"Origine della storia: r{ordered[0]}; creazione/copia del percorso attuale: r{branch_start_revision}.")
            self._log(f"Storia ereditata inclusa, attraverso {copy_boundaries} copie/spostamenti. "
                      "Revisioni di altri percorsi non appartenenti alla genealogia escluse.")
        elif copy_from_path:
            self._log(f"Il percorso nasce da {copy_from_path}@{copy_from_revision}; la storia precedente alla copia è esclusa.")
        return analysis

    def _load_analysis(self) -> Analysis | None:
        if self._analysis:
            return self._analysis
        path = self.workspace / "analysis.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["revisions"] = tuple(data["revisions"])
            data["authors"] = tuple(data["authors"])
            data["locations"] = tuple(HistoryLocation(**location) for location in data.get("locations", ()))
            analysis = Analysis(**data)
            if analysis.history_scope != self.history_scope:
                raise ValueError("ambito storico differente")
            if not analysis.revisions or tuple(sorted(set(analysis.revisions))) != analysis.revisions:
                raise ValueError("sequenza revisioni non valida")
            if analysis.start_revision != analysis.revisions[0] or analysis.last_revision != analysis.revisions[-1]:
                raise ValueError("estremi non validi")
            if analysis.last_revision > self.config.revision_max:
                raise ValueError("revisione massima incoerente")
            if analysis.history_scope == "lineage":
                if tuple(location.revision for location in analysis.locations) != analysis.revisions:
                    raise ValueError("mappatura dei percorsi storici incompleta")
                if (not analysis.repository_root_url or analysis.branch_start_revision is None
                        or not all(location.url and location.repository_path.startswith("/")
                                   and location.segment >= 0 for location in analysis.locations)):
                    raise ValueError("percorsi storici non validi")
                if any(_canonical_repository_url(location.url) != _canonical_repository_url(
                        historical_url(analysis.repository_root_url, location.repository_path))
                       for location in analysis.locations):
                    raise ValueError("URL e percorsi storici incoerenti")
            self._analysis = analysis
            return analysis
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise MigrationError("Analisi salvata non valida. Rieseguire Verifica prima dell'importazione.") from exc

    def _initialize_repository(self) -> str:
        if self.engine == "svn-cli":
            from native_import import initialize_native
            return initialize_native(self)
        if not self.repo_path.exists():
            self.runner.run(["git", "init", "--object-format=sha1", "--initial-branch=svn-import-work", str(self.repo_path)])
        if not (self.repo_path / ".git").is_dir():
            raise MigrationError("La cartella repository esiste ma non contiene un repository Git valido.")
        expected_fetch = ":refs/remotes/svn-source"
        stored_url = self._git("config", "--get", "svn-remote.svn.url", allowed_codes=(0, 1), log_command=False).stdout.strip()
        if stored_url and stored_url != self.config.svn_url:
            raise MigrationError("Il repository locale traccia un URL SVN differente.")
        if not stored_url:
            self._git("svn", "init", "--no-minimize-url", "--id=svn-source", self.config.svn_url)
            # Riferimento esplicito per evitare dipendenze dai default prefix di Git.
            self._git("config", "svn-remote.svn.fetch", expected_fetch)
        current_fetch = self._git("config", "--get", "svn-remote.svn.fetch", log_command=False).stdout.strip()
        if current_fetch != expected_fetch:
            raise MigrationError("Il riferimento SVN locale è stato modificato; importazione sospesa.")
        for name, value in {
            "svn.followparent": "false", "svn-remote.svn.followparent": "false",
            "svn.noMetadata": "false", "svn-remote.svn.noMetadata": "false",
            "svn.useLogAuthor": "false", "core.autocrlf": "false",
            # Ignora eventuali mapping globali lasciati da altre migrazioni.
            "svn.authorsfile": "", "svn.authorsProg": "",
        }.items():
            self._git("config", name, value, log_command=False)
        hooks = self.repo_path / ".git" / "migration-empty-hooks"
        hooks.mkdir(exist_ok=True)
        self._git("config", "core.hooksPath", str(hooks), log_command=False)
        return "refs/remotes/svn-source"

    def _read_imported(self, reference: str, source_uuid: str,
                       analysis: Analysis | None = None) -> tuple[tuple[int, ...], str | None]:
        head = self._ref_sha(reference)
        if not head:
            return (), None
        result = self._git("log", "--format=%H%x00%B%x00", reference, log_command=False)
        fields = result.stdout.split("\0")
        revisions: list[int] = []
        historical_urls = (
            {location.revision: location.url for location in analysis.locations}
            if analysis is not None and analysis.history_scope == "lineage" else {}
        )
        for i in range(0, len(fields) - 1, 2):
            sha, message = fields[i].strip(), fields[i + 1]
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise MigrationError("Cronologia Git locale non riconosciuta.")
            marker = re.search(r"(?:^|\n)git-svn-id: (.+)@(\d+) ([0-9A-Fa-f-]{36})\s*\Z", message)
            if not marker or marker.group(3).lower() != source_uuid.lower():
                raise MigrationError("Un commit locale non ha i metadati SVN attesi. Pubblicazione sospesa.")
            revision = int(marker.group(2))
            if analysis is not None and analysis.history_scope == "lineage" and revision not in historical_urls:
                raise MigrationError("Un commit locale non appartiene alla storia SVN selezionata. Pubblicazione sospesa.")
            expected_url = historical_urls.get(revision, self.config.svn_url)
            if _canonical_repository_url(marker.group(1)) != _canonical_repository_url(expected_url):
                raise MigrationError("Un commit locale proviene da un percorso SVN differente. Pubblicazione sospesa.")
            revisions.append(revision)
        if len(revisions) != len(set(revisions)):
            raise MigrationError("Sono presenti revisioni SVN duplicate nella storia importata.")
        return tuple(sorted(revisions)), head

    def _checkout_options(self) -> list[str]:
        # Il tree importato può contenere .gitattributes. Disabilitiamo i filtri
        # esterni soltanto per questo checkout, evitando esecuzioni (per esempio
        # Git LFS) ereditate dalla configurazione del PC. I commit restano quelli
        # costruiti dal motore e le impostazioni globali non vengono modificate.
        names = self._git("config", "--null", "--name-only", "--get-regexp",
                          r"^filter\..*\.(smudge|process|required)$",
                          allowed_codes=(0, 1), log_command=False).stdout
        drivers = {name.rsplit(".", 1)[0] for name in names.split("\0") if name}
        options: list[str] = []
        for driver in sorted(drivers):
            options.extend(["-c", driver + ".smudge=", "-c", driver + ".process=",
                            "-c", driver + ".required=false"])
        return options

    def migrate(self) -> MigrationResult:
        with self._locked():
            self._dependencies()
            analysis = self._load_analysis() or self._analyze_unlocked()
            self._stage("Preparazione del repository Git locale…")
            reference = self._initialize_repository()
            imported, _ = self._read_imported(reference, analysis.source_uuid, analysis)
            # Un'interruzione può lasciare solo un prefisso dei commit; un buco
            # precedente all'ultimo commit richiede riparazione, non un salto.
            if imported != analysis.revisions[:len(imported)]:
                raise MigrationError("La storia locale non è un prefisso completo della lista SVN. Non proseguo saltando revisioni mancanti.")
            pending = analysis.revisions[len(imported):]
            self.emit({"type": "progress", "current": len(imported), "total": len(analysis.revisions)})
            ranges = plan_revision_ranges(pending)
            self._log(f"Revisioni già importate: {len(imported)}; ancora da importare: {len(pending)}.")

            def on_line(line: str) -> None:
                match = re.match(r"^r(\d+) = [0-9a-f]+", line)
                if match:
                    current = bisect.bisect_right(analysis.revisions, int(match.group(1)))
                    self.emit({"type": "progress", "current": current, "total": len(analysis.revisions)})

            if self.engine == "svn-cli":
                from native_import import import_native
                import_native(self, analysis, imported)
            else:
                for index, (first, last) in enumerate(ranges, 1):
                    self.runner.check_cancelled()
                    self._stage(f"Importazione gruppo {index}/{len(ranges)}: r{first} → r{last}")
                    self._git(
                        "svn", "fetch", "--no-follow-parent", f"--log-window-size={LOG_WINDOW}",
                        "-r", f"{first}:{last}", stream=True, on_line=on_line,
                    )
            self._stage("Verifica della storia importata…")
            imported, head = self._read_imported(reference, analysis.source_uuid, analysis)
            if imported != analysis.revisions or not head:
                missing = sorted(set(analysis.revisions) - set(imported))
                raise MigrationError(
                    "La cronologia Git non corrisponde a tutte le revisioni SVN selezionate. "
                    f"Revisioni mancanti: {missing[:20]}. Pubblicazione sospesa; dati locali conservati."
                )
            self._git("fsck", "--no-reflogs", stream=True)
            local_reference = "refs/heads/" + self.config.git_branch
            current = self._ref_sha(local_reference)
            if current and current != head:
                raise MigrationError("Il branch Git locale contiene commit differenti: non viene sovrascritto.")
            checkout_options = self._checkout_options()
            if not current:
                self._git(*checkout_options, "checkout", "--no-guess", "-b", self.config.git_branch, head, stream=True)
            else:
                self._git(*checkout_options, "checkout", "--no-guess", self.config.git_branch, stream=True)
            result = MigrationResult(self.repo_path, len(imported), imported, head)
            self._atomic_json("result.json", {**asdict(result), "repo_path": str(self.repo_path)})
            self.emit({"type": "progress", "current": len(imported), "total": len(analysis.revisions)})
            self._stage("Importazione locale completata. Pronta per la pubblicazione.")
            return result

    def publish(self) -> PublishResult:
        with self._locked():
            self._dependencies()
            analysis = self._load_analysis()
            if analysis is None or not (self.workspace / "result.json").exists():
                raise MigrationError("Completare prima l'importazione locale.")
            imported, head = self._read_imported("refs/remotes/svn-source", analysis.source_uuid, analysis)
            if imported != analysis.revisions or not head:
                raise MigrationError("La storia locale è incompleta; pubblicazione sospesa.")
            if self._ref_sha("refs/heads/" + self.config.git_branch) != head:
                raise MigrationError("Il branch locale è cambiato dopo l'importazione; non pubblico commit differenti.")
            self._stage("Controllo del branch Git di destinazione…")
            current = self._remote_head()
            if current == head:
                result = PublishResult(self.config.git_url, self.config.git_branch, head, True)
                self._stage("Questo import è già presente sul branch Git di destinazione.")
                return result
            if current is not None:
                raise MigrationError("Il branch remoto esiste già con commit differenti. Nessun commit è stato sovrascritto.")
            reference = "refs/heads/" + self.config.git_branch
            self._stage("Pubblicazione del nuovo branch Git…")
            # Lease con atteso VUOTO = crea soltanto se il riferimento è ancora
            # assente. Protegge anche da una creazione concorrente dopo ls-remote.
            self._git(
                "push", "--porcelain", f"--force-with-lease={reference}:", "--",
                self.config.git_url, f"{head}:{reference}", stream=True,
            )
            observed = self._remote_head()
            if observed != head:
                raise MigrationError("Il push è terminato, ma il branch remoto non corrisponde più al commit importato. Verificare il repository Git.")
            result = PublishResult(self.config.git_url, self.config.git_branch, head)
            self._atomic_json("published.json", asdict(result))
            self._stage("Pubblicazione completata e verificata.")
            return result
