"""Reader-contract trials keep their actual generation-time configuration."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import evolution
from workflows.base import WorkflowBase


class ReaderContractTrackingTest(unittest.TestCase):
    def test_generation_records_switch_and_each_new_trial_episode(self):
        with tempfile.TemporaryDirectory() as root:
            local = lambda *parts: str(Path(root, *parts))
            with patch('core.paths.data', local), patch('core.paths.program', local):
                workflow = WorkflowBase()
                for index, enabled in enumerate((True, False, True), 1):
                    with patch('config.story.READER_CONTRACT_ENABLE', enabled, create=True):
                        workflow.save_story_file('原创试验正文 ' + str(index), index=index)
                data = json.loads(Path(local('data', 'state', 'evolution.json')).read_text(encoding='utf-8'))
        generations = data['generations']
        self.assertEqual([g['settings']['READER_CONTRACT_ENABLE'] for g in generations],
                         [True, False, True])
        self.assertEqual(len({g['episode'] for g in generations}), 3)
        self.assertEqual(generations[0]['fingerprint'], generations[2]['fingerprint'])
        self.assertNotEqual(generations[0]['fingerprint'], generations[1]['fingerprint'])
        self.assertTrue(all(g['settings']['WORKFLOW_MODE'] == 'classic' for g in generations))

    def test_explicit_historical_settings_are_not_backfilled(self):
        with patch('config.story.READER_CONTRACT_ENABLE', True, create=True):
            self.assertNotIn('READER_CONTRACT_ENABLE', evolution._settings({'LLM_MODE': 'web'}))
            self.assertIs(evolution._settings({'READER_CONTRACT_ENABLE': False})[
                'READER_CONTRACT_ENABLE'], False)


if __name__ == '__main__':
    unittest.main()
