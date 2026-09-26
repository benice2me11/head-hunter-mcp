"""Private, durable application events shared by all local MCP processes."""
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import uuid

from hh_mcp_server.constants import PROFILE_DIR
from hh_mcp_server.private_files import private_directory

KINDS = {'draft_prepared', 'approved', 'cancelled', 'history_checked', 'manual_applied',
         'attempt_started', 'dispatch_started', 'attempt_result', 'reconciliation'}
DECISIONS = {'approved', 'cancelled', 'history_checked', 'manual_applied', 'reconciliation'}


def draft_context(draft):
    content = draft['content']
    return {'draft_id': draft['draft_id'], 'digest': draft['digest'],
            'opportunity_key': content.get('opportunity_key', 'hh:' + content['vacancy_id']),
            'opportunity_ids': content.get('opportunity_ids', [content['vacancy_id']])}


class Journal(AbstractContextManager):
    def __init__(self, path=None):
        self.path = Path(path) if path else PROFILE_DIR / 'applications.jsonl'
        self.lock_fd = None
        self.handle = None
        self.events = []

    def __enter__(self):
        private_directory(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        try:
            self.lock_fd = os.open(self.path.with_suffix('.lock'), flags, 0o600)
            os.fchmod(self.lock_fd, 0o600)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Application journal is in use; do not bypass its lock.') from None
            fd = os.open(self.path, flags, 0o600)
            os.fchmod(fd, 0o600)
            self.handle = os.fdopen(fd, 'r+', encoding='utf-8')
            seen = set()
            for line in self.handle:
                if not line.endswith('\n'):
                    raise ValueError('Incomplete journal record; reconcile before continuing.')
                event = json.loads(line)
                if (not isinstance(event, dict) or event.get('schema_version') != 1
                        or event.get('kind') not in KINDS or not event.get('event_id')
                        or event['event_id'] in seen or not event.get('opportunity_key')
                        or not isinstance(event.get('opportunity_ids'), list)):
                    raise ValueError('Invalid application journal; reconcile before continuing.')
                seen.add(event['event_id'])
                self.events.append(event)
            self.handle.seek(0, os.SEEK_END)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def append(self, kind, context, *, evidence_ref=None, attempt_id=None, data=None,
               actor='hh_local', event_id=None):
        if self.handle is None or kind not in KINDS:
            raise ValueError('Use a locked application journal and a known event kind.')
        if kind in DECISIONS and (not isinstance(evidence_ref, str) or not evidence_ref.strip()):
            raise ValueError('A decision requires a real user message or verification evidence reference.')
        if not context.get('opportunity_key') or not context.get('opportunity_ids'):
            raise ValueError('An event requires the opportunity key and source IDs.')
        event = {'schema_version': 1, 'event_id': event_id or uuid.uuid4().hex,
                 'occurred_at': datetime.now(timezone.utc).isoformat(), 'kind': kind,
                 'actor': actor, **context, 'evidence_ref': evidence_ref,
                 'attempt_id': attempt_id, 'data': data or {}}
        existing = next((e for e in self.events if e['event_id'] == event['event_id']), None)
        if existing:
            comparable = lambda e: {k: v for k, v in e.items() if k != 'occurred_at'}
            if comparable(existing) != comparable(event):
                raise ValueError('Event ID already exists with different content.')
            return existing
        self.handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n')
        self.handle.flush()
        os.fsync(self.handle.fileno())
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.events.append(event)
        return event

    def __exit__(self, *args):
        try:
            if self.handle is not None:
                self.handle.close()
        finally:
            self.handle = None
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None


def related(events, content):
    ids = set(content['opportunity_ids'])
    return [e for e in events if e['opportunity_key'] == content['opportunity_key']
            or ids.intersection(e['opportunity_ids'])]


def require_review(journal, draft):
    context = draft_context(draft)
    decisions = [e for e in journal.events if e.get('draft_id') == draft['draft_id']]
    if any(e['kind'] == 'cancelled' for e in decisions):
        raise ValueError('Draft cancelled; prepare a new draft if the user changes their decision.')
    if not any(e['kind'] == 'approved' and e.get('digest') == draft['digest']
               and e.get('evidence_ref') for e in decisions):
        raise ValueError('Durable user approval with a message reference is required.')
    history = [e for e in related(journal.events, context) if e['kind'] == 'history_checked']
    if not history:
        raise ValueError('Verified HH and local application history is required before sending.')
    event = history[-1]
    age = datetime.now(timezone.utc) - datetime.fromisoformat(event['occurred_at'])
    if (not 0 <= age.total_seconds() < 86400
            or event['data'].get('result') != 'no_prior_response'
            or not set(context['opportunity_ids']).issubset(event['data'].get('checked_ids', []))
            or not event.get('evidence_ref')):
        raise ValueError('History check is stale, incomplete or does not rule out a prior response.')


def require_no_prior_attempt(journal, content):
    events = related(journal.events, content)
    for event in events:
        if event['kind'] in {'dispatch_started', 'manual_applied'}:
            raise ValueError('Opportunity was dispatched or manually applied; reconcile, do not resend.')
        if event['kind'] == 'attempt_result' and event['data'].get('status') in {'success', 'already_applied', 'unverified'}:
            raise ValueError('Opportunity has a completed or uncertain application; do not resend.')
        if event['kind'] == 'attempt_started':
            outcomes = [e for e in events if e['kind'] == 'attempt_result'
                        and e.get('attempt_id') == event['attempt_id']]
            if not outcomes:
                raise ValueError('Opportunity has an unfinished attempt; reconcile before continuing.')


def main():
    """Record local decisions; this command cannot send an application."""
    import argparse
    from hh_mcp_server.drafts import draft_path, content_digest
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--event-file', type=Path, required=True,
                        help='Private JSON with kind, draft_id, digest, evidence_ref and optional data/event_id')
    args = parser.parse_args()
    event = json.loads(args.event_file.read_text(encoding='utf-8'))
    if event['kind'] not in DECISIONS:
        raise ValueError('Only review decisions can be entered through this command.')
    with Journal() as journal:
        draft = json.loads(draft_path(event['draft_id']).read_text(encoding='utf-8'))
        if event['digest'] != draft['digest'] or content_digest(draft['content']) != draft['digest']:
            raise ValueError('Decision digest does not match the current draft.')
        if event['kind'] in {'approved', 'cancelled'}:
            dispatched = any(e['kind'] == 'dispatch_started' and e.get('draft_id') == draft['draft_id']
                             for e in journal.events)
            if draft['status'] != 'prepared' or dispatched:
                raise ValueError('Draft already dispatched; review the outcome instead of approving/cancelling.')
        journal.append(event['kind'], draft_context(draft), evidence_ref=event.get('evidence_ref'),
                       data=event.get('data'), actor='local-reviewer', event_id=event.get('event_id'))
    print('Private review event recorded. No application request was sent.')


if __name__ == '__main__':
    main()
