# Отличие текущего состояния от North Star

Документ фиксирует разрыв между текущим состоянием репозитория и целевым состоянием из `northstar.md`. Он нужен как ориентир для заведения следующих задач.

## Краткая оценка

Текущее состояние — рабочий Python-прототип GitHub issue/PR runner'а.

Он уже умеет брать GitHub issue, запускать Claude или OpenCode, создавать/переиспользовать ветку и PR, повторно заходить в существующую работу, обрабатывать часть PR review feedback и синхронизировать ветку с base branch.

North Star описывает более широкий продукт: автономный Go-оркестратор, который работает с разными трекерами и code hosting системами, сам выбирает задачи и исполнителей, декомпозирует работу, управляет scope, бюджетами, workflow, CI/review loop, merge/deploy и восстановлением после сбоев.

## Что уже есть

### GitHub issue -> branch -> PR

- Есть основной скрипт: `scripts/run_github_issues_to_opencode.py`.
- Поддерживается GitHub через `gh`.
- Можно обработать одну issue через `--issue` или список issue через `--state` + `--limit`.
- Определяется default branch репозитория.
- Создаётся issue-ветка по шаблону `<branch-prefix>/<issue-number>-<slug-title>`.
- При изменениях создаётся commit, push и PR.
- Существующий PR для ветки переиспользуется, чтобы не создавать дубликаты.

### PR review loop, частично

- Есть PR review mode: `--pr <N> --from-review-comments`.
- Загружаются unresolved review threads, review summaries и conversation comments.
- Неактуальный и неactionable feedback фильтруется.
- По review feedback строится prompt для агента.
- Есть автоматическое переключение `--issue <N>` в PR review mode, если у issue уже есть linked open PR.
- Можно опционально оставить короткий PR summary comment через `--post-pr-summary`.

### Runner/backend support, базово

- Поддерживаются два runner'а: `claude` и `opencode`.
- Можно указать `--model`.
- Для OpenCode можно указать `--agent`.
- Есть локальный JSON-конфиг `local-config.json` с приоритетом ниже CLI-флагов.

### Повторные запуски и восстановление, частично

- Существующие локальные и remote issue-ветки переиспользуются.
- Существующие PR переиспользуются.
- При повторном запуске reused branch синхронизируется с base branch.
- Есть стратегии `rebase` и `merge`.
- При rebase conflict есть fallback на merge.
- При merge conflict есть deterministic auto-resolution в пользу base branch.
- Для rebase-based sync используется `--force-with-lease`.
- Есть `--dry-run` для предварительного просмотра действий.

### Базовые лимиты выполнения

- Есть hard timeout агента: `--agent-timeout-seconds`.
- Есть idle timeout агента: `--agent-idle-timeout-seconds`.
- Есть `--stop-on-error` для остановки после первой ошибки.

### Документация и тесты

- Есть `README.md` с примерами запуска и описанием режимов.
- Есть `docs/current-behavior.md` с фактическим текущим поведением.
- Есть unit tests для выбора режима, local config, PR review comments, reused branch/PR behavior и base branch resolution.

## Основные разрывы с North Star

### 1. Это ещё не Go-приложение

North Star: компилируемое Go-приложение с CLI и daemon режимом.

Текущее состояние: один Python-скрипт.

Разрыв:

- нет Go CLI;
- нет устанавливаемого бинарника;
- нет устойчивой структуры приложения;
- нет daemon/service режима;
- нет команд `install`, `init`, `run issue`, `run daemon`, `doctor`, `autodoctor`.

### 2. Нет автономного выбора задач

North Star: оркестратор периодически получает задачи, выбирает подходящие и ведёт их до результата.

Текущее состояние:

- можно обработать список open/closed/all issues через `--limit`;
- выбор задач ограничен тем, что вернул `gh issue list`;
- нет проектных правил выбора задач.

Разрыв:

- нет daemon polling;
- нет readiness/priority/label/milestone based selection;
- нет claim/lock механизма;
- нет понимания scope задачи перед началом выполнения.

### 3. Scope задач не конфигурируется

North Star: проект может явно указать, какие задачи можно брать автономно, а какие вне scope.

Текущее состояние:

