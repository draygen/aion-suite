import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import repair
from repair import (
    RepairChange,
    RepairRecipe,
    handle_repair_command,
    propose_repair_for_failure,
)


class TestRepairWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "sample.py").write_text("value = 'old'\n", encoding="utf-8")
        (self.root / "test_sample.py").write_text("# focused tests\n", encoding="utf-8")
        self.lesson_path = self.root / "data" / "repair_lessons.jsonl"
        self.recipe = RepairRecipe(
            recipe_id="demo_local_repair",
            title="Repair a known local failure",
            diagnosis="A known project-local value is stale.",
            failure_patterns=(r"known crash",),
            tool_ids=("demo_tool",),
            relevant_paths=("sample.py", "test_sample.py"),
            changes=(RepairChange("sample.py", "value = 'old'", "value = 'new'"),),
            verification_targets=(),
        )
        self.old_root = repair._PROJECT_ROOT_OVERRIDE
        self.old_lessons = repair._LESSONS_PATH_OVERRIDE
        self.old_recipes = repair._RECIPES
        repair._PROJECT_ROOT_OVERRIDE = self.root
        repair._LESSONS_PATH_OVERRIDE = self.lesson_path
        repair._RECIPES = (self.recipe,)
        repair._PENDING.clear()

    def tearDown(self):
        repair._PROJECT_ROOT_OVERRIDE = self.old_root
        repair._LESSONS_PATH_OVERRIDE = self.old_lessons
        repair._RECIPES = self.old_recipes
        repair._PENDING.clear()
        self.temp_dir.cleanup()

    def _propose(self):
        return propose_repair_for_failure(
            "known crash",
            tool_id="demo_tool",
            username="local-user",
            session_id="local-session",
        )

    def _token(self):
        return repair._PENDING[("local-user", "local-session")]["token"]

    def test_proposal_is_read_only_until_explicit_approval(self):
        outcome = self._propose()

        self.assertIsNotNone(outcome)
        self.assertTrue(outcome.staged)
        self.assertIn("Proposed minimal patch (not applied)", outcome.response)
        self.assertEqual((self.root / "sample.py").read_text(encoding="utf-8"), "value = 'old'\n")
        self.assertFalse(self.lesson_path.exists())

    def test_matching_approval_applies_exact_change_and_records_minimal_lesson(self):
        self._propose()
        token = self._token()

        outcome = handle_repair_command(
            f"repair approve {token}",
            username="local-user",
            session_id="local-session",
        )

        self.assertIn("Applied the approved repair", outcome.response)
        self.assertEqual((self.root / "sample.py").read_text(encoding="utf-8"), "value = 'new'\n")
        lesson = json.loads(self.lesson_path.read_text(encoding="utf-8").strip())
        self.assertEqual(lesson["outcome"], "approved")
        self.assertEqual(lesson["result"], "applied")
        self.assertEqual(lesson["changed_files"], ["sample.py"])
        self.assertNotIn("local-user", json.dumps(lesson))
        self.assertNotIn("known crash", json.dumps(lesson))

    def test_rejection_changes_nothing_and_records_outcome(self):
        self._propose()
        token = self._token()

        outcome = handle_repair_command(
            f"repair reject {token}",
            username="local-user",
            session_id="local-session",
        )

        self.assertIn("Repair rejected", outcome.response)
        self.assertEqual((self.root / "sample.py").read_text(encoding="utf-8"), "value = 'old'\n")
        lesson = json.loads(self.lesson_path.read_text(encoding="utf-8").strip())
        self.assertEqual(lesson["outcome"], "rejected")
        self.assertEqual(lesson["result"], "not_applied")

    def test_wrong_token_cannot_apply_repair(self):
        self._propose()

        outcome = handle_repair_command(
            "repair approve 000000",
            username="local-user",
            session_id="local-session",
        )

        self.assertIn("does not match", outcome.response)
        self.assertEqual((self.root / "sample.py").read_text(encoding="utf-8"), "value = 'old'\n")
        self.assertFalse(self.lesson_path.exists())

    def test_out_of_project_change_is_blocked_at_proposal_time(self):
        repair._RECIPES = (
            RepairRecipe(
                recipe_id="outside",
                title="Outside",
                diagnosis="Outside path.",
                failure_patterns=(r"known crash",),
                tool_ids=("demo_tool",),
                relevant_paths=("sample.py",),
                changes=(RepairChange("../outside.py", "old", "new"),),
                verification_targets=(),
            ),
        )

        outcome = self._propose()

        self.assertIsNotNone(outcome)
        self.assertFalse(outcome.staged)
        self.assertIn("No changes were made", outcome.response)
        self.assertFalse(repair._PENDING)

    def test_failed_verification_rolls_back_approved_change(self):
        self._propose()
        token = self._token()

        with patch("repair._run_verification", return_value=(False, "exit_1")):
            outcome = handle_repair_command(
                f"repair approve {token}",
                username="local-user",
                session_id="local-session",
            )

        self.assertIn("restored", outcome.response)
        self.assertEqual((self.root / "sample.py").read_text(encoding="utf-8"), "value = 'old'\n")
        lesson = json.loads(self.lesson_path.read_text(encoding="utf-8").strip())
        self.assertEqual(lesson["result"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
