#!/usr/bin/env python3
from __future__ import annotations

"""Individuazione dei client locali, senza installazioni o modifiche globali.

Su Windows usa gli eseguibili nativi e la directory di configurazione standard
di Subversion, condivisa con TortoiseSVN. Non legge né copia credenziali.
"""

pass

from dataclasses import dataclass
import ntpath
import os
import re
import shutil
from typing import Mapping, Sequence


@dataclass(frozen=True)
class RuntimeConfig:
    git_executable: str | None
    svn_executable: str | None
    environment: dict[str, str]
    svn_config_dir: str | None
    notes: tuple[str, ...]
    windows: bool = False

    @property
    def missing_tools(self) -> tuple[str, ...]:
        return tuple(name for name, path in (("git", self.git_executable),
                                             ("svn", self.svn_executable)) if not path)

    def command(self, args: Sequence[str | os.PathLike]) -> list[str]:
        """Restituisce argv per Popen(shell=False); non esegue il comando."""
        result = [os.fspath(value) for value in args]
        if not result:
            raise ValueError("Comando vuoto.")
        name = ntpath.basename(result[0]).lower()
        if name in {"git", "git.exe"}:
            if self.git_executable:
                result[0] = self.git_executable
            # Compatibilità con il motore git-svn esistente. Il motore nativo
            # Windows usa soltanto svn.exe e git.exe.
            svn_index = _git_svn_index(result)
            if (self.svn_config_dir and svn_index is not None
                    and len(result) > svn_index + 1
                    and result[svn_index + 1] in {"init", "fetch", "clone", "rebase"}
                    and not _has_config_dir(result[svn_index + 2:])):
                result[svn_index + 2:svn_index + 2] = ["--config-dir", self.svn_config_dir]
        elif name in {"svn", "svn.exe"}:
            if self.svn_executable:
                result[0] = self.svn_executable
            if self.svn_config_dir and not _has_config_dir(result):
                # Le opzioni globali SVN possono precedere il sottocomando;
                # così un eventuale '--' del chiamante rimane intatto.
                result[1:1] = ["--config-dir", self.svn_config_dir]
        return result


def _has_config_dir(args: Sequence[str]) -> bool:
    for value in args:
        if value == "--":
            break
        if value == "--config-dir" or value.startswith("--config-dir="):
            return True
    return False


def _git_svn_index(args: Sequence[str]) -> int | None:
    """Individua solo il sottocomando Git, saltando le opzioni globali note.

    Non cerchiamo la parola 'svn' negli argomenti: potrebbe essere il nome
    di una directory passato a -C, un valore di -c o parte di un URL.
    """
    with_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
    flag_only = {
        "-p", "-P", "--paginate", "--no-pager", "--bare", "--no-replace-objects",
        "--no-lazy-fetch", "--no-optional-locks", "--no-advice", "--literal-pathspecs",
        "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
    }
    index = 1
    while index < len(args):
        value = args[index]
        if value in with_value:
            if index + 1 >= len(args):
                return None
            index += 2
        elif (value in flag_only or value.startswith(("-C", "-c")) and len(value) > 2
              or any(value.startswith(option + "=") for option in with_value
                     if option.startswith("--"))
              or value.startswith(("--exec-path=", "--attr-source="))):
            index += 1
        else:
            return index if value == "svn" else None
    return None


def _env_get(environ: Mapping[str, str], name: str) -> str:
    """Il mapping restituito da Windows è insensibile alle maiuscole."""
    upper = name.upper()
    return next((value for key, value in environ.items() if key.upper() == upper), "")


def _expand_windows_path(value: str, environ: Mapping[str, str]) -> str:
    value = value.strip().strip('"')
    value = re.sub(r"%([^%]+)%", lambda match: _env_get(environ, match.group(1))
                   or match.group(0), value)
    return ntpath.normpath(value) if value else ""


def _absolute_windows_path(value: str) -> bool:
    drive, tail = ntpath.splitdrive(value)
    return bool(drive and (tail.startswith(("\\", "/")) or drive.startswith("\\\\")))


def _registry_install_dirs() -> tuple[list[str], list[str]]:
    """Solo valori noti: nessuna scansione o scrittura del registro."""
    git_dirs: list[str] = []
    svn_dirs: list[str] = []
    try:
        import winreg
    except ImportError:
        return git_dirs, svn_dirs
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for key, name, destination in (
                (r"SOFTWARE\GitForWindows", "InstallPath", git_dirs),
                (r"SOFTWARE\TortoiseSVN", "Directory", svn_dirs),
            ):
                try:
                    with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                        value, kind = winreg.QueryValueEx(handle, name)
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and value:
                        destination.append(value)
                except OSError:
                    continue
    return git_dirs, svn_dirs


