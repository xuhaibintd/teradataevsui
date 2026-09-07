"""Rename contracts: new public names without invalidating runtime data."""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.settings import Settings
from app.runtime import AUTH_DATABASE_FILE_DEFAULT, SESSION_COOKIE_NAME

ROOT = Path(__file__).resolve().parents[1]


class ProjectIdentityTests(unittest.TestCase):
    def test_package_and_cli_names(self):
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        project = metadata.split("[project]\n", 1)[1].split("\n[", 1)[0]
        self.assertRegex(project, r'(?m)^name = "teradataevsui"$')
        scripts = metadata.split("[project.scripts]\n", 1)[1].split("\n[", 1)[0]
        entries = dict(re.findall(r'(?m)^([\w-]+) = "([^"]+)"$', scripts))
        for command in ("db", "ops"):
            self.assertEqual(entries[f"teradataevsui-{command}"], entries[f"evsui-{command}"])
        self.assertNotIn("teradataevsui-worker", entries)
        self.assertNotIn("evsui-worker", entries)

    def test_custom_windows_lifecycle_wrappers_are_not_shipped(self):
        obsolete = [
            *(ROOT / f"{action}.cmd" for action in ("start", "stop", "restart", "status")),
            ROOT / "scripts/evsui.ps1",
            ROOT / "scripts/teradataevsui.ps1",
            ROOT / "scripts/service_control.py",
            ROOT / "app/worker.py",
            ROOT / "app/core/process_lock.py",
        ]
        self.assertFalse([path for path in obsolete if path.exists()])

    def test_existing_database_key_and_cookie_names_are_preserved(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env(project_dir=ROOT)
        self.assertEqual(settings.database_path, AUTH_DATABASE_FILE_DEFAULT)
        self.assertEqual(settings.credential_key_file, ROOT / "data/evsui.credentials.key")
        self.assertEqual(SESSION_COOKIE_NAME, "evsui_sid")

    def test_fresh_install_examples_default_to_browser_admin_setup(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertRegex(env_example, r"(?m)^EVSUI_BOOTSTRAP_ADMIN=$")
        self.assertRegex(env_example, r"(?m)^EVSUI_BOOTSTRAP_PASSWORD=$")
        self.assertIn('EVSUI_BOOTSTRAP_ADMIN: "${EVSUI_BOOTSTRAP_ADMIN:-}"', compose)
        self.assertIn('EVSUI_BOOTSTRAP_PASSWORD: "${EVSUI_BOOTSTRAP_PASSWORD:-}"', compose)
        self.assertNotIn("EVSUI_BOOTSTRAP_ADMIN:-admin", compose)
        self.assertNotIn("EVSUI_BOOTSTRAP_PASSWORD:?", compose)
        installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
        installation_ja = (ROOT / "docs" / "installation_ja.md").read_text(encoding="utf-8")
        for required in (
            "## 2. Windows PowerShell installation",
            "## 3. Linux x86-64 installation",
            "uv python install 3.11",
            "uv sync --locked --no-dev",
            "## 5. Docker Compose installation",
            "There is no initial or default password.",
        ):
            self.assertIn(required, installation)
        self.assertIn("## 2. Windows PowerShell でのインストール", installation_ja)
        self.assertIn("## 3. Linux x86-64 でのインストール", installation_ja)


if __name__ == "__main__":
    unittest.main()