- есть только технические фильтры: state, limit, issue number, empty body;
- нет allow/deny правил по labels, author, area, типу задачи, workflow или файлам.

Разрыв:

- нет project-level scope config;
- нет проверки scope перед запуском агента;
- нет понятного статуса “задача вне scope”.

### 4. Нет декомпозиции и создания связанных задач

North Star: оркестратор может декомпозировать краткую задачу, создавать подзадачи или связанные задачи и отражать связи.

Текущее состояние:

- issue body напрямую превращается в prompt;
- декомпозиция, если происходит, полностью внутри агента;
- скрипт не создаёт child issues или linked issues.

Разрыв:

- нет отдельной planning/decomposition фазы;
- нет `gh issue create` для подзадач;
- нет модели связей между задачами;
- нет отражения плана в трекере.

### 5. Уточнение требований не реализовано как workflow

North Star: при неоднозначности оркестратор запрашивает автора задачи и блокируется до ответа.

Текущее состояние:

- агент может сам написать что-то в изменениях или завершиться без результата;
- скрипт не распознаёт “нужно уточнение” как отдельное состояние;
- нет автоматического комментария автору issue с вопросом.

Разрыв:

- нет requirement clarification state;
- нет адресного вопроса автору;
- нет resume после ответа;
- нет правил, когда неопределённость значима.

### 6. Routing, presets и эскалация моделей отсутствуют

North Star: выбор исполнителя, модели и backend'а конфигурируется по проекту/задаче; при ошибках есть эскалация.

Текущее состояние:

- runner/model/agent задаются вручную через CLI или local config;
- поддерживаются только `claude` и `opencode`;
- нет presets;
- нет автоматической эскалации после ошибок.

Разрыв:

- нет cheap/default/hard presets;
- нет routing rules по labels/типам задач;
- нет retry policy с повышением модели;
- нет budget-aware выбора модели;
- нет pluggable runner abstraction за пределами двух hardcoded runner'ов.

### 7. CI loop есть только косвенно

North Star: оркестратор разбирает и чинит падающий CI, повторяет проверки, зовёт человека при блокере.

Текущее состояние:

- скрипт не читает GitHub Checks/Actions logs;
- не ждёт завершения CI;
- не запускает проектные проверки сам, кроме того, что может сделать агент внутри своего run;
- не принимает решения по CI статусам.

Разрыв:

- нет wait-for-checks;
- нет анализа failing checks;
- нет загрузки CI logs;
- нет отдельного CI-fix loop;
- нет правил “подождать”, “попробовать исправить”, “эскалировать человеку”.

### 8. Merge/deploy не реализованы

North Star: target state включает автоматический merge и возможную выкатку, если это разрешено проектом.

Текущее состояние:

- создаётся или обновляется PR;
- merge не выполняется;
- deploy не выполняется;
- approval/merge policy не анализируется.

Разрыв:

- нет `gh pr merge` workflow;
- нет проверки mergeability/required approvals как условия завершения;
- нет deploy hooks;
- нет “готово к merge” как формализованного статуса.

### 9. Workflow проекта почти не конфигурируется

North Star: workflow зависит от проекта: setup/test/lint/e2e/deploy, PR rules, merge policy.

Текущее состояние:

- local config покрывает параметры runner'а и git-поведения;
- нет project workflow config;
- нет команд setup/test/lint/build/e2e/deploy;
- нет doctor/autodoctor.

Разрыв:

- нет декларативного project config;
- нет автоопределения команд проекта;
- нет pre/post hooks;
- нет диагностики окружения.

### 10. Состояние хранится не в issue/PR comments как источник истины

North Star: на первых этапах state должен храниться в issue/PR comments, чтобы избежать split-brain.

Текущее состояние:

- основное состояние вычисляется из git branches, PR и текущих CLI флагов;
- PR summary comment опционален и очень короткий;
- нет стандартизированных state comments.

Разрыв:

- нет machine-readable комментариев состояния;
- нет записи попыток, выбранного runner/model, branch context, next action;
- нет восстановления по state comments;
- нет явных статусов blocked/waiting-for-author/waiting-for-ci/ready-to-merge.

### 11. Восстановление после сбоев реализовано частично

North Star: оркестратор продолжает незавершённые ветки/PR и закрывает старые ненужные PR.

