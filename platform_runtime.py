"""Individuazione dei client locali, senza installazioni o modifiche globali.

Su Windows usa gli eseguibili nativi e la directory di configurazione standard
di Subversion, condivisa con TortoiseSVN. Non legge né copia credenziali.
"""

from __future__ import annotations

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
