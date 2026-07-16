import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_web import agent_sdk_manager


class AgentSdkManagerTest(unittest.TestCase):
    def test_activation_rollback_restores_previous_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sdk"
            root.mkdir()
            (root / "marker").write_text("old", encoding="utf-8")
            staging = Path(temp) / "staging"
            staging.mkdir()
            (staging / "marker").write_text("new", encoding="utf-8")
            with patch.object(agent_sdk_manager, "install_root", return_value=root):
                backup = agent_sdk_manager.activate_staging(staging)
                self.assertEqual("new", (root / "marker").read_text(encoding="utf-8"))
                agent_sdk_manager.rollback_activation(backup)
            self.assertEqual("old", (root / "marker").read_text(encoding="utf-8"))

    def test_first_install_rollback_removes_failed_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sdk"
            staging = Path(temp) / "staging"
            staging.mkdir()
            with patch.object(agent_sdk_manager, "install_root", return_value=root):
                backup = agent_sdk_manager.activate_staging(staging)
                self.assertIsNone(backup)
                self.assertTrue(root.exists())
                agent_sdk_manager.rollback_activation(backup)
            self.assertFalse(root.exists())

    def test_node_version_compatibility_requires_node_18_or_newer(self):
        self.assertTrue(agent_sdk_manager.node_version_compatible("18.0.0"))
        self.assertTrue(agent_sdk_manager.node_version_compatible("22.14.0"))
        self.assertFalse(agent_sdk_manager.node_version_compatible("17.9.1"))
        self.assertFalse(agent_sdk_manager.node_version_compatible(None))
        self.assertFalse(agent_sdk_manager.node_version_compatible("unknown"))

    def test_lock_is_exact_and_status_detects_managed_install(self):
        version = agent_sdk_manager.required_version()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        lock = json.loads(agent_sdk_manager.BRIDGE_PACKAGE_LOCK.read_text(encoding="utf-8"))
        self.assertEqual(
            version,
            lock["packages"]["node_modules/@anthropic-ai/claude-agent-sdk"]["version"],
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sdk"
            package = agent_sdk_manager.installed_package_dir(root)
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
            with patch.object(agent_sdk_manager, "install_root", return_value=root):
                status = agent_sdk_manager.status_payload(
                    {"version": version, "path": str(package)},
                    running=True,
                )
        self.assertTrue(status["installed_compatible"])
        self.assertTrue(status["active_compatible"])
        self.assertEqual("managed", status["active_source"])
        self.assertFalse(status["auto_upgrade"])


if __name__ == "__main__":
    unittest.main()
