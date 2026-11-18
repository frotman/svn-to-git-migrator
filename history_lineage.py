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
from __future__ import annotations

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
