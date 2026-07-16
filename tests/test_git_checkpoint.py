import asyncio
import subprocess
import tempfile
import unittest
import uuid
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

    async def test_turn_diff_preserves_preexisting_dirty_change_and_safe_file_revert(self):
        (self.repo / "tracked.txt").write_text("user work\n", encoding="utf-8")
        checkpoint = await server.create_git_checkpoint(str(self.repo))
        dirty_before = await server.git_dirty_signatures(str(self.repo))
        (self.repo / "tracked.txt").write_text("user work\nAI change\n", encoding="utf-8")

        changed = await server.git_changed_files_since(str(self.repo), dirty_before, checkpoint)
        self.assertEqual(["tracked.txt"], [item["path"] for item in changed])
        self.assertIn("+AI change", changed[0]["diff"])
        self.assertNotIn("-user work", changed[0]["diff"])
        self.assertTrue(changed[0]["revertible"])

        session_id = "review-" + uuid.uuid4().hex
        change_set_id = uuid.uuid4().hex
        server.upsert_session(session_id, "review", str(self.repo), "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server.save_events(session_id, [{
            "type": "result",
            "change_set_id": change_set_id,
            "changed_files": changed,
        }])
        try:
            result = await server.review_code_change(
                session_id,
                server.CodeChangeReviewRequest(
                    change_set_id=change_set_id,
                    path="tracked.txt",
                    action="revert",
                ),
            )
            self.assertTrue(result["ok"])
            self.assertEqual("reverted", result["item"]["review_state"])
            self.assertEqual("user work\n", (self.repo / "tracked.txt").read_text(encoding="utf-8"))
            self.assertEqual(
                "reverted",
                server.load_events(session_id)[0]["changed_files"][0]["review_state"],
            )
        finally:
            server.save_events(session_id, [])
            with server.db_connect() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await server.discard_git_checkpoint(checkpoint, str(self.repo))

    async def test_file_revert_refuses_overlapping_later_edit(self):
        checkpoint = await server.create_git_checkpoint(str(self.repo))
        dirty_before = await server.git_dirty_signatures(str(self.repo))
        (self.repo / "tracked.txt").write_text("AI change\n", encoding="utf-8")
        changed = await server.git_changed_files_since(str(self.repo), dirty_before, checkpoint)
        (self.repo / "tracked.txt").write_text("later user edit\n", encoding="utf-8")

        session_id = "review-conflict-" + uuid.uuid4().hex
        change_set_id = uuid.uuid4().hex
        server.upsert_session(session_id, "review conflict", str(self.repo), "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server.save_events(session_id, [{
            "type": "result",
            "change_set_id": change_set_id,
            "changed_files": changed,
        }])
        try:
            with self.assertRaises(server.HTTPException) as raised:
                await server.review_code_change(
                    session_id,
                    server.CodeChangeReviewRequest(
                        change_set_id=change_set_id,
                        path="tracked.txt",
                        action="revert",
                    ),
                )
            self.assertEqual(409, raised.exception.status_code)
            self.assertEqual("later user edit\n", (self.repo / "tracked.txt").read_text(encoding="utf-8"))
        finally:
            server.save_events(session_id, [])
            with server.db_connect() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await server.discard_git_checkpoint(checkpoint, str(self.repo))

    async def test_preexisting_untracked_file_diff_reverts_to_exact_checkpoint_bytes(self):
        draft = self.repo / "draft.txt"
        draft.write_bytes(b"user draft without newline")
        checkpoint = await server.create_git_checkpoint(str(self.repo))
        dirty_before = await server.git_dirty_signatures(str(self.repo))
        draft.write_bytes(b"user draft without newline\nAI line without newline")
        changed = await server.git_changed_files_since(str(self.repo), dirty_before, checkpoint)

        session_id = "review-untracked-" + uuid.uuid4().hex
        change_set_id = uuid.uuid4().hex
        server.upsert_session(session_id, "review untracked", str(self.repo), "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server.save_events(session_id, [{
            "type": "result",
            "change_set_id": change_set_id,
            "changed_files": changed,
        }])
        try:
            result = await server.review_code_change(
                session_id,
                server.CodeChangeReviewRequest(
                    change_set_id=change_set_id,
                    path="draft.txt",
                    action="revert",
                ),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(b"user draft without newline", draft.read_bytes())
        finally:
            server.save_events(session_id, [])
            with server.db_connect() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await server.discard_git_checkpoint(checkpoint, str(self.repo))

    async def test_empty_untracked_files_have_reversible_mode_aware_patches(self):
        existing = self.repo / "existing-empty.sh"
        existing.touch()
        existing.chmod(0o755)
        checkpoint = await server.create_git_checkpoint(str(self.repo))
        dirty_before = await server.git_dirty_signatures(str(self.repo))
        existing.unlink()
        created = self.repo / "created-empty.txt"
        created.touch()

        changed = await server.git_changed_files_since(str(self.repo), dirty_before, checkpoint)
        by_path = {item["path"]: item for item in changed}
        self.assertIn("deleted file mode 100755", by_path["existing-empty.sh"]["diff"])
        self.assertIn("new file mode 100644", by_path["created-empty.txt"]["diff"])
        self.assertTrue(by_path["existing-empty.sh"]["revertible"])
        self.assertTrue(by_path["created-empty.txt"]["revertible"])

        session_id = "review-empty-files-" + uuid.uuid4().hex
        change_set_id = uuid.uuid4().hex
        server.upsert_session(session_id, "review empty files", str(self.repo), "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server.save_events(session_id, [{
            "type": "result",
            "change_set_id": change_set_id,
            "changed_files": changed,
        }])
        try:
            for path in ("existing-empty.sh", "created-empty.txt"):
                result = await server.review_code_change(
                    session_id,
                    server.CodeChangeReviewRequest(
                        change_set_id=change_set_id,
                        path=path,
                        action="revert",
                    ),
                )
                self.assertTrue(result["ok"])
            self.assertTrue(existing.exists())
            self.assertTrue(existing.stat().st_mode & 0o111)
            self.assertFalse(created.exists())
        finally:
            server.save_events(session_id, [])
            with server.db_connect() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await server.discard_git_checkpoint(checkpoint, str(self.repo))

    async def test_turn_diff_includes_changes_committed_during_the_turn(self):
        checkpoint = await server.create_git_checkpoint(str(self.repo))
        dirty_before = await server.git_dirty_signatures(str(self.repo))
        (self.repo / "tracked.txt").write_text("committed by AI\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "AI turn"], check=True)
        self.assertEqual({}, await server.git_dirty_signatures(str(self.repo)))

        changed = await server.git_changed_files_since(str(self.repo), dirty_before, checkpoint)
        self.assertEqual(["tracked.txt"], [item["path"] for item in changed])
        self.assertIn("+committed by AI", changed[0]["diff"])
        self.assertTrue(changed[0]["revertible"])
        await server.discard_git_checkpoint(checkpoint, str(self.repo))


if __name__ == "__main__":
    unittest.main()
