import json
import os
from pathlib import Path
import tempfile
import unittest

from hh_mcp_server.ledger import Journal

CONTEXT = {'opportunity_key': 'hh:123', 'opportunity_ids': ['123']}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'private' / 'applications.jsonl'

    def test_decision_needs_evidence_and_survives_reopen(self):
        with Journal(self.path) as journal:
            with self.assertRaisesRegex(ValueError, 'reference'):
                journal.append('approved', CONTEXT)
            journal.append('approved', CONTEXT, evidence_ref='fixture:user-message')
        with Journal(self.path) as journal:
            self.assertEqual(len(journal.events), 1)
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_competing_writer_is_rejected_without_lost_events(self):
        with Journal(self.path) as first:
            first.append('draft_prepared', CONTEXT)
            with self.assertRaisesRegex(ValueError, 'in use'):
                with Journal(self.path):
                    self.fail('Competing writer acquired lock')
        with Journal(self.path) as second:
            second.append('draft_prepared', CONTEXT)
            self.assertEqual(len(second.events), 2)

    def test_truncated_or_corrupt_journal_fails_closed(self):
        with Journal(self.path):
            pass
        for contents in ['{"partial":', '{}\n', '\n']:
            self.path.write_text(contents)
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                with Journal(self.path):
                    self.fail('Corrupt journal accepted')

    def test_repeated_event_is_idempotent_but_changed_event_rejected(self):
        with Journal(self.path) as journal:
            for _ in range(2):
                journal.append('draft_prepared', CONTEXT, event_id='fixture-event')
            self.assertEqual(len(journal.events), 1)
            with self.assertRaisesRegex(ValueError, 'different content'):
                journal.append('draft_prepared', CONTEXT, event_id='fixture-event', data={'changed': True})

    def test_symlink_target_is_rejected(self):
        self.path.parent.mkdir()
        target = self.path.parent / 'other'
        target.write_text('private')
        try:
            self.path.symlink_to(target)
        except OSError as error:
            self.skipTest(f'Symlinks unavailable on this platform: {error}')
        with self.assertRaises(OSError):
            with Journal(self.path):
                self.fail('Symlink followed')
        self.assertEqual(target.read_text(), 'private')
