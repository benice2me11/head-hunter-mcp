import copy
import unittest

from hh_mcp_server.snapshots import resume_snapshot, vacancy_snapshot

RESUME = {'raw_text': '''Меню
Fixture Resume
Fixture Resume
280 000 RUB net
Редактировать
Контакты
fixture@example.invalid
Опыт работы: 3 года
Добавить
Fixture Employer
Engineer
2023 — 2026
Developed Go and React software
Навыки
Go
React
Редактировать
Образование
Добавить
Fixture University
Computer Science
Подтверждение навыков
Outdated platform tests
О себе
Builds useful software
Развернуть
По-русски
Видимость резюме
Подобрали 1040 вакансий
Можно поднять в 18:32
'''}

VACANCY = {'title': 'Fixture Engineer (Go + React)', 'employer': {'name': 'Fixture Employer', 'id': '1'},
           'salary': '100 RUB net', 'experience': '3 years', 'employment': 'Full time',
           'work_format': 'Remote', 'schedule': '5/2', 'working_hours': '8',
           'hiring_formats': 'Employment contract', 'payment_frequency': 'Twice monthly', 'address': 'Fixture City',
           'description': '<p>Build <b>useful</b> software.</p><p>English B1.</p>', 'skills': ['Go', 'React']}


class SnapshotTests(unittest.TestCase):
    def test_resume_counters_controls_and_whitespace_are_not_material(self):
        changed = {'raw_text': RESUME['raw_text'].replace('1040', '9000').replace('18:32', '23:59')
                   .replace('Go\nReact', 'Go\n\nReact').replace('280 000', '280\u00a0000')}
        self.assertEqual(resume_snapshot(RESUME, 'Fixture Resume'), resume_snapshot(changed, 'Fixture Resume'))

    def test_resume_experience_skills_pay_and_contacts_are_material(self):
        for old, new in [('280 000', '300 000'), ('3 года', '4 года'), ('Go\nReact', 'Python\nReact'),
                         ('fixture@example.invalid', 'different@example.invalid')]:
            with self.subTest(change=old):
                self.assertNotEqual(resume_snapshot(RESUME, 'Fixture Resume'),
                                    resume_snapshot({'raw_text': RESUME['raw_text'].replace(old, new)}, 'Fixture Resume'))

    def test_incomplete_resume_and_vacancy_fail_closed(self):
        with self.assertRaises(ValueError):
            resume_snapshot({'raw_text': 'Log in'}, 'Fixture Resume')
        with self.assertRaises(ValueError):
            vacancy_snapshot({**VACANCY, 'description': ''})

    def test_vacancy_html_counters_order_and_title_punctuation_are_cosmetic(self):
        changed = copy.deepcopy(VACANCY)
        changed.update(title='Fixture Engineer — Go + React', views=2000, published_at='tomorrow',
                       skills=['React', 'Go'], description='<div>Build <strong>useful</strong> software.</div><div>English B1.</div>')
        self.assertEqual(vacancy_snapshot(VACANCY), vacancy_snapshot(changed))

    def test_all_material_terms_change_version(self):
        for field in ['salary', 'experience', 'employment', 'work_format', 'schedule', 'working_hours',
                      'hiring_formats', 'payment_frequency', 'address', 'description']:
            with self.subTest(field=field):
                self.assertNotEqual(vacancy_snapshot(VACANCY), vacancy_snapshot({**VACANCY, field: 'Changed term'}))
