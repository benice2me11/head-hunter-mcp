from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hh_mcp_server import drafts
from hh_mcp_server.ledger import Journal, draft_context, require_no_prior_attempt, require_review
from hh_mcp_server.opportunities import resolve_opportunity, require_available


class OpportunityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.state = self.root / 'monitoring.json'
        self.state.write_text(json.dumps({'schema_version': 1, 'vacancies': [
            {'key': 'hh:123', 'source': 'hh.ru', 'source_id': '123', 'company': 'Fixture',
             'aliases': [{'source_id': '456'}], 'process': {'status': 'application_status_unknown'}}]}))
        self.env = patch.dict(os.environ, {'HH_MONITORING_FILE': str(self.state)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.patch = patch.object(drafts, 'DRAFT_DIR', self.root / 'drafts')
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.opportunity = resolve_opportunity('456')
        self.draft = {'draft_id': 'fixture', 'digest': 'fixture-digest', 'content': {
            'vacancy_id': '456', **self.opportunity}}

    def test_alias_and_second_draft_cannot_bypass_uncertain_dispatch(self):
        with Journal(self.root / 'events.jsonl') as journal:
            journal.append('dispatch_started', {'opportunity_key': 'hh:old-key',
                           'opportunity_ids': ['123'], 'draft_id': 'previous'}, attempt_id='first')
            with self.assertRaisesRegex(ValueError, 'dispatched'):
                require_available(self.opportunity, journal)

    def test_manual_history_and_legacy_draft_block_duplicates(self):
        with Journal(self.root / 'events.jsonl') as journal:
            journal.append('manual_applied', draft_context(self.draft), evidence_ref='fixture:history')
            with self.assertRaisesRegex(ValueError, 'manually'):
                require_available(self.opportunity, journal)
        with Journal(self.root / 'other.jsonl') as journal:
            drafts.DRAFT_DIR.mkdir()
            (drafts.DRAFT_DIR / 'old.json').write_text(json.dumps({
                'content': {'vacancy_id': '123'}, 'status': 'dispatched_unverified'}))
            with self.assertRaisesRegex(ValueError, 'earlier draft'):
                require_available(self.opportunity, journal)

    def test_known_monitoring_application_blocks_only_that_opportunity(self):
        state = json.loads(self.state.read_text())
        state['vacancies'][0]['process']['status'] = 'already_applied'
        self.state.write_text(json.dumps(state))
        with Journal(self.root / 'events.jsonl') as journal:
            with self.assertRaisesRegex(ValueError, 'prior application'):
                require_available(resolve_opportunity('456'), journal)
            require_available(resolve_opportunity('789'), journal)

    def test_prepared_projection_is_not_a_response_but_journal_still_blocks(self):
        state = json.loads(self.state.read_text())
        state['vacancies'][0]['process']['status'] = 'draft_prepared_awaiting_approval'
        self.state.write_text(json.dumps(state))
        with Journal(self.root / 'events.jsonl') as journal:
            opportunity = resolve_opportunity('456')
            require_available(opportunity, journal)
            journal.append('manual_applied', draft_context(self.draft), evidence_ref='fixture:history')
            with self.assertRaisesRegex(ValueError, 'manually'):
                require_available(opportunity, journal)

    def test_explicit_prior_response_overrides_prepared_projection(self):
        state = json.loads(self.state.read_text())
        state['vacancies'][0]['process'] = {
            'status': 'draft_prepared_awaiting_approval', 'known_prior_response': True}
        self.state.write_text(json.dumps(state))
        with Journal(self.root / 'events.jsonl') as journal:
            with self.assertRaisesRegex(ValueError, 'prior application'):
                require_available(resolve_opportunity('456'), journal)

    def test_negative_history_projection_does_not_count_as_a_response_or_consent(self):
        state = json.loads(self.state.read_text())
        state['vacancies'][0]['process']['status'] = 'no_prior_application_found_in_reviewed_history'
        self.state.write_text(json.dumps(state))
        with Journal(self.root / 'events.jsonl') as journal:
            require_available(resolve_opportunity('456'), journal)
            with self.assertRaisesRegex(ValueError, 'approval'):
                require_review(journal, self.draft)

    def test_unfinished_attempt_blocks_but_verified_pre_dispatch_failure_does_not(self):
        with Journal(self.root / 'events.jsonl') as journal:
            journal.append('attempt_started', draft_context(self.draft), attempt_id='one')
            with self.assertRaisesRegex(ValueError, 'unfinished'):
                require_no_prior_attempt(journal, self.opportunity)
            journal.append('attempt_result', draft_context(self.draft), attempt_id='one', data={'status': 'blocked'})
            require_no_prior_attempt(journal, self.opportunity)

    def test_consent_history_coverage_expiry_and_cancellation(self):
        with Journal(self.root / 'events.jsonl') as journal:
            context = draft_context(self.draft)
            with self.assertRaisesRegex(ValueError, 'approval'):
                require_review(journal, self.draft)
            journal.append('approved', context, evidence_ref='fixture:user-consent')
            with self.assertRaisesRegex(ValueError, 'history'):
                require_review(journal, self.draft)
            journal.append('history_checked', context, evidence_ref='fixture:verified-history',
                           data={'result': 'no_prior_response', 'checked_ids': ['456']})
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                require_review(journal, self.draft)
            event = journal.append('history_checked', context, evidence_ref='fixture:verified-history',
                                   data={'result': 'no_prior_response', 'checked_ids': ['123', '456']})
            require_review(journal, self.draft)
            event['occurred_at'] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            with self.assertRaisesRegex(ValueError, 'stale'):
                require_review(journal, self.draft)
            journal.append('cancelled', context, evidence_ref='fixture:user-cancellation')
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                require_review(journal, self.draft)
