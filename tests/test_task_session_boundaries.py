import unittest
from unittest.mock import patch

from workflows.workflow_batch import BatchGenerationMixin
from web_drivers.browser_pool import WorkflowCancelled


class _BatchHarness(BatchGenerationMixin):
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.saved = []

    def _generate_web(self, title, answer, recipe=None):
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def save_story_file(self, story, index):
        self.saved.append((index, story))
        return f"{index}.md"


def _material(index):
    return {"index": index, "title": f"故事 {index}", "answer": "素材"}


class BatchSessionBoundaryTests(unittest.TestCase):
    def test_generate_resets_after_each_material_even_when_one_fails(self):
        harness = _BatchHarness(["x" * 500, RuntimeError("boom")])
        materials = [_material(1), _material(2)]

        with patch("web_drivers.reset_driver") as reset:
            harness._batch_generate_web_serial(materials)

        self.assertEqual(reset.call_count, 2)
        self.assertEqual(materials[0]["story"], "x" * 500)
        self.assertIsNone(materials[1]["story"])

    def test_generate_resets_when_cancelled(self):
        harness = _BatchHarness([KeyboardInterrupt()])

        with patch("web_drivers.reset_driver") as reset:
            with self.assertRaises(KeyboardInterrupt):
                harness._batch_generate_web_serial([_material(1)])

        reset.assert_called_once_with(delete_session=True)

    def test_generate_resets_when_workflow_is_cancelled(self):
        harness = _BatchHarness([WorkflowCancelled("cancel")])
        materials = [_material(1), _material(2)]

        with patch("web_drivers.reset_driver") as reset:
            with self.assertRaises(WorkflowCancelled):
                harness._batch_generate_web_serial(materials)

        reset.assert_called_once_with(delete_session=True)
        self.assertNotIn("story", materials[1])

    def test_retry_resets_after_each_material_even_when_one_fails(self):
        harness = _BatchHarness([RuntimeError("boom"), RuntimeError("stop")])
        materials = [_material(1), _material(2)]

        with patch("web_drivers.reset_driver") as reset:
            self.assertEqual(
                harness._batch_retry_web_serial(materials, []), 0
            )

        self.assertEqual(reset.call_count, 2)

    def test_retry_resets_when_cancelled(self):
        harness = _BatchHarness([KeyboardInterrupt()])

        with patch("web_drivers.reset_driver") as reset:
            with self.assertRaises(KeyboardInterrupt):
                harness._batch_retry_web_serial([_material(1)], [])

        reset.assert_called_once_with(delete_session=True)

    def test_retry_propagates_workflow_cancellation_and_stops_batch(self):
        harness = _BatchHarness([WorkflowCancelled("cancel")])
        materials = [_material(1), _material(2)]

        with patch("web_drivers.reset_driver") as reset:
            with self.assertRaises(WorkflowCancelled):
                harness._batch_retry_web_serial(materials, [])

        reset.assert_called_once_with(delete_session=True)
        self.assertNotIn("story", materials[1])


if __name__ == "__main__":
    unittest.main()
