"""Genera la distribuzione in un singolo file e il pacchetto ZIP."""
from __future__ import annotations

import ast
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent


def strip_local_imports(source: str) -> str:
    lines = source.splitlines(keepends=True)
    drop = set()
    replacements = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "__future__", "migration_core", "platform_runtime", "history_lineage", "native_import",
        }:
            drop.update(range(node.lineno - 1, node.end_lineno))
            replacements[node.lineno - 1] = " " * node.col_offset + "pass\n"
    # 'pass' mantiene validi anche eventuali blocchi di import condizionali.
    return "".join(replacements.get(index, "") if index in drop else line
                   for index, line in enumerate(lines))


def main():
    modules = ["platform_runtime.py", "history_lineage.py", "migration_core.py", "native_import.py", "gui.py"]
    contents = [strip_local_imports((ROOT / name).read_text(encoding="utf-8")) for name in modules]
    standalone = ROOT / "svn_git_migrator.py"
    standalone.write_text("#!/usr/bin/env python3\nfrom __future__ import annotations\n\n"
                          + "\n\n".join(contents), encoding="utf-8")
    compile(standalone.read_text(encoding="utf-8"), str(standalone), "exec")
    standalone.chmod(0o755)
    release = ROOT / "svn_git_migrator.zip"
    files = [
        "svn_git_migrator.py", "README.md", "LEGGIMI.md", ".gitignore",
        "AVVIA_WINDOWS.cmd", "AVVIA_WINDOWS_WSL.cmd",
        "avvia.sh", *modules, "build_release.py",
    ]
    for name in ("LICENSE", "LICENSE.txt", "LICENSE.md"):
        if (ROOT / name).is_file():
            files.append(name)
    files += [str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests").glob("test_*.py"))]
    if (ROOT / "VERIFICHE.txt").exists():
        files.append("VERIFICHE.txt")
    with zipfile.ZipFile(release, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(ROOT / relative, "svn_git_migrator/" + relative)
    print(standalone)
    print(release)


if __name__ == "__main__":
    main()