Текущее состояние:

- reused branch/PR поддерживаются;
- sync с base branch поддерживается;
- старые ненужные PR не закрываются;
- нет политики определения obsolete PR.

Разрыв:

- нет resume model на уровне task state;
- нет cleanup stale PR;
- нет безопасного close PR workflow;
- нет восстановления после падения daemon/job в середине многошагового сценария.

### 12. Коммуникация не конфигурируется

North Star: кратко при happy path, подробно с reasoning summary при проблемах; уровень подробности настраивается.

Текущее состояние:

- скрипт печатает логи в stdout/stderr;
- PR summary comment можно включить вручную;
- issue comments и blocker comments автоматически не пишутся.

Разрыв:

- нет communication policy;
- нет автоматических issue/PR comments при неожиданных ошибках;
- нет подробных failure reports;
- нет messenger integrations.

### 13. Бюджеты и лимиты ограничены timeout'ами

North Star: можно ограничить стоимость, время, количество попыток, уровень моделей и эскалацию.

Текущее состояние:

- есть hard timeout и idle timeout;
- нет cost budget;
- нет max attempts per task;
- нет model tier limits.

Разрыв:

- нет budget config;
- нет подсчёта стоимости;
- нет политики остановки/эскалации по бюджету.

### 14. Только GitHub, нет Bitbucket/custom API proxy

North Star: GitHub, Bitbucket и custom API proxy как ближайшие интеграции.

Текущее состояние:

- вся интеграция завязана на GitHub CLI `gh`;
- GitHub-specific issue/PR model зашит в код.

Разрыв:

- нет provider abstraction;
- нет Bitbucket adapter;
- нет API proxy adapter;
- нет разделения tracker provider и code hosting provider.

### 15. Нет messenger integrations

North Star: опциональные интеграции с мессенджерами для уточнений, статусов и review requests.

Текущее состояние:

- messenger integrations отсутствуют.

Разрыв:

- нет notifier abstraction;
- нет Slack/Telegram/Discord;
- нет routing уведомлений к автору или команде.

## Предлагаемые направления задач

### Ближайшие задачи вокруг текущего прототипа

1. Зафиксировать `northstar.md` и этот gap analysis как продуктовые документы.
2. Добавить стандартизированные issue/PR state comments: branch, PR, runner, model, status, next action.
3. Добавить project config для scope rules и базовых workflow команд.
4. Добавить `doctor` для проверки `gh`, git repo, runner binaries, auth и clean worktree.
5. Добавить explicit statuses: `blocked`, `waiting-for-author`, `waiting-for-ci`, `ready-for-review`, `ready-to-merge`.
6. Добавить комментарий в issue/PR при ошибке с evidence, observed gap и next hypothesis.
7. Добавить max attempts per task и простую retry/escalation policy.
8. Добавить wait/check GitHub CI status без auto-fix как первый шаг к CI loop.

### Задачи для перехода к продуктовой архитектуре

1. Спроектировать Go CLI wrapper или новый Go core вокруг текущего Python-прототипа.
2. Вынести provider interfaces: tracker, code host, runner, notifier, workflow.
3. Ввести presets: cheap/default/hard с привязкой к runner/model/agent.
4. Сделать `init` и проектный config scaffold.
5. Сделать `run issue` как стабильный one-shot entrypoint.
6. Сделать `run daemon` / polling loop как отдельный режим.
7. Спроектировать Bitbucket/custom API proxy adapter.

### Target-state задачи

1. Автоматический PR review/CI loop до зелёного состояния.
2. Автоматический merge при соблюдении policy проекта.
3. Deploy hooks после merge.
4. Messenger integrations для уточнений и статусов.
5. Создание и ведение связанных задач/подзадач в трекере.
6. Budget-aware routing и cost reporting.

## Итог

Репозиторий уже содержит полезный MVP-прототип для GitHub + Claude/OpenCode + one-shot/batch issue runs + частичный PR review loop.

Главный разрыв с North Star — не в отсутствии запуска агента, а в отсутствии продуктового слоя оркестрации: Go CLI/daemon, project config, scope, state comments, task selection, clarification workflow, presets/escalation, CI/merge/deploy loop и multi-provider architecture.
