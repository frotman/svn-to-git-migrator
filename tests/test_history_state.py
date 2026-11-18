"""Protect existing short-history work and persisted lineage provenance."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from history_lineage import HistoryLocation
from migration_core import Analysis, MigrationConfig, MigrationError, Migrator
from gui import config_dict, workspace_for


class HistoryStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = MigrationConfig(
            "https://svn.example.invalid/repo/branches/release", 12,
            "https://git.example.invalid/project.git", "release",
        )
        self.analysis = Analysis(
            start_revision=5, last_revision=9, revisions=(5, 9), authors=("alice",),
            source_uuid="11111111-2222-3333-4444-555555555555",
            branch_path="/branches/release", history_scope="lineage",
            repository_root_url="https://svn.example.invalid/repo",
            locations=(
                HistoryLocation(5, "/trunk", "https://svn.example.invalid/repo/trunk", 1),
                HistoryLocation(9, "/branches/release", self.config.svn_url, 0),
            ), branch_start_revision=9, copy_boundaries=1,
            copy_from_path="/trunk", copy_from_revision=8,
        )

    def test_new_gui_workspace_is_distinct_and_repeatable(self):
        old_serialized = json.dumps(config_dict(self.config), sort_keys=True, ensure_ascii=False)
        old_key = hashlib.sha256(old_serialized.encode("utf-8")).hexdigest()[:12]
        with patch.object(Path, "home", return_value=self.root):
            new_path = workspace_for(self.config)
            old_path = self.root / "SVN_Git_Migrations" / f"release-{old_key}"
            self.assertNotEqual(old_path, new_path)
            self.assertEqual(workspace_for(self.config), new_path)
        migrator = Migrator(self.config, new_path)
        self.assertEqual((migrator.engine, migrator.history_scope), ("svn-cli", "lineage"))

    def test_old_manifest_and_data_are_preserved(self):
        manifest = self.root / "migration.json"
        data = {"format_version": 1, "config": asdict(self.config), "engine": "svn-cli"}
        manifest.write_text(json.dumps(data), encoding="utf-8")
        saved = manifest.read_bytes()
        completed = self.root / "previous-import.txt"
        completed.write_text("already imported", encoding="utf-8")
        migrator = Migrator(self.config, self.root)
        with self.assertRaisesRegex(MigrationError, "ambito storico diverso"):
            migrator._ensure_manifest()
        self.assertEqual(manifest.read_bytes(), saved)
        self.assertEqual(completed.read_text(encoding="utf-8"), "already imported")
        legacy = Migrator(self.config, self.root, history_scope="path")
        legacy._ensure_manifest()
        self.assertEqual(legacy.engine, "svn-cli")

    def test_saved_mapping_round_trip_and_mismatched_url_rejected(self):
        path = self.root / "analysis.json"
        path.write_text(json.dumps(asdict(self.analysis)), encoding="utf-8")
        loaded = Migrator(self.config, self.root)._load_analysis()
        self.assertEqual(asdict(loaded), asdict(self.analysis))
        broken = replace(self.analysis, locations=(
            replace(self.analysis.locations[0], url="https://svn.example.invalid/repo/wrong"),
            self.analysis.locations[1],
        ))
        path.write_text(json.dumps(asdict(broken)), encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "Analisi salvata non valida"):
            Migrator(self.config, self.root)._load_analysis()

    def test_commit_provenance_uses_each_historical_url(self):
        migrator = Migrator(self.config, self.root)
        sha = "a" * 40
        def output(url):
            return SimpleNamespace(stdout=(f"{sha}\0original message\n\n"
                f"git-svn-id: {url}@5 {self.analysis.source_uuid}\n\0"))
        with patch.object(migrator, "_ref_sha", return_value=sha), patch.object(
                migrator, "_git", return_value=output(self.analysis.locations[0].url)):
            revisions, head = migrator._read_imported("refs/remotes/svn-source",
                                                      self.analysis.source_uuid, self.analysis)
            self.assertEqual((revisions, head), ((5,), sha))
        with patch.object(migrator, "_ref_sha", return_value=sha), patch.object(
                migrator, "_git", return_value=output(self.config.svn_url)):
            with self.assertRaisesRegex(MigrationError, "percorso SVN differente"):
                migrator._read_imported("refs/remotes/svn-source", self.analysis.source_uuid,
                                       self.analysis)
            # The old API without an analysis keeps its original single-path behavior.
            self.assertEqual(migrator._read_imported("refs/remotes/svn-source",
                                                    self.analysis.source_uuid), ((5,), sha))


if __name__ == "__main__":
    unittest.main()
