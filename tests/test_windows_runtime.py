"""Scelta dei client Windows e condivisione config senza toccare credenziali."""

import ntpath
import unittest
from unittest.mock import patch

from platform_runtime import discover_runtime


class WindowsRuntimeTests(unittest.TestCase):
    def discover(self, files, environment=None, registry=([], [])):
        names = {ntpath.normcase(path) for path in files}
        env = {
            "Path": r"C:\Windows\System32",
            "ProgramFiles": r"C:\Program Files",
            "LOCALAPPDATA": r"C:\Users\ExampleUser\AppData\Local",
            "APPDATA": r"C:\Users\ExampleUser\AppData\Roaming",
        }
        env.update(environment or {})
        with patch("platform_runtime.os.path.isfile",
                   side_effect=lambda path: ntpath.normcase(path) in names), \
                patch("platform_runtime._registry_install_dirs", return_value=registry):
            result = discover_runtime(env, windows=True)
        return result, env

    def test_path_selection_wins_and_global_environment_is_unchanged(self):
        runtime, original = self.discover([
            r"D:\Preferred\git.exe", r"D:\Preferred\svn.exe",
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files\TortoiseSVN\bin\svn.exe",
        ], {"Path": r'D:\Preferred;C:\Windows\System32',
            "GIT_SSH": r"D:\Tools\plink.exe", "SSH_AUTH_SOCK": "agent",
            "GIT_CONFIG_GLOBAL": r"D:\Profile\.gitconfig"})
        self.assertEqual(runtime.git_executable, r"D:\Preferred\git.exe")
        self.assertEqual(runtime.svn_executable, r"D:\Preferred\svn.exe")
        self.assertEqual(runtime.environment, {key.upper(): value for key, value in original.items()})
        self.assertIsNot(runtime.environment, original)
        self.assertEqual(runtime.missing_tools, ())

    def test_finds_git_and_tortoise_without_global_path_edits(self):
        runtime, original = self.discover([
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files\TortoiseSVN\bin\svn.exe",
        ])
        self.assertEqual(original["Path"], r"C:\Windows\System32")
        self.assertIn(r"C:\Program Files\Git\cmd", runtime.environment["PATH"])
        self.assertIn(r"C:\Program Files\TortoiseSVN\bin", runtime.environment["PATH"])
        self.assertNotIn(r"usr\bin", runtime.environment["PATH"])
        self.assertEqual(runtime.svn_config_dir,
                         r"C:\Users\ExampleUser\AppData\Roaming\Subversion")

    def test_custom_registered_installation_and_local_git(self):
        runtime, _ = self.discover([
            r"C:\Users\ExampleUser\AppData\Local\Programs\Git\cmd\git.exe",
            r"D:\Client SVN\bin\svn.exe",
        ], registry=([], [r"D:\Client SVN"]))
        self.assertEqual(runtime.svn_executable, r"D:\Client SVN\bin\svn.exe")
        self.assertEqual(runtime.git_executable,
                         r"C:\Users\ExampleUser\AppData\Local\Programs\Git\cmd\git.exe")

    def test_argv_preserves_spaces_and_explicit_config_and_sentinel(self):
        runtime, _ = self.discover([
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files\TortoiseSVN\bin\svn.exe",
        ])
        url = "https://svn.example/repo/branches/one%20two@123"
        argv = runtime.command(["svn", "info", "--", url])
        self.assertEqual(argv, [runtime.svn_executable, "--config-dir",
                                runtime.svn_config_dir, "info", "--", url])
        explicit = runtime.command(["svn", "info", "--config-dir=D:/Auth", url])
        self.assertEqual(explicit[1:], ["info", "--config-dir=D:/Auth", url])
        git_argv = runtime.command(["git", "svn", "fetch", "-r", "100:200"])
        self.assertEqual(git_argv, [runtime.git_executable, "svn", "fetch",
                                   "--config-dir", runtime.svn_config_dir,
                                   "-r", "100:200"])
        self.assertEqual(runtime.command(["git", "ls-remote", "--", url]),
                         [runtime.git_executable, "ls-remote", "--", url])

    def test_missing_cli_is_not_replaced_with_tortoiseproc(self):
        runtime, _ = self.discover([r"C:\Program Files\TortoiseSVN\bin\TortoiseProc.exe"])
        self.assertEqual(runtime.missing_tools, ("git", "svn"))

    def test_current_directory_and_relative_path_are_not_searched(self):
        runtime, _ = self.discover([r"git.exe", r".\svn.exe", r"tools\git.exe"],
                                   {"Path": r";.\;tools"})
        self.assertEqual(runtime.missing_tools, ("git", "svn"))

    def test_explicit_overrides_retain_existing_client_auth(self):
        runtime, _ = self.discover([r"D:\Tools\git.exe", r"D:\Tools\svn.exe"], {
            "SVNGIT_GIT_EXE": r"D:\Tools\git.exe",
            "SVNGIT_SVN_EXE": r"D:\Tools\svn.exe",
            "SVNGIT_SVN_CONFIG_DIR": r"D:\Existing SVN profile",
        })
        self.assertEqual(runtime.git_executable, r"D:\Tools\git.exe")
        self.assertEqual(runtime.svn_config_dir, r"D:\Existing SVN profile")

    def test_bad_explicit_override_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "SVNGIT_SVN_EXE"):
            self.discover([], {"SVNGIT_SVN_EXE": r"D:\Missing\svn.exe"})
        with self.assertRaisesRegex(ValueError, "SVNGIT_SVN_CONFIG_DIR"):
            self.discover([], {"SVNGIT_SVN_CONFIG_DIR": "relative"})

    def test_git_global_arguments_do_not_hide_svn_or_confuse_its_name(self):
        runtime, _ = self.discover([])
        argv = runtime.command(["git", "-C", "svn", "-c", "core.quotePath=false",
                                "svn", "fetch", "--", "https://example/branch"])
        self.assertEqual(argv, ["git", "-C", "svn", "-c", "core.quotePath=false",
                                "svn", "fetch", "--config-dir", runtime.svn_config_dir,
                                "--", "https://example/branch"])
        for command in (
            ["git", "-C", "svn", "ls-remote", "--", "https://example/svn"],
            ["git", "-c", "alias.custom=svn", "log", "--", "svn"],
            ["git", "-C", "svn"],
            ["git", "--git-dir=svn", "log", "--", "svn"],
        ):
            self.assertEqual(runtime.command(command), command)
        explicit = ["git", "-C", "repo", "svn", "fetch", "--config-dir", "D:/profile"]
        self.assertEqual(runtime.command(explicit), explicit)

    def test_tortoise_registry_directory_may_already_point_to_bin(self):
        runtime, _ = self.discover([r"D:\Tortoise SVN\bin\svn.exe"],
                                   registry=([], [r"D:\Tortoise SVN\bin"]))
        self.assertEqual(runtime.svn_executable, r"D:\Tortoise SVN\bin\svn.exe")

    def test_windows_environment_is_case_insensitive_and_retains_ssh_choice(self):
        runtime, original = self.discover([r"D:\Preferred\git.exe"], {
            "path": r"D:\Preferred", "git_ssh": r"D:\PuTTY\plink.exe",
            "git_ssh_variant": "plink",
        })
        self.assertEqual(runtime.git_executable, r"D:\Preferred\git.exe")
        self.assertEqual(runtime.environment["GIT_SSH"], r"D:\PuTTY\plink.exe")
        self.assertEqual(runtime.environment["GIT_SSH_VARIANT"], "plink")
        self.assertNotIn("GIT_SSH_COMMAND", runtime.environment)
        self.assertIn("git_ssh", original)
        self.assertNotIn("GIT_SSH", original)

    def test_drive_relative_config_is_not_an_absolute_override(self):
        for path in (r"C:relative", r"\current-drive-root"):
            with self.assertRaisesRegex(ValueError, "SVNGIT_SVN_CONFIG_DIR"):
                self.discover([], {"SVNGIT_SVN_CONFIG_DIR": path})


if __name__ == "__main__":
    unittest.main()
