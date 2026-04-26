# Retro: сессия как мета-задача для оркестратора

## Главный вывод

Вся сессия фактически была ручным исполнением одной большой orchestration-задачи:

> Двигай roadmap к Northstar, выбирай следующие issues, запускай worker’ов, проверяй PR, merge’и готовое, заводи blockers, resume’ь после blockers.

Это не обычная implementation task для worker-а. Это responsibility будущего autonomous orchestrator controller.

## Почему задача была плохо декомпозирована

Команда “продолжай делать следующие задачи” на практике включала много разных обязанностей:

- выбрать next issue из roadmap/open issues;
- проверить текущее состояние GitHub issues/PRs;
- запустить worker с правильной моделью;
- дождаться результата;
- проверить PR diff, tests, mergeability;
- решить: merge, retry, comment, split или blocker;
- создать blocker issue при системной проблеме;
- переключиться на blocker;
- после blocker вернуться к original issue;
- поддерживать branch context и state comments;
- не доверять agent summary без проверки фактического diff.

Это workflow/control-plane задача, а не единичная coding задача.

## Что показала сессия

### Worker хорошо справляется с узкими задачами

Codex Spark и другие workers были полезны, когда задача была локальной и хорошо ограниченной:

- обновить docs;
- добавить небольшой шаблон;
- обработать конкретный PR-review comment;
- исправить небольшой isolated behavior.

### Worker хуже справляется с широкими задачами

Широкие задачи вроде #11 и #61 дали плохие результаты:

- #11 Jira tracker support — PR #76 остался scaffold-only;
- #61 presets/retry policy — зависания/timeouts и partial changes;
- review retry не гарантировал meaningful progress.

Вывод: для широких задач нужен decomposition layer, а не просто другой worker.

## Что должен делать оркестратор автоматически

### 1. Task decomposition

Если issue слишком широкая, оркестратор должен разбивать её на sub-issues.

Пример для #11:

1. `--tracker` parser/config only;
2. Jira env validation only;
3. Jira single issue fetch;
4. Jira list/search fetch;
5. branch/commit naming for Jira keys;
6. README/tests.

Пример для #61:

1. presets config only;
2. retry max attempts only;
3. escalation policy only;
4. docs/tests.

### 2. Acceptance-aware validation

Оркестратор не должен считать PR готовым только потому, что PR создан и mergeable.

Нужно проверять:

- changed files against acceptance criteria;
- tests/docs requested by issue;
- whether required files exist in PR diff;
- whether agent-created files остались untracked.

Пример #12:

- Acceptance criteria: `docs/jira-issue-template.md` exists.
- Actual PR #80 files: only `README.md`.
- Оркестратор должен был автоматически пометить PR incomplete.

### 3. Worker output skepticism

Agent summary нельзя считать source of truth.

Нужно сверять summary с фактами:

```bash
gh pr diff <PR> --name-only
git status --short --branch
python3 -m unittest discover -s tests -p 'test_*.py'
```

Если agent говорит “добавил tests”, но PR diff tests не содержит — это failure или blocker.

### 4. Blocker loop

Сессия вручную реализовала blocker loop:

1. Обнаружили, что #12 incomplete.
2. Поняли, что причина системная: new files не staging’ятся.
3. Создали blocker #81.
4. Зафиксировали связь с original #12 / PR #80.
5. Должны после #81 resume’нуть #12.

Это должно быть встроенным поведением оркестратора.

### 5. Safe staging model

Оркестратор должен поддерживать intentional new files без риска случайно закоммитить старый мусор.

Нужная модель:

1. Снять baseline untracked перед agent run.
2. После agent run вычислить новые untracked.
3. В commit включить:
   - tracked modifications/deletions;
   - new untracked files, появившиеся после baseline.
4. Не включать pre-existing untracked.
5. После commit fail/warn, если expected new files остались untracked.

### 6. Retry/split/switch policy

Если worker дважды выдаёт scaffold-only PR или зависает:

- split issue;
- tighten prompt;
- switch model/agent;
- mark `needs-human-review`;
- create blocker if failure is systemic.

Сейчас эти решения принимались вручную.

## GitHub уже почти является state machine

Сессия показала, что GitHub artifacts подходят как orchestration state:

- issues = tasks;
- PRs = implementation artifacts;
- comments = append-only state log;
- labels = failure/blocker markers;
- branches = execution context;
- PR review comments = feedback channel.

Недостаёт controller logic поверх этого.

## Northstar insight

Northstar MVP — это не просто:

> агент пишет код по issue.

Более точная формулировка:

> оркестратор управляет графом задач, PRs, blockers, retries, checks и acceptance criteria до достижения verified outcome.

Worker — это executor. Orchestrator — это controller.

## Практический вывод

Следующие engineering investments должны быть направлены не только на capabilities worker-а, а на orchestration loop:

1. baseline/new untracked tracking;
2. acceptance-aware PR validation;
3. automatic blocker creation and resume;
4. automatic issue splitting for broad tasks;
5. retry/escalation policy;
6. stronger PR-review completion checks;
7. explicit worker trust boundary.

## Summary

Эта сессия была полезна именно потому, что вручную проявила будущий control loop. Почти каждый ручной шаг можно превратить в deterministic orchestration behavior.

Главный урок:

> Нам нужен не только более сильный worker, а более строгий orchestrator, который декомпозирует, проверяет, блокирует, resume’ит и не доверяет agent summary без фактической проверки.
