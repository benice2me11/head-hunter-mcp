# HH MCP Server

Локальный MCP-сервер для работы с hh.ru через Playwright. Сервер использует
авторизованную web-сессию пользователя и работает локально через stdio.

Основной репозиторий: [benice2me11/hh-mcp-server](https://github.com/benice2me11/hh-mcp-server).

Проект основан на [iraguzov/hh-mcp-server](https://github.com/iraguzov/hh-mcp-server/tree/8d1b8925126602d93270e7014a0ab02d35e18c36)
и расширен безопасными двухфазными write-операциями: перед изменением резюме или
отправкой отклика сервер сначала формирует точный preview, а запись выполняет
только отдельным подтверждённым вызовом.

## Возможности

- поиск вакансий и просмотр карточки вакансии;
- рекомендации HH для выбранного резюме;
- просмотр работодателя;
- просмотр собственных резюме и истории откликов;
- безопасное редактирование собственного резюме;
- безопасная подготовка и отправка отклика.

Write-операции намеренно fail-closed: неизвестная структура страницы, изменившееся
состояние, неподтверждённый draft или неопределённый результат блокируют дальнейшую
автоматическую запись.

## Требования

- Python 3.12;
- macOS или Linux;
- Playwright Chromium.

Для блокировки одновременного доступа к сессии используется `fcntl`, поэтому Windows
сейчас не является поддерживаемой платформой.

## Установка

```sh
git clone https://github.com/benice2me11/hh-mcp-server.git
cd hh-mcp-server

python3.12 -m venv .venv
.venv/bin/python -m pip --isolated install \
  --index-url https://pypi.org/simple \
  --only-binary=:all: \
  -r requirements.lock

.venv/bin/python -m pip --isolated install \
  --index-url https://pypi.org/simple \
  --no-deps -e .

.venv/bin/python -m playwright install chromium
```

## Авторизация

По умолчанию состояние браузерной сессии хранится в:

```text
~/.hh-mcp/profile/state.json
```

Запустить вход:

```sh
.venv/bin/python -m hh_mcp_server --login
```

Откроется Chromium. Пароль и код подтверждения вводятся только на hh.ru. После
успешной проверки сессия сохраняется локально.

Путь можно переопределить переменной `HH_STATE_FILE` или через приватный env-файл:

```dotenv
HH_STATE_FILE=~/.hh-mcp/profile/state.json
HH_MONITORING_FILE=/absolute/path/to/private/monitoring/vacancies.json
```

```sh
HH_ENV_FILE=/absolute/path/to/private/.env \
  .venv/bin/python -m hh_mcp_server --login
```

`HH_MONITORING_FILE` необязателен. Он используется application workflow для
сопоставления известных aliases/перепубликаций вакансий.

Сессия, drafts и journals содержат приватные данные и не должны попадать в Git.
Каталог профиля создаётся с правами `0700`, приватные файлы — `0600`.

## Подключение MCP

Пример конфигурации:

```toml
[mcp_servers.hh_local]
command = "/absolute/path/to/hh-mcp-server/.venv/bin/python"
args = ["-m", "hh_mcp_server"]
cwd = "/absolute/path/to/hh-mcp-server"
startup_timeout_sec = 30
tool_timeout_sec = 120

enabled_tools = [
  "get_my_resumes",
  "get_resume",
  "prepare_resume_update",
  "apply_resume_update",
  "search_vacancies",
  "get_recommended_vacancies",
  "get_vacancy_details",
  "get_employer_info",
  "get_responses",
  "prepare_application",
  "apply_to_vacancy",
  "close_session",
]

[mcp_servers.hh_local.env]
HH_ENV_FILE = "/absolute/path/to/private/.env"
```

Одновременно сохранённую HH-сессию может использовать только один процесс.

## Редактирование резюме

Редактирование построено как двухфазная операция.

### 1. Прочитать текущее состояние

`get_my_resumes()` возвращает собственные резюме.

`get_resume(resume_id)` возвращает полный текст резюме и структурированный
`editable` с идентификаторами, необходимыми для безопасного редактирования.

### 2. Подготовить изменение

```text
prepare_resume_update(resume_id, changes)
```

Prepare полностью read-only: сетевой guard блокирует
`POST`/`PUT`/`PATCH`/`DELETE`.

Инструмент:

- проверяет, что резюме принадлежит текущему пользователю;
- принимает только поддерживаемые typed changes;
- читает актуальное структурированное состояние;
- показывает exact before/after;
- сохраняет material snapshot;
- создаёт приватный draft и digest.

Если изменение ничего не меняет, возвращается `no_changes` без write-operation.

V1 поддерживает:

```json
{
  "headline": "Golang-разработчик / Backend Engineer",
  "about": "Текст раздела О себе",
  "experience": [
    {
      "entry_id": 123456789,
      "description": "Новое описание"
    }
  ],
  "skills": ["Go", "PostgreSQL", "Docker"],
  "skill_levels": [
    {
      "name": "Go",
      "level": "advanced"
    }
  ]
}
```

Для skill levels поддерживаются `base`, `middle`, `advanced`.

Не входят в V1:

- salary;
- contacts;
- visibility;
- создание или удаление резюме;
- company/position/dates существующего опыта;
- публикация и автоматическое поднятие резюме.

### 3. Применить подтверждённый draft

```text
apply_resume_update(
  draft_id,
  approved_digest,
  confirmed=true
)
```

Перед записью сервер повторно читает HH и проверяет ownership, digest и material
snapshot. Если состояние изменилось после preview, операция блокируется.

Каждая фактическая sub-operation фиксируется в durable journal до передачи запроса.
После записи сервер снова читает резюме и подтверждает ожидаемое состояние.

Возможные важные результаты:

- `success` — все изменения подтверждены read-back;
- `blocked` — запись не была разрешена;
- `unverified` — запрос мог уйти, но результат нельзя надёжно подтвердить;
- `partial_success` — часть независимых изменений подтверждена;
- `reconciled` — состояние сверено после ранее неопределённой попытки.

После uncertain write запрос автоматически не повторяется.

Технические детали исследованного HH UI и transport:
[RESUME_EDIT_RESEARCH.md](RESUME_EDIT_RESEARCH.md).

## Отклики на вакансии

Отклик также использует prepare/confirm workflow.

### 1. Prepare

```text
prepare_application(
  vacancy_id,
  resume_id,
  cover_letter,
  question_answers
)
```

Prepare проверяет выбранное собственное резюме и актуальные условия вакансии,
создаёт material snapshots и возвращает точный draft для просмотра. Отправка
отклика на этом этапе блокируется network guard.

### 2. Confirmation

После явного подтверждения draft клиент должен зафиксировать approval и актуальную
проверку истории в приватном application journal. Одного `confirmed=true` без
этого недостаточно.

### 3. Send

```text
apply_to_vacancy(
  draft_id,
  approved_digest,
  confirmed=true
)
```

Перед отправкой сервер повторно проверяет material state резюме и вакансии.
Исходящий request сверяется с утверждёнными resume ID, vacancy ID, cover letter и
ответами.

Если результат отправки неопределён, сервер не повторяет request автоматически.
Нужна сверка истории через `get_responses` или личный кабинет HH.

Подробности модели безопасности:
[SECURITY_REVIEW.md](SECURITY_REVIEW.md).

## Локальные данные

По умолчанию приватные runtime-файлы находятся рядом с `HH_STATE_FILE`.

Там могут храниться:

- browser storage state;
- resume-update drafts;
- application drafts;
- durable journals.

Не удаляйте journal/draft после неопределённой write-операции только ради повторной
отправки: эти данные используются для reconciliation и защиты от duplicate writes.

## Проверки

Полный test suite:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Проверка окружения:

```sh
.venv/bin/python -m pip check
```

Security/dependency audit:

```sh
.venv/bin/python scripts/security_audit.py \
  security/dependency-audit-2026-09-16.json
```

Тесты используют искусственные данные и перехват сетевых запросов. Они не должны
отправлять реальные отклики или изменять реальные резюме.

## Документация

- [RESUME_EDIT_RESEARCH.md](RESUME_EDIT_RESEARCH.md) — подтверждённые маршруты,
  selectors и transport текущего UI редактирования резюме;
- [SECURITY_REVIEW.md](SECURITY_REVIEW.md) — security model, ограничения и
  результаты аудита.

## Репозиторий

Основная ветка — `master`. Сессии, env-файлы, приватные drafts/journals, `.venv`
и другие локальные данные пользователя не должны коммититься.
