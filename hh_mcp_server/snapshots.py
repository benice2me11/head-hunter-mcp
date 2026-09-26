"""Material fields only: UI counters and HTML styling are not resume/job versions."""
import copy
from html.parser import HTMLParser

from hh_mcp_server.drafts import content_digest


def normalized_text(value):
    return ' '.join((value or '').split())


def normalized_label(value):
    return normalized_text(''.join(c if c.isalnum() or c in '+#' else ' '
                                   for c in normalized_text(value).casefold()))


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {'p', 'div', 'li', 'ul', 'ol', 'br', 'h1', 'h2', 'h3', 'table', 'tr', 'td'}:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        self.handle_starttag(tag, [])


def snapshot(fields):
    return {'schema_version': 1, 'fields': fields, 'sha256': content_digest(fields)}


def vacancy_snapshot(vacancy):
    parser = PlainHTML()
    parser.feed(vacancy.get('description') or '')
    description = normalized_text(''.join(parser.parts))
    employer = vacancy.get('employer') or {}
    if not vacancy.get('title') or not employer.get('name') or not description:
        raise ValueError('Incomplete vacancy content; cannot establish a material version.')
    fields = {'title': normalized_label(vacancy['title']),
              'employer': {'name': normalized_label(employer['name']), 'id': employer.get('id')},
              'description': description,
              'skills': sorted(set(normalized_text(s) for s in vacancy.get('skills', [])))}
    for name in ('salary', 'experience', 'employment', 'work_format', 'schedule',
                 'working_hours', 'hiring_formats', 'payment_frequency', 'address'):
        fields[name] = normalized_text(vacancy.get(name)) or None
    return snapshot(fields)


def resume_snapshot(resume, title):
    lines = [normalized_text(line) for line in resume['raw_text'].splitlines()]
    lines = [line for line in lines if line]
    title = normalized_text(title)
    pairs = [i for i in range(len(lines) - 1) if lines[i:i + 2] == [title, title]]
    required = ['Контакты', 'Навыки', 'Образование', 'Подтверждение навыков', 'О себе', 'По-русски']
    positions = {}
    for label in required:
        if lines.count(label) != 1:
            raise ValueError('Unrecognized resume sections; review the full content before sending.')
        positions[label] = lines.index(label)
    experience = [i for i, line in enumerate(lines) if line.startswith('Опыт работы:')]
    if len(pairs) != 1 or len(experience) != 1:
        raise ValueError('Unrecognized resume title or experience; review before sending.')
    start, exp = pairs[0] + 2, experience[0]
    ordered = [start, positions['Контакты'], exp] + [positions[k] for k in required[1:]]
    if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
        raise ValueError('Resume section order changed; review before sending.')
    controls = {'Редактировать', 'Добавить', 'Развернуть', 'Указать уровни'}

    def section(begin, end):
        result = [line for line in lines[begin:end] if line not in controls]
        if not result:
            raise ValueError('Resume material section is empty; review before sending.')
        return result

    return snapshot({'schema_version': 1, 'title': title,
                     'conditions': section(start, positions['Контакты']),
                     'contacts': section(positions['Контакты'] + 1, exp),
                     'experience': section(exp, positions['Навыки']),
                     'skills': section(positions['Навыки'] + 1, positions['Образование']),
                     'education': section(positions['Образование'] + 1, positions['Подтверждение навыков']),
                     'about': section(positions['О себе'] + 1, positions['По-русски']),
                     'languages': None})


def resume_edit_snapshot(state):
    """Structured version used to gate resume edits.

    Editable fields stay exact so reviewed whitespace and line breaks remain
    material. Protected view fields are already normalized by resume_snapshot.
    """
    required = {'resume_id', 'headline', 'about', 'experience', 'skills', 'protected_material'}
    if not isinstance(state, dict) or not required.issubset(state):
        raise ValueError('Incomplete editable resume state')
    if not isinstance(state['experience'], list) or not isinstance(state['skills'], list):
        raise ValueError('Invalid editable resume collections')
    entry_ids = [entry.get('entry_id') for entry in state['experience'] if isinstance(entry, dict)]
    if len(entry_ids) != len(state['experience']) or len(set(entry_ids)) != len(entry_ids):
        raise ValueError('Experience entry IDs must be complete and unique')
    fields = {
        'resume_id': state['resume_id'],
        'headline': state['headline'],
        'about': state['about'],
        'experience': copy.deepcopy(state['experience']),
        'skills': copy.deepcopy(state['skills']),
        'protected_material': copy.deepcopy(state['protected_material']),
    }
    return snapshot(fields)