def _unique_windows_paths(values: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = _expand_windows_path(value, environ)
        # Non cercare automaticamente nella directory corrente: uno zip
        # scaricato non deve poter sostituire i client di sistema.
        if not _absolute_windows_path(value):
            continue
        normalized = ntpath.normcase(value)
        if normalized not in seen:
            seen.add(normalized)
            result.append(value)
    return result


def _find_windows_tool(name: str, directories: Sequence[str],
                       environ: Mapping[str, str]) -> str | None:
    for directory in _unique_windows_paths(directories, environ):
        candidate = ntpath.join(directory, name + ".exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def _explicit_tool(environ: Mapping[str, str], name: str, windows: bool) -> str | None:
    variable = "SVNGIT_" + name.upper() + "_EXE"
    value = _env_get(environ, variable) if windows else environ.get(variable, "")
    if not value:
        return None
    value = _expand_windows_path(value, environ) if windows else os.path.expanduser(value)
    absolute = _absolute_windows_path(value) if windows else os.path.isabs(value)
    if (not absolute or not os.path.isfile(value)
            or (windows and not value.lower().endswith(".exe"))):
        raise ValueError(f"{variable}: indicare il percorso assoluto di un eseguibile esistente.")
    return value


def discover_runtime(environ: Mapping[str, str] | None = None,
                     windows: bool | None = None) -> RuntimeConfig:
    """Prepara il solo ambiente figlio per i client già installati.

    Le variabili SVNGIT_GIT_EXE, SVNGIT_SVN_EXE e SVNGIT_SVN_CONFIG_DIR
    permettono un override esplicito senza aggiungere campi alla GUI.
    Non modifica os.environ, PATH di sistema o le impostazioni dei client.
    """
    windows = os.name == "nt" if windows is None else windows
    original = os.environ if environ is None else environ
    # os.environ su Windows lo fa già, ma anche i mapping forniti dal
    # chiamante devono evitare chiavi duplicate quali Path/PATH o Git_Ssh.
    env = ({key.upper(): value for key, value in original.items()} if windows
           else dict(original))
    git = _explicit_tool(env, "git", windows)
    svn = _explicit_tool(env, "svn", windows)
    config_dir = _env_get(env, "SVNGIT_SVN_CONFIG_DIR") if windows else env.get(
        "SVNGIT_SVN_CONFIG_DIR", "")
    if config_dir:
        config_dir = (_expand_windows_path(config_dir, env) if windows else
                      os.path.expanduser(config_dir))
        if not (_absolute_windows_path(config_dir) if windows else os.path.isabs(config_dir)):
            raise ValueError("SVNGIT_SVN_CONFIG_DIR: usare un percorso assoluto.")

    if not windows:
        git = git or shutil.which("git", path=env.get("PATH", ""))
        svn = svn or shutil.which("svn", path=env.get("PATH", ""))
        return RuntimeConfig(git, svn, env, config_dir or None, (), False)

    path_value = _env_get(env, "PATH")
    path_dirs = path_value.split(";")
    git_registry, svn_registry = _registry_install_dirs()
    roots = [value for value in (_env_get(env, "ProgramW6432"),
                                 _env_get(env, "ProgramFiles"),
                                 _env_get(env, "ProgramFiles(x86)")) if value]
    local = _env_get(env, "LOCALAPPDATA")
    git_roots = list(git_registry) + [ntpath.join(root, "Git") for root in roots]
    if local:
        git_roots.append(ntpath.join(local, "Programs", "Git"))
    git_dirs = list(path_dirs)
    for root in git_roots:
        # 'cmd' è il punto d'ingresso Git for Windows adatto ai processi
        # Windows. Non aggiungiamo usr/bin con altri programmi/DLL omonimi.
        git_dirs.extend([ntpath.join(root, "cmd"), ntpath.join(root, "bin")])

    svn_roots = []
    for root in roots:
        svn_roots.extend(ntpath.join(root, product) for product in
                         ("TortoiseSVN", "SlikSvn", "SlikSVN", "Subversion",
                          "Apache-Subversion"))
    if local:
        svn_roots.append(ntpath.join(local, "Programs", "TortoiseSVN"))
    svn_dirs = list(path_dirs)
    for root in svn_registry:
        # A seconda dell'installer/versione Directory può già terminare
        # con 'bin'. Entrambi i layout devono trovare lo stesso svn.exe.
        svn_dirs.extend([ntpath.join(root, "bin"), root])
    for root in svn_roots:
        svn_dirs.append(ntpath.join(root, "bin"))

    git = git or _find_windows_tool("git", git_dirs, env)
    svn = svn or _find_windows_tool("svn", svn_dirs, env)
    if not config_dir:
        appdata = _env_get(env, "APPDATA")
        if appdata:
            config_dir = ntpath.join(appdata, "Subversion")

    # Solo i processi figli ricevono le directory trovate. Le installazioni
    # standard entrano in coda per non cambiare le altre preferenze nel PATH.
    known_dirs = {ntpath.normcase(path) for path in _unique_windows_paths(path_dirs, env)}
    added: list[str] = []
    for executable in (git, svn):
        if executable:
            directory = ntpath.dirname(executable)
            normalized = ntpath.normcase(directory)
            if normalized not in known_dirs:
                known_dirs.add(normalized)
                added.append(directory)
    path_key = next((key for key in env if key.upper() == "PATH"), "PATH")
    if added:
        env[path_key] = ";".join(value for value in (path_value, *added) if value)
    notes = ["Esecuzione nativa Windows: client e credenziali dell'utente Windows."]
    if git:
        notes.append("Git: " + git)
    if svn:
        notes.append("SVN: " + svn)
    if config_dir:
        notes.append("Configurazione SVN: " + config_dir)
    return RuntimeConfig(git, svn, env, config_dir or None, tuple(notes), True)


"""Resolve the root directory's inherited SVN history from a verbose log.

Input is ``svn log --xml --verbose -r END:1 URL@END`` without
``--stop-on-copy`` or ``--use-merge-history``. Only the selected directory and
its ancestors are followed; copying a child does not import the child's
independent history. No request is made for unrelated global revisions.

The server must expose the relevant changed paths and copy metadata. An add
with no copy metadata is the visible origin. SVN authorization can conceal
both copy attributes; that case is indistinguishable from an original add
using this response alone, so callers must not promise inaccessible history.
"""
pass

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET


class HistoryLineageError(ValueError):
    """The visible log cannot safely identify the selected history."""


@dataclass(frozen=True)
class HistoryLocation:
    revision: int
    repository_path: str
    url: str
    # Identity changes even when a replace/copy reuses the same pathname.
    segment: int = 0


@dataclass(frozen=True)
class LineagePlan:
    revisions: tuple[int, ...]
    locations: tuple[HistoryLocation, ...]
    branch_start_revision: int
    copy_from_path: str | None
    copy_from_revision: int | None
    copy_boundaries: int


def _repository_path(value: str) -> str:
    if not value or not value.startswith("/") or "\0" in value:
        raise HistoryLineageError("Il log contiene un percorso SVN non valido.")
    if value != "/" and (value.endswith("/") or any(
            part in {"", ".", ".."} for part in value.split("/")[1:])):
        raise HistoryLineageError("Il log contiene un percorso SVN non canonico.")
    return value


def _contains(parent: str, child: str) -> bool:
    return parent == "/" or parent == child or child.startswith(parent + "/")


def historical_url(repository_root_url: str, repository_path: str) -> str:
    """Append an actual SVN path to an already encoded repository-root URL."""
    path = _repository_path(repository_path)
    try:
        parsed = urlsplit(repository_root_url)
    except ValueError as exc:
        raise HistoryLineageError("URL radice del repository SVN non valido.") from exc
    if (not parsed.scheme or parsed.query or parsed.fragment
            or (parsed.scheme != "file" and not parsed.netloc)):
        raise HistoryLineageError("URL radice del repository SVN non valido.")
    encoded = parsed.path.rstrip("/") + ("" if path == "/" else quote(path, safe="/"))
    return urlunsplit((parsed.scheme, parsed.netloc, encoded or "/", "", ""))


def analyze_lineage(log_path: Path, branch_path: str, repository_root_url: str,
                    *, revision_max: int | None = None) -> LineagePlan:
    """Map each relevant revision to its historical path and copy segment.

    A copy at R still belongs to its destination. Older entries must be at
    or before its ``copyfrom-rev`` and belong to ``copyfrom-path + suffix``.
    The most specific added/replaced ancestor wins when a parent and child
    are independently replaced within the same commit.
    """
    current_path = _repository_path(branch_path)
    historical_url(repository_root_url, current_path)  # validate before parsing
    if revision_max is not None and revision_max < 1:
        raise HistoryLineageError("Revisione massima SVN non valida.")
    ceiling = revision_max
    previous_revision = None
    branch_start = None
    immediate_copy_path = None
    immediate_copy_revision = None
    segment = 0
    locations: list[HistoryLocation] = []
    origin_found = False
    try:
        root = None
        entry_depth = 0
        for event, node in ET.iterparse(log_path, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = node
                    if root.tag != "log":
                        raise HistoryLineageError("Il documento XML non è un log SVN.")
                if node.tag == "logentry":
                    entry_depth += 1
                    if entry_depth != 1:
                        raise HistoryLineageError(
                            "Il log include revisioni di merge annidate. "
                            "Richiedere la storia senza --use-merge-history."
                        )
                continue
            if node.tag != "logentry":
                continue
            revision = int(node.attrib["revision"])
            if revision < 1 or (previous_revision is not None and revision >= previous_revision):
                raise HistoryLineageError("Il log SVN non è ordinato per revisioni decrescenti univoche.")
            if ceiling is not None and revision > ceiling:
                raise HistoryLineageError(
                    f"La revisione r{revision} supera la revisione sorgente di una copia SVN. "
                    "Non importo modifiche che quel ramo non ha ereditato."
                )
            if origin_found:
                raise HistoryLineageError("Il log contiene revisioni precedenti all'origine del percorso selezionato.")
            previous_revision = revision
            paths = node.find("paths")
            if paths is None or not list(paths):
                raise HistoryLineageError(
                    f"A r{revision} mancano i percorsi modificati. "
                    "Verificare che il server renda visibili storia e metadati delle copie."
                )
            ancestor_changes: list[tuple[str, ET.Element]] = []
            relevant = False
            for changed in paths.findall("path"):
                target = _repository_path(changed.text or "")
                if _contains(current_path, target) or _contains(target, current_path):
                    relevant = True
                if _contains(target, current_path) and changed.attrib.get("action") in {"A", "R", "D"}:
                    ancestor_changes.append((target, changed))
            if not relevant:
                raise HistoryLineageError(
                    f"La revisione r{revision} non modifica il percorso storico atteso {current_path}. "
                    "I metadati di copia potrebbero essere incompleti."
                )
            locations.append(HistoryLocation(
                revision, current_path, historical_url(repository_root_url, current_path), segment,
            ))
            if ancestor_changes:
                target, boundary = max(ancestor_changes, key=lambda pair: len(pair[0]))
                if boundary.attrib.get("action") == "D":
                    raise HistoryLineageError(
                        f"Il log elimina il percorso storico a r{revision}, "
                        "ma non ne identifica un'origine valida."
                    )
                if branch_start is None:
                    branch_start = revision
                has_path = "copyfrom-path" in boundary.attrib
                has_revision = "copyfrom-rev" in boundary.attrib
                if has_path != has_revision:
                    raise HistoryLineageError(f"Metadati di copia incompleti a r{revision}.")
                if not has_path:
                    origin_found = True
                else:
                    source_path = _repository_path(boundary.attrib["copyfrom-path"])
                    source_revision = int(boundary.attrib["copyfrom-rev"])
                    if not 0 <= source_revision < revision:
                        raise HistoryLineageError(f"Revisione sorgente della copia non valida a r{revision}.")
                    suffix = current_path[len(target):] if target != "/" else current_path
                    if target == "/" and current_path == "/":
                        suffix = ""
                    inherited_path = _repository_path(source_path.rstrip("/") + suffix or "/")
                    if segment == 0:
                        immediate_copy_path = inherited_path
                        immediate_copy_revision = source_revision
                    current_path = inherited_path
                    ceiling = source_revision
                    segment += 1
            node.clear()
            entry_depth -= 1
            # Element.clear alone would leave an empty element per revision
            # attached to the root for the lifetime of a very large log.
            if root is not None:
                root.clear()
    except HistoryLineageError:
        raise
    except (OSError, ET.ParseError, KeyError, TypeError, ValueError) as exc:
        raise HistoryLineageError("Il log SVN non contiene una storia completa e valida.") from exc
    if not locations:
        raise HistoryLineageError("Nessuna revisione pertinente trovata nel log SVN.")
    # The repository root exists at r0 without an explicit add log entry.
    if not origin_found and current_path != "/":
        raise HistoryLineageError(
            "La storia SVN termina prima di mostrare la creazione del percorso antenato. "
            "Verificare i permessi di lettura sugli antenati e rieseguire Verifica."
        )
    if branch_start is None:
        if branch_path != "/":
            raise HistoryLineageError("Non è stato individuato l'inizio del percorso SVN corrente.")
        branch_start = 0
    ordered = tuple(reversed(locations))
    return LineagePlan(tuple(location.revision for location in ordered), ordered,
                       branch_start, immediate_copy_path, immediate_copy_revision, segment)


"""Backend, solo libreria standard, per SVN → Git.

La sorgente è il percorso SVN alla revisione indicata, con la storia ereditata
attraverso copie e spostamenti. La modalità path è conservata per compatibilità.
Nessuna operazione pubblica modifica il remoto, salvo ``publish()``.
"""

pass

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

pass
pass


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
            pass
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
                pass
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


"""Motore SVN CLI → Git, utilizzabile anche senza git-svn su Windows.

Una working copy privata riceve solo le revisioni selezionate. I metadati
locali SVN permettono di riutilizzare i file invariati; i file modificati
entrano in Git con fast-import, senza filtri Git, .gitignore o conversioni
di fine riga. Ogni revisione aggiorna atomicamente il riferimento di import.
"""
pass

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

pass


NATIVE_REFERENCE = "refs/remotes/svn-source"


@dataclass(frozen=True)
class _NativeRevision:
    revision: int
    author: str
    date: str
    message: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class _NativeFile:
    path: Path
    relative: str
    last_changed: int
    mode: str
    eol: str
    keywords: str
    special: bool

    @property
    def signature(self) -> tuple[Any, ...]:
        return self.last_changed, self.mode, self.eol, self.keywords, self.special


def initialize_native(host) -> str:
    """Prepara un repository privato senza invocare git-svn."""
    if not host.repo_path.exists():
        host.runner.run([
            "git", "init", "--object-format=sha1", "--initial-branch=svn-import-work",
            str(host.repo_path),
        ])
    if not (host.repo_path / ".git").is_dir():
        raise MigrationError("La cartella repository non contiene un repository Git valido.")
    stored = host._git("config", "--get", "svngit.sourceurl", allowed_codes=(0, 1),
                       log_command=False).stdout.strip()
    if stored and stored != host.config.svn_url:
        raise MigrationError("Il repository locale appartiene a una sorgente SVN differente.")
    engine = host._git("config", "--get", "svngit.engine", allowed_codes=(0, 1),
                       log_command=False).stdout.strip()
    if engine and engine != "svn-cli":
        raise MigrationError("Il repository locale usa un motore di importazione differente.")
    for key, value in {
        "svngit.engine": "svn-cli", "svngit.sourceurl": host.config.svn_url,
        "core.autocrlf": "false", "core.safecrlf": "false",
    }.items():
        host._git("config", key, value, log_command=False)
    if host.runner.runtime.windows:
        host._git("config", "core.longpaths", "true", log_command=False)
    hooks = host.repo_path / ".git" / "migration-empty-hooks"
    hooks.mkdir(exist_ok=True)
    host._git("config", "core.hooksPath", str(hooks), log_command=False)
    return NATIVE_REFERENCE


def _load_native_revisions(path: Path, expected: tuple[int, ...]) -> dict[int, _NativeRevision]:
    records: dict[int, _NativeRevision] = {}
    try:
        for _, node in ET.iterparse(path, events=("end",)):
            if node.tag != "logentry":
                continue
            revision = int(node.attrib["revision"])
            if revision in records:
                raise ValueError("revisione duplicata")
            if node.find("msg") is None or node.find("paths") is None:
                raise ValueError("mancano messaggi o percorsi modificati")
            records[revision] = _NativeRevision(
                revision, node.findtext("author") or "(no author)",
                node.findtext("date") or "", node.findtext("msg") or "",
                tuple(p.text or "" for p in node.findall("paths/path")),
            )
            node.clear()
    except (OSError, ET.ParseError, KeyError, ValueError) as exc:
        raise MigrationError(
            "Il log per il motore nativo è incompleto. Rieseguire «Verifica» "
            "per acquisire messaggi e percorsi delle revisioni SVN."
        ) from exc
    if tuple(sorted(records)) != expected:
        raise MigrationError("Il log nativo non corrisponde alle revisioni dell'analisi. Rieseguire Verifica.")
    return records


def _relative_native_path(raw: str, wc: Path) -> tuple[Path, str]:
    # Non usare resolve(): un link SVN può legittimamente puntare fuori dalla WC.
    path = Path(os.path.abspath(raw))
    try:
        relative = path.relative_to(wc).as_posix()
    except ValueError as exc:
        raise MigrationError("SVN ha restituito un percorso esterno alla working copy privata.") from exc
    if relative == ".":
        relative = ""
    if any(component.casefold() == ".git" for component in relative.split("/")):
        raise MigrationError(
            f"Il percorso SVN contiene una directory o un file riservato a Git: {relative}. "
            "La migrazione è sospesa per evitare una perdita silenziosa di file."
        )
    return path, relative


def _native_snapshot(host, wc: Path) -> dict[str, _NativeFile]:
    status = host._svn("status", "--xml", "--verbose", "--ignore-externals", str(wc),
                       log_command=False)
    properties = host._svn("proplist", "--xml", "--verbose", "--recursive", str(wc) + "@",
                           log_command=False)
    try:
        property_map: dict[str, dict[str, str]] = {}
        for target in ET.fromstring(properties.stdout).findall("target"):
            _, relative = _relative_native_path(target.attrib["path"], wc)
            property_map[relative] = {
                prop.attrib["name"]: prop.text or "" for prop in target.findall("property")
            }
        snapshot: dict[str, _NativeFile] = {}
        for entry in ET.fromstring(status.stdout).findall(".//entry"):
            path, relative = _relative_native_path(entry.attrib["path"], wc)
            state = entry.find("wc-status")
            if state is None:
                raise ValueError("stato assente")
            item = state.attrib.get("item", "")
            if item in {"external", "unversioned", "ignored"} or state.attrib.get("file-external") == "true":
                continue
            if (item != "normal" or state.attrib.get("props", "none") not in {"none", "normal"}
                    or state.attrib.get("switched") == "true"
                    or state.attrib.get("tree-conflicted") == "true"):
                raise MigrationError(
                    f"La working copy privata contiene modifiche o conflitti: {relative or '.'} "
                    f"(stato {item}). Non viene ripristinata né cancellata automaticamente."
                )
            props = property_map.get(relative, {})
            special = "svn:special" in props
            if path.is_dir() and not path.is_symlink() and not special:
                continue
            if not relative or (not path.is_file() and not path.is_symlink()):
                raise MigrationError(f"File SVN mancante o non supportato: {relative or '.'}.")
            commit = state.find("commit")
            if commit is None:
                raise ValueError("ultima revisione del file assente")
            eol = props.get("svn:eol-style", "")
            if eol and eol not in {"native", "LF", "CR", "CRLF"}:
                raise MigrationError(f"Proprietà svn:eol-style non riconosciuta per {relative}: {eol}.")
            mode = "120000" if special else "100755" if "svn:executable" in props else "100644"
            snapshot[relative] = _NativeFile(
                path, relative, int(commit.attrib["revision"]), mode, eol,
                props.get("svn:keywords", ""), special,
            )
        return snapshot
    except (ET.ParseError, ValueError, KeyError) as exc:
        raise MigrationError("Metadati XML della working copy SVN non validi.") from exc


def _changed_native_prefixes(paths: tuple[str, ...], branch_path: str) -> tuple[str, ...]:
    branch = branch_path.rstrip("/")
    result = []
    for path in paths:
        prefix = path.rstrip("/")
        if prefix == branch or branch.startswith(prefix + "/"):
            return ("",)
        if prefix.startswith(branch + "/"):
            result.append(prefix[len(branch) + 1:])
    return tuple(result)


def _native_path_changed(relative: str, prefixes: tuple[str, ...]) -> bool:
    return any(not prefix or relative == prefix or relative.startswith(prefix + "/")
               for prefix in prefixes)


def _quote_native_git_path(path: str) -> bytes:
    # Il protocollo fast-import usa quoting C, non JSON (che produce \uNNNN).
    raw = path.encode("utf-8")
    result = bytearray(b'"')
    for value in raw:
        if value in {34, 92}:
            result.extend(b"\\" + bytes((value,)))
        elif 32 <= value < 127:
            result.append(value)
        else:
            result.extend(("\\%03o" % value).encode("ascii"))
    result.extend(b'"')
    return bytes(result)


def _native_identity(record: _NativeRevision, source_uuid: str) -> bytes:
    if any(c in record.author for c in "<>\n\r\0"):
        raise MigrationError(f"L'autore di r{record.revision} contiene caratteri non rappresentabili in un'identità Git.")
    try:
        date = datetime.fromisoformat(record.date.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("data senza timezone")
        stamp = int(date.astimezone(timezone.utc).timestamp())
    except (ValueError, OverflowError) as exc:
        raise MigrationError(f"La revisione r{record.revision} non ha una data SVN valida.") from exc
    return f"{record.author} <{record.author}@{source_uuid}> {stamp} +0000".encode("utf-8")


def _write_native_data(destination, data: bytes) -> None:
    destination.write(f"data {len(data)}\n".encode("ascii"))
    destination.write(data)
    destination.write(b"\n")


def _normalize_native_eol(source: Path, destination: Path, check_cancelled) -> None:
    """Riporta a LF il contenuto SVN testuale, senza caricarlo interamente in RAM."""
    pending_cr = b""
    with source.open("rb") as reader, destination.open("wb") as writer:
        while True:
            check_cancelled()
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            chunk = pending_cr + chunk
            pending_cr = b"\r" if chunk.endswith(b"\r") else b""
            if pending_cr:
                chunk = chunk[:-1]
            writer.write(chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
        if pending_cr:
            writer.write(b"\n")


def _write_native_file(host, destination, file: _NativeFile, scratch: Path) -> None:
    host.runner.check_cancelled()
    source = file.path
    # svn cat -r BASE legge la copia locale di riferimento senza scaricare ogni
    # file. --ignore-keywords evita l'espansione effettuata dal checkout.
    if file.special or file.eol or file.keywords:
        source = scratch / "content.raw"
        host._svn("cat", "--ignore-keywords", "-r", "BASE", str(file.path) + "@",
                  output_bytes_file=source, log_command=False)
    offset = 0
    if file.special:
        with source.open("rb") as handle:
            if handle.read(5) != b"link ":
                raise MigrationError(
                    f"svn:special non rappresenta un link simbolico supportato: {file.relative}."
                )
        offset = 5
    elif file.eol:
        normalized = scratch / "content.normalized"
        _normalize_native_eol(source, normalized, host.runner.check_cancelled)
        source = normalized
    size = source.stat().st_size - offset
    destination.write(b"M " + file.mode.encode("ascii") + b" inline "
                      + _quote_native_git_path(file.relative) + b"\n")
    destination.write(f"data {size}\n".encode("ascii"))
    with source.open("rb") as handle:
        handle.seek(offset)
        remaining = size
        while remaining:
            host.runner.check_cancelled()
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise MigrationError(f"Il file {file.relative} è cambiato durante l'importazione.")
            destination.write(chunk)
            remaining -= len(chunk)
        if handle.read(1):
            raise MigrationError(f"Il file {file.relative} è cambiato durante l'importazione.")
    destination.write(b"\n")


def _check_native_working_copy(host, wc: Path, source_uuid: str,
                               revision: int | None = None, *,
                               expected_url: str | None = None,
                               allowed_urls: tuple[str, ...] = ()) -> None:
    result = host._svn("info", "--xml", str(wc) + "@", log_command=False)
    try:
        info = ET.fromstring(result.stdout).find("entry")
        if info is None:
            raise ValueError("entry assente")
        actual_url = _canonical_repository_url(info.findtext("url") or "")
        valid_urls = allowed_urls or (expected_url or host.config.svn_url,)
        if (actual_url not in {_canonical_repository_url(url) for url in valid_urls}
                or info.findtext("repository/uuid") != source_uuid):
            raise MigrationError("La working copy privata proviene da un URL o repository SVN differente.")
        if revision is not None and int(info.attrib["revision"]) != revision:
            raise MigrationError("La working copy SVN non è arrivata alla revisione richiesta.")
    except (ET.ParseError, ValueError, KeyError) as exc:
        raise MigrationError("Identità della working copy SVN non verificabile.") from exc


def _prepare_native_working_copy(host, wc: Path, revision: int, source_uuid: str, *,
                                 source_url: str | None = None,
                                 allowed_urls: tuple[str, ...] = (),
                                 switch: bool = False) -> None:
    source_url = source_url or host.config.svn_url
    if (wc / ".svn").is_dir():
        # Un'interruzione può lasciare lock locali SVN: cleanup non scarta
        # modifiche, non cancella file non versionati e non tocca il server.
        host._svn("cleanup", str(wc), log_command=False)
        _check_native_working_copy(host, wc, source_uuid, expected_url=source_url,
                                   allowed_urls=allowed_urls)
        if switch:
            # Al primo passo della ripresa e a ogni cambio di incarnazione si
            # seleziona esplicitamente URL@R. Un semplice update del ramo
            # attuale non può materializzare correttamente i suoi antenati.
            # Il controllo precede lo switch: nessuna modifica manuale viene
            # unita o sostituita durante il cambio di percorso.
            _native_snapshot(host, wc)
            host._svn("switch", "--ignore-ancestry", "--ignore-externals",
                      "--accept", "postpone", "--set-depth", "infinity",
                      "-r", str(revision), source_url + "@" + str(revision),
                      str(wc), stream=True)
        else:
            host._svn("update", "--ignore-externals", "--accept", "postpone",
                      "--set-depth", "infinity", "-r", str(revision), str(wc), stream=True)
    elif wc.exists() and any(wc.iterdir()):
        raise MigrationError(
            "La working copy privata esiste ma non è una copia SVN valida. "
            f"I file sono conservati per la verifica: {wc}."
        )
    else:
        host._svn("checkout", "--ignore-externals", "--depth", "infinity", "-r", str(revision),
                  source_url + "@" + str(revision), str(wc), stream=True)
    _check_native_working_copy(host, wc, source_uuid, revision, expected_url=source_url)


def import_native(host, analysis, imported: tuple[int, ...]) -> None:
    """Importa ciascuna revisione pertinente; nessuna operazione di push."""
    pending = analysis.revisions[len(imported):]
    if not pending:
        return
    records = _load_native_revisions(host.workspace / "branch-log.xml", analysis.revisions)
    lineage = getattr(analysis, "history_scope", "path") == "lineage"
    locations = {location.revision: location for location in getattr(analysis, "locations", ())}
    if lineage and tuple(sorted(locations)) != analysis.revisions:
        raise MigrationError("La mappa dei percorsi storici SVN è incompleta. Rieseguire Verifica.")
    allowed_urls = tuple(sorted({location.url for location in locations.values()})) if lineage else ()
    wc = host.workspace / "svn-working-copy"
    scratch = host.workspace / "native-temporary"
    scratch.mkdir(exist_ok=True)
    stream_path = scratch / "commit.fast-import"
    previous: dict[str, _NativeFile] | None = None
    previous_segment: int | None = None
    head = host._ref_sha(NATIVE_REFERENCE)
    host._log(
        f"Motore nativo SVN + Git: {len(imported)} revisioni già importate, "
        f"{len(pending)} revisioni da elaborare. Le revisioni estranee vengono saltate."
    )
    host._log("La working copy SVN è privata. I file externals non vengono importati.")
    try:
        for index, revision in enumerate(pending, len(imported) + 1):
            host.runner.check_cancelled()
            host._stage(f"Importazione nativa {index}/{len(analysis.revisions)}: r{revision}")
            location = locations.get(revision) if lineage else None
            source_url = location.url if location is not None else host.config.svn_url
            source_path = location.repository_path if location is not None else analysis.branch_path
            segment = location.segment if location is not None else 0
            change_segment = previous_segment != segment
            if lineage and change_segment:
                host._log(f"Percorso storico a r{revision}: {source_path}")
                previous = None
            _prepare_native_working_copy(host, wc, revision, analysis.source_uuid,
                                         source_url=source_url, allowed_urls=allowed_urls,
                                         switch=lineage and change_segment)
            snapshot = _native_snapshot(host, wc)
            record = records[revision]
            changed = _changed_native_prefixes(record.changed_paths, source_path)
            identity = _native_identity(record, analysis.source_uuid)
            marker = f"git-svn-id: {source_url}@{revision} {analysis.source_uuid}\n"
            message = record.message + ("\n" if record.message.endswith("\n") else "\n\n") + marker
            with stream_path.open("wb") as output:
                output.write(b"commit " + NATIVE_REFERENCE.encode("ascii") + b"\n")
                output.write(b"author " + identity + b"\ncommitter " + identity + b"\n")
                _write_native_data(output, message.encode("utf-8"))
                if head:
                    output.write(b"from " + head.encode("ascii") + b"\n")
                if previous is None:
                    # Alla ripresa si ricostruisce uno snapshot completo: nessuna
                    # cache persistente può rendere incompleta la revisione.
                    output.write(b"deleteall\n")
                else:
                    for relative in sorted(previous.keys() - snapshot.keys()):
                        output.write(b"D " + _quote_native_git_path(relative) + b"\n")
                writes = 0
                for relative, file in sorted(snapshot.items()):
                    before = previous.get(relative) if previous is not None else None
                    if before is not None and before.signature == file.signature and not _native_path_changed(relative, changed):
                        continue
                    _write_native_file(host, output, file, scratch)
                    writes += 1
                output.write(b"\ndone\n")
                output.flush()
                os.fsync(output.fileno())
            host._git("fast-import", "--quiet", "--done", "--date-format=raw",
                      input_file=stream_path, stream=True)
            head = host._ref_sha(NATIVE_REFERENCE)
            if not head:
                raise MigrationError(f"Git non ha creato il commit della revisione r{revision}.")
            previous = snapshot
            previous_segment = segment
            host._log(f"r{revision} = {head[:12]} · {writes} file scritti")
            host.emit({"type": "progress", "current": index, "total": len(analysis.revisions)})
            host.runner.check_cancelled()
    finally:
        # Solo file temporanei creati dal motore; la working copy e i commit
        # completati rimangono disponibili dopo errori o interruzioni.
        for name in ("commit.fast-import", "content.raw", "content.normalized"):
            try:
                (scratch / name).unlink()
            except FileNotFoundError:
                pass


"""Interfaccia desktop italiana. Nessuna dipendenza Python di terze parti."""

pass

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

pass
pass


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
