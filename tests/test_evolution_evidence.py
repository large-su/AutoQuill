import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import evolution, paths


class EvolutionEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(paths, 'DATA_ROOT', str(self.root)),
                        patch.object(paths, 'PROGRAM_ROOT', str(self.root)),
                        patch.object(evolution, '_now', return_value='2026-01-01T12:00:00+08:00')]
        for p in self.patches:
            p.start()
        history = self.root/'core/evolution_history.json'
        history.parent.mkdir()
        history.write_text(json.dumps({'releases': [{'version': '1.0.0', 'tag': 'v1.0.0', 'date': '2026-01-01'}]}))

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def draft(self, body='原创故事', name='a.md', version='1.0.0'):
        with patch.object(evolution, 'VERSION', version):
            evolution.record_generation(name, body)
        return evolution.record_draft('https://www.zhihu.com/question/9', name,
                                      story_text=body, rendered_html='<p>'+body+'</p>')

    def receipt(self, body='原创故事', qid='9', aid='11'):
        return evolution.record_publication({'ok': True, 'qid': qid,
            'url': 'https://www.zhihu.com/question/9/answer/'+aid,
            'draft_content_fingerprint': evolution.content_fingerprint('<p>'+body+'</p>')})

    def observe(self, aid='11'):
        evolution.ingest_snapshot([{'aid': aid, 'publish_date': '2026-01-01',
                                    'metrics': {'阅读': '100', '赞同': '0'}}], '2026-01-08T23:00:00+08:00')

    def test_full_chain_confirms_original_version_only_after_public_observation(self):
        self.draft()
        with patch.object(evolution, 'VERSION', '2.0.0'):
            self.receipt()
            before = evolution.contract(7, as_of='2026-01-08')
            self.assertEqual(before['articles'][0]['attribution_reason'], 'missing_public_snapshot')
            self.observe()
        state = evolution._path().read_bytes()
        result = evolution.contract(7, version='v1.0.0', as_of='2026-01-08')
        article = result['articles'][0]
        self.assertEqual(article['assignment_source'], 'verified_chain')
        self.assertEqual(article['generation_version'], '1.0.0')
        self.assertEqual(result['releases'][0]['metrics']['by_metric']['likes']['median'], 0)
        self.assertEqual(evolution._path().read_bytes(), state)
        self.assertEqual(evolution.contract(version='2.0.0')['articles'], [])

    def test_missing_or_modified_content_stays_candidate_not_confirmed(self):
        self.draft()
        self.receipt('手动改稿')
        self.observe()
        result = evolution.contract(7, as_of='2026-01-08')
        self.assertEqual(result['quality']['candidates'], 1)
        self.assertEqual(result['releases'][0]['metrics']['assigned'], 0)
        self.assertEqual(result['releases'][0]['candidate_metrics']['median'], 0)
        self.assertFalse(result['articles'][0]['version_confirmed'])

    def test_reused_filename_must_match_actual_written_body(self):
        self.draft()
        result = evolution.record_draft('https://www.zhihu.com/question/9', 'a.md',
                                        story_text='changed', rendered_html='<p>changed</p>')
        self.assertIsNone(result)

    def test_same_content_multiple_generations_is_ambiguous(self):
        self.draft()
        self.draft(name='b.md', version='2.0.0')
        self.receipt()
        self.observe()
        article = evolution.contract()['articles'][0]
        self.assertFalse(article['version_confirmed'])
        self.assertIsNone(article['generation_id'])

    def test_conflicting_qid_or_later_receipt_is_excluded(self):
        self.draft()
        self.receipt()
        self.observe()
        self.receipt(body='other', qid='8')
        result = evolution.contract()
        self.assertEqual(result['quality']['conflicts'], 1)
        self.assertEqual(result['articles'][0]['attribution_reason'], 'conflicting_receipt')
        self.assertEqual(result['releases'][0]['metrics']['assigned'], 0)

    def test_manual_clear_survives_replay_and_records_audit(self):
        self.draft()
        self.receipt()
        self.observe()
        evolution.assign_article('11', None)
        self.receipt()
        self.observe()
        result = evolution.contract()
        self.assertEqual(result['quality']['manual_cleared'], 1)
        self.assertFalse(result['articles'][0]['version_confirmed'])
        self.assertEqual(len(evolution._load()[0]['assignments']), 1)
        self.assertEqual(len(evolution._load()[0]['publication_receipts']), 1)

    def test_ledger_runtime_version_cannot_be_generation_version(self):
        self.observe()
        ledger = self.root/'data/state/published_topics.jsonl'
        ledger.write_text(json.dumps({'aid': '11', 'version': '1.0.0', 'source': 'manual'}))
        article = evolution.contract()['articles'][0]
        self.assertIsNone(article['generation_version'])
        self.assertEqual(article['ledger_version_hint'], '1.0.0')
        self.assertFalse(article['version_confirmed'])

    def test_missing_answer_url_is_retained_and_dry_run_not_recorded(self):
        result = {'ok': True, 'qid': '9', 'url': 'https://www.zhihu.com/question/9'}
        evolution.record_publication(dict(result, dry_run=True))
        self.assertEqual(evolution.contract()['quality']['missing_answer_receipts'], 0)
        evolution.record_publication(result)
        evolution.record_publication(result)
        self.assertEqual(evolution.contract()['quality']['missing_answer_receipts'], 1)

    def test_content_fingerprint_preserves_actual_text_and_decodes_entities(self):
        self.assertEqual(evolution.content_fingerprint('<p>甲 &amp; 乙</p>'),
                         evolution.content_fingerprint('<div>甲&amp;乙</div>'))
        self.assertNotEqual(evolution.content_fingerprint('甲乙'), evolution.content_fingerprint('甲丙'))
        self.assertIsNone(evolution.content_fingerprint('<p> </p>'))

    def test_cutoff_excludes_future_public_evidence_and_reports_missing_metrics(self):
        self.draft()
        self.receipt()
        self.observe()
        before = evolution.contract(7, as_of='2026-01-07')
        self.assertEqual(before['quality']['confirmed'], 0)
        after = evolution.contract(7, as_of='2026-01-09')
        self.assertEqual(after['quality']['stale_days'], 1)
        self.assertIsNone(after['quality']['window_metrics']['by_metric']['comments']['median'])
        self.assertIn('followers', after['quality']['unavailable'])


if __name__ == '__main__':
    unittest.main()
