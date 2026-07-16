import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_web import server


class GitCheckpointV2Test(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.backups = Path(self.temp_dir.name) / "checkpoints"
        self.repo.mkdir()
        self.backups.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.checkpoint_dir_patch = patch.object(server, "CHECKPOINT_DIR", self.backups)
        self.checkpoint_dir_patch.start()

    async def asyncTearDown(self):
        self.checkpoint_dir_patch.stop()
        self.temp_dir.cleanup()

    async def test_restores_tracked_and_untracked_without_git_clean_data_loss(self):
        (self.repo / "tracked.txt").write_text("before turn\n", encoding="utf-8")
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "draft.txt").write_text("original untracked\n", encoding="utf-8")
        checkpoint = await server.create_git_checkpoint(str(self.repo))
        self.assertEqual("git-v2", checkpoint["type"])
        self.assertTrue((self.backups / checkpoint["id"] / "manifest.json").exists())
        ref = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", checkpoint["ref"]],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, ref.returncode)

        (self.repo / "tracked.txt").write_text("after turn\n", encoding="utf-8")
        (self.repo / "notes" / "draft.txt").write_text("overwritten\n", encoding="utf-8")
        (self.repo / "created-by-turn.txt").write_text("new\n", encoding="utf-8")

        self.assertTrue(await server.restore_git_checkpoint(str(self.repo), checkpoint))
        self.assertEqual("before turn\n", (self.repo / "tracked.txt").read_text(encoding="utf-8"))
        self.assertEqual("original untracked\n", (self.repo / "notes" / "draft.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / "created-by-turn.txt").exists())
        await server.discard_git_checkpoint(checkpoint, str(self.repo))

    async def test_failed_restore_rolls_back_to_click_time_state(self):
        (self.repo / "tracked.txt").write_text("checkpoint state\n", encoding="utf-8")
        checkpoint = await server.create_git_checkpoint(str(self.repo))
        (self.repo / "tracked.txt").write_text("state when rollback clicked\n", encoding="utf-8")
        real_apply = server._apply_git_checkpoint
        calls = 0

        async def fail_target_then_restore_safety(cwd, value):
            nonlocal calls
            calls += 1
            if calls == 1:
                (self.repo / "tracked.txt").write_text("partially destroyed\n", encoding="utf-8")
                return False
            return await real_apply(cwd, value)

        with patch.object(server, "_apply_git_checkpoint", side_effect=fail_target_then_restore_safety):
            self.assertFalse(await server.restore_git_checkpoint(str(self.repo), checkpoint))
        self.assertEqual("state when rollback clicked\n", (self.repo / "tracked.txt").read_text(encoding="utf-8"))
        await server.discard_git_checkpoint(checkpoint, str(self.repo))


if __name__ == "__main__":
    unittest.main()
