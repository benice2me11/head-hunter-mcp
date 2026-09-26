"""Read the owner's monitoring data; never rewrite its database."""
import json
import os
from pathlib import Path

from hh_mcp_server import drafts

# A saved draft is not a prior application. The shared journal still blocks
# actual/uncertain dispatch and manual applications regardless of this projection.
UNKNOWN = {'application_status_unknown', 'known_link_now_reviewed_application_status_unknown',
           'draft_prepared_awaiting_approval', 'no_prior_application_found_in_reviewed_history'}


def resolve_opportunity(vacancy_id):
    path = os.environ.get('HH_MONITORING_FILE')
    result = {'opportunity_key': 'hh:' + vacancy_id, 'opportunity_ids': [vacancy_id],
              'known_process': None, 'historical_context': []}
    if not path:
        return result
    state = json.loads(Path(path).read_text(encoding='utf-8'))
    if state.get('schema_version') != 1 or not isinstance(state.get('vacancies'), list):
        raise ValueError('Unsupported monitoring data; review before sending.')
    matches = []
    for card in state['vacancies']:
        if card.get('source') != 'hh.ru':
            continue
        ids = {str(card['source_id'])}
        ids.update(str(a['source_id']) for a in card.get('aliases', []) if a.get('source_id'))
        if vacancy_id in ids:
            matches.append((card, ids))
    if len(matches) > 1:
        raise ValueError('Ambiguous opportunity aliases; reconcile the monitoring database.')
    if matches:
        card, ids = matches[0]
        projection = card.get('process', {})
        process = projection.get('status')
        if projection.get('known_prior_response') or projection.get('automatic_repeat_blocked'):
            process = 'known_prior_response'
        result.update(opportunity_key=card['key'], opportunity_ids=sorted(ids),
                      known_process=process if process and process not in UNKNOWN else None)
        result['historical_context'] = [p for p in state.get('known_processes_without_vacancy_id', [])
                                        if p.get('company', '').casefold() == card.get('company', '').casefold()]
    exact_history = [p for p in state.get('known_processes_without_vacancy_id', [])
                     if str(p.get('source_id')) in result['opportunity_ids']]
    if exact_history:
        result['known_process'] = 'known_historical_process'
    return result


def require_available(opportunity, journal, draft_id=None):
    from hh_mcp_server.ledger import require_no_prior_attempt
    if opportunity.get('known_process'):
        raise ValueError('A prior application or recruiter process is known; review it before any new application.')
    require_no_prior_attempt(journal, opportunity)
    # Also honor older drafts written before the shared journal was introduced.
    if drafts.DRAFT_DIR.exists():
        for path in drafts.DRAFT_DIR.glob('*.json'):
            previous = json.loads(path.read_text(encoding='utf-8'))
            content = previous['content']
            ids = set(content.get('opportunity_ids', [content['vacancy_id']]))
            if ids.intersection(opportunity['opportunity_ids']) and previous['status'] in {
                    'dispatched_unverified', 'success', 'already_applied'}:
                raise ValueError('An earlier draft was dispatched for this opportunity; do not resend.')
