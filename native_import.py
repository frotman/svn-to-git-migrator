"""Motore SVN CLI → Git, utilizzabile anche senza git-svn su Windows.

Una working copy privata riceve solo le revisioni selezionate. I metadati
locali SVN permettono di riutilizzare i file invariati; i file modificati
entrano in Git con fast-import, senza filtri Git, .gitignore o conversioni
di fine riga. Ogni revisione aggiorna atomicamente il riferimento di import.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from migration_core import MigrationError, _canonical_repository_url


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
