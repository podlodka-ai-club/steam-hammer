# Архитектура Go-оркестратора

Документ предлагает целевую архитектуру Go-сервиса на основе `northstar.md` и текущего Python-прототипа, описанного в `README.md` и `docs/current-behavior.md`.

Цель архитектуры: перейти от скрипта GitHub issue/PR runner'а к устанавливаемому Go-оркестратору, который может работать в one-shot и daemon режимах, сохраняет состояние в трекере, выбирает задачи по правилам проекта и доводит изменение до PR/MR, а затем до merge, если это разрешено политиками проекта.

## Ключевые принципы

- Tracker/code host остается источником истины на ранних стадиях: состояние пишется в issue/PR comments с machine-readable маркерами.
- Core workflow не должен зависеть от GitHub, `gh`, Claude, OpenCode или Python-скрипта напрямую.
- Все опасные действия проходят через project policy: scope, budgets, retry limits, merge policy, destructive-action policy.
- One-shot и daemon используют один execution core, отличаются только способом выбора задач и жизненным циклом процесса.
- Миграция должна быть инкрементальной: Go CLI сначала может вызывать текущий Python-runner через adapter, затем логика переносится в Go по слоям.

## Текущий миграционный срез

- Первый чистый Go core slice расположен в `internal/core/dependencies`.
- Он пока дублирует Python-логику `parse_issue_dependency_references` и остается provider-agnostic: на входе tracker, self ref и тексты issue/comments; на выходе нормализованные dependency refs.
- Следующий безопасный шаг после этого среза: использовать пакет в Go task-intake/readiness логике, не меняя текущий Python execution path.

## Верхнеуровневая схема

```text
                +----------------------+
                | CLI / Daemon Entrypoint |
                +-----------+----------+
                            |
                            v
                  +---------+---------+
                  | Orchestration Core |
                  +---------+---------+
                            |
      +---------------------+----------------------+
      |                     |                      |
      v                     v                      v
+-----+------+       +------+-------+       +------+------+
| Task Intake |       | Task Engine  |       | Recovery    |
+-----+------+       +------+-------+       +------+------+
      |                     |                      |
      v                     v                      v
+-----+------+       +------+-------+       +------+------+
| Tracker    |       | Runner       |       | State Store  |
| Adapter    |       | Adapter      |       | Comments/DB  |
+-----+------+       +------+-------+       +------+------+
      |                     |                      |
      v                     v                      v
+-----+------+       +------+-------+       +------+------+
| Code Host  |       | Workflow     |       | Notifier     |
| Adapter    |       | Adapter      |       | Adapter      |
+------------+       +--------------+       +-------------+
```

## Основные компоненты

### 1. Entrypoint Layer

Отвечает только за запуск режима, чтение конфигурации, wiring зависимостей и graceful shutdown.

Команды MVP:

- `orchestrator init`: создает `orchestrator.yaml` или `orchestrator.json` scaffold.
- `orchestrator doctor`: проверяет git, auth, provider credentials, runner binaries, project config, workflow commands.
- `orchestrator run issue --id N`: one-shot обработка одной задачи.
- `orchestrator run pr --id N`: one-shot обработка существующего PR/MR.
- `orchestrator run daemon`: polling loop для автономного режима.

Команды позже:

- `orchestrator autodoctor`: диагностика с рекомендациями.
- `orchestrator status --issue N`: чтение последнего orchestration state.
- `orchestrator resume --issue N`: явное продолжение по сохраненному состоянию.

### 2. Orchestration Core

Центральный слой с бизнес-логикой. Он не знает о GitHub CLI, Bitbucket API, Claude CLI или OpenCode CLI. Все внешние действия выполняются через интерфейсы.

Зоны ответственности:

- построить execution context задачи;
- проверить scope и policy;
- выбрать runner preset;
- выполнить state machine задачи;
- вызвать workflow checks;
- обработать review/CI loop;
- решить, можно ли продолжать, эскалировать или блокироваться;
- записать статус и next action.

Основной тип:

```go
type Engine struct {
    Tracker   TrackerProvider
    CodeHost  CodeHostProvider
    Runner    RunnerRouter
    Workflow  WorkflowRunner
    State     StateStore
    Policy    PolicyEngine
    Notify    Notifier
    Clock     Clock
}
```

### 3. Task Intake

Выбирает задачи для выполнения.

В one-shot режиме intake получает конкретный issue/PR id из CLI.

В daemon режиме intake:

- периодически получает кандидатов из tracker'а;
- применяет scope и readiness rules;
- проверяет labels, priority, milestone, assignee, author, freshness;
- делает claim/lock задачи через state comment и/или label;
- соблюдает concurrency limits;
- не берет задачи в статусах `blocked`, `waiting-for-author`, `manual-only` без явного override.

### 4. Policy Engine

Единая точка принятия решений о допустимости действий.

Проверки:

- scope: можно ли брать задачу автономно;
- budget: лимиты времени, попыток, стоимости, уровня модели;
- retry: можно ли повторить шаг;
- escalation: когда перейти на более сильную модель/backend;
- merge: можно ли выполнить auto-merge;
- destructive actions: можно ли закрывать PR, force push, удалять ветки, запускать deploy.

Важное правило: core не должен обходить политики code host'а. Если merge заблокирован required reviews, security checks или branch protection, задача переходит в `waiting-for-human`/`ready-to-merge`, а не пытается обойти ограничение.

### 5. Runner Router

Выбирает исполнителя и запускает agent backend.

MVP backend'ы:

- OpenCode;
- Claude;
- compatibility adapter к текущему Python-runner.

Целевая модель:

```go
type Runner interface {
    Run(ctx context.Context, req RunnerRequest) (RunnerResult, error)
}

type RunnerRequest struct {
    RepoPath     string
    Task         TaskContext
    Prompt       string
    Branch       string
    Model        string
    Agent        string
    Budget       Budget
    Permissions  PermissionSet
    Attachments  []ArtifactRef
}
```

Runner Router выбирает preset по правилам проекта:

- `cheap`: простые документационные задачи, small fixes, низкий бюджет;
- `default`: обычные bugfix/feature задачи;
- `hard`: сложные изменения, failing CI после первой попытки, review conflicts;
- `manual`: задача не запускается автоматически, требуется человек.

### 6. Provider Adapters

Provider слой делится на tracker и code host, даже если в GitHub они физически представлены одной системой.

TrackerProvider:

```go
type TrackerProvider interface {
    GetIssue(ctx context.Context, id string) (Issue, error)
    ListCandidateIssues(ctx context.Context, q TaskQuery) ([]Issue, error)
    AddIssueComment(ctx context.Context, id string, body string) error
    SetLabels(ctx context.Context, id string, labels LabelPatch) error
    CreateLinkedIssue(ctx context.Context, req CreateIssueRequest) (Issue, error)
}
```

CodeHostProvider:

```go
type CodeHostProvider interface {
    DefaultBranch(ctx context.Context, repo RepoRef) (string, error)
    FindOpenPR(ctx context.Context, q PullRequestQuery) (*PullRequest, error)
    CreatePR(ctx context.Context, req CreatePullRequestRequest) (PullRequest, error)
    GetPR(ctx context.Context, id string) (PullRequest, error)
    ListReviewFeedback(ctx context.Context, pr string) ([]ReviewFeedback, error)
    ListChecks(ctx context.Context, ref string) (CheckSummary, error)
    MergePR(ctx context.Context, id string, opts MergeOptions) error
    ClosePR(ctx context.Context, id string, reason string) error
}
```

MVP adapters:

- `github-gh`: shell-out к `gh`, максимально близко к текущему прототипу;
- `github-api`: прямой REST/GraphQL adapter, целевой для service mode.

Следующие adapters:

- `bitbucket-api`;
- `custom-proxy-api`.

### 7. Workflow Runner

Запускает проектные команды и нормализует результат.

Команды берутся из project config:

- setup;
- test;
- lint;
- build;
- e2e;
- deploy hooks, только если включены явно.

Workflow Runner должен возвращать структурированный результат: команда, exit code, длительность, stdout/stderr excerpts, artifact references. Core использует это для state comments, CI-fix prompts и failure reports.

### 8. State Store

На MVP источник истины - append-only comments в issue/PR.

State markers:

- `<!-- orchestration-state:v1 -->` для общего состояния;
- `<!-- orchestration-scope:v1 -->` для scope decisions;
- `<!-- orchestration-agent-failure:v1 -->` для failure reports;
- `<!-- orchestration-claim:v1 -->` для daemon claim/lock.

StateStore обязан уметь:

- записать новый state event;
- прочитать последний валидный event;
- игнорировать поврежденные payload'ы;
- восстановить execution context после рестарта;
- найти зависшие или устаревшие claims;
- сохранить ссылку на branch, PR/MR, runner, model, attempt, stage, next action.

Позже можно добавить внутреннее хранилище для аналитики и ускорения daemon mode, но оно не должно становиться конфликтующим источником истины. Если DB есть, comments остаются externally visible audit log.

## State Machine

Базовые статусы:

- `new`: задача найдена, orchestration state еще нет.
- `claimed`: daemon взял задачу в работу.
- `in-progress`: идет выполнение шага.
- `waiting-for-author`: нужен ответ автора или продуктовое решение.
- `waiting-for-ci`: PR/MR ожидает завершения checks.
- `ready-for-review`: PR/MR создан и готов к human review.
- `ready-to-merge`: checks зелёные, known blockers отсутствуют, merge требует разрешения или включенной policy.
- `merged`: изменение смержено.
- `blocked`: автоматическое продолжение невозможно.
- `failed`: техническая ошибка выполнения, rerun возможен по retry policy.
- `out-of-scope`: задача не соответствует project rules.

Основной happy path:

```text
new
  -> claimed
  -> in-progress(scope_check)
  -> in-progress(planning)
  -> in-progress(branch_prepare)
  -> in-progress(agent_run)
  -> in-progress(workflow_checks)
  -> ready-for-review
  -> waiting-for-ci
  -> ready-to-merge
  -> merged
```

Основные развилки:

- scope deny -> `out-of-scope` или `blocked` с reason;
- неоднозначные требования -> `waiting-for-author`;
- agent timeout/failure -> retry, escalation или `failed`;
- workflow checks failed -> CI/workflow fix loop или `blocked`;
- review feedback exists -> PR review loop -> `waiting-for-ci`;
- branch protection blocks merge -> `ready-to-merge` с next action `await human approval`;
- устаревший PR -> safe close только при включенной cleanup policy.

## Основной сценарий `run issue`

1. Загрузить project config и local config.
2. Выполнить provider и repository preflight checks.
3. Получить issue из tracker'а.
4. Прочитать последний orchestration state.
5. Если state `waiting-for-author` или `blocked`, остановиться без override.
6. Проверить scope и budget policy.
7. Найти связанный open PR; если он есть, перейти в PR progression path.
8. Выбрать или создать branch.
9. Синхронизировать reused branch с base branch по policy.
10. Сформировать prompt из issue, recovered state, project instructions и workflow context.
11. Выбрать runner preset.
12. Запустить runner.
13. Если изменений нет, записать `waiting-for-author` или no-op result с reason.
14. Commit/push изменений через Git layer.
15. Запустить локальные workflow checks, если они настроены.
16. Создать или переиспользовать PR/MR.
17. Записать `ready-for-review` или `waiting-for-ci`.
18. Если policy разрешает, перейти к CI/review/merge progression.

## PR/CI progression path

Этот путь используется для `run pr`, auto-switch из `run issue` и daemon resume.

1. Получить PR/MR и связанный issue context.
2. Прочитать review feedback.
3. Если есть actionable feedback, сформировать prompt и запустить runner на PR branch или follow-up branch.
4. Если были изменения, commit/push и перейти в `waiting-for-ci`.
5. Если feedback нет, прочитать checks.
6. Pending checks -> `waiting-for-ci` и retry позже.
7. Failed checks -> получить logs, классифицировать failure.
8. Fixable failure -> runner получает CI context и делает попытку исправления.
9. Non-fixable или превышен budget -> `blocked` с evidence.
10. Successful checks -> проверить merge policy.
11. Auto-merge allowed -> merge PR/MR.
12. Auto-merge not allowed -> `ready-to-merge`.

## Daemon Mode

Daemon - это scheduler вокруг того же core engine.

Зоны ответственности daemon:

- polling задач по project rules;
- claim/lock через tracker state;
- ограничение параллельности;
- periodic resume задач в `waiting-for-ci`, `ready-for-review`, `failed` при разрешенном retry;
- cleanup устаревших claims;
- graceful shutdown без потери состояния;
- метрики и health endpoint, если сервис запускается постоянно.

Минимальная модель конкурентности:

- один worker pool на репозиторий;
- per-repository lock на git worktree operations;
- isolated worktree на задачу для параллельной работы;
- global budget limiter на процесс.

## Git и workspace strategy

MVP может работать в одном clean worktree, как текущий прототип.

Для daemon режима лучше использовать isolated worktrees:

```text
.orchestrator/worktrees/
  issue-123/
  pr-456/
```

Преимущества:

- параллельные задачи не мешают друг другу;
- проще recovery после падения;
- меньше риск затронуть рабочее дерево пользователя;
- PR review mode не требует переключать основную ветку.

Git слой должен быть отдельным adapter'ом внутри infrastructure layer, потому что branch prepare, sync, commit, push и cleanup имеют много edge cases и должны тестироваться отдельно.

## Конфигурация

Предлагаемый `orchestrator.yaml`:

```yaml
project:
  repo: owner/repo
  default_base: default

providers:
  tracker:
    type: github
  code_host:
    type: github

scope:
  labels:
    allow: [autonomous, bug]
    deny: [manual-only, needs-product-decision]
  authors:
    allow: []
    deny: [dependabot[bot]]

workflow:
  commands:
    test: python3 -m unittest discover -s tests -p 'test_*.py'
    lint: null
    build: null

routing:
  default_preset: default
  rules:
    - when:
        labels: [docs]
      preset: cheap
    - when:
        labels: [hard, architecture]
      preset: hard

presets:
  cheap:
    runner: opencode
    model: openai/gpt-4o-mini
    max_attempts: 1
  default:
    runner: opencode
    model: openai/gpt-5.5
    max_attempts: 2
  hard:
    runner: claude
    model: claude-sonnet-4-6
    max_attempts: 3

budgets:
  max_attempts_per_task: 3
  max_runtime_minutes: 60
  max_model_tier: hard

communication:
  verbosity: normal

merge:
  auto_merge: false
  method: squash

daemon:
  poll_interval_seconds: 120
  max_parallel_tasks: 1
```

Приоритет настроек:

1. CLI flags.
2. Local user config.
3. Project config.
4. Built-in defaults.

## Go package layout

Предлагаемая структура:

```text
cmd/orchestrator/
  main.go

internal/app/
  cli.go
  daemon.go
  wiring.go

internal/core/
  engine.go
  state_machine.go
  task.go
  policy.go
  planning.go
  errors.go

internal/config/
  config.go
  validate.go
  defaults.go

internal/state/
  comments.go
  recovery.go
  payloads.go

internal/providers/tracker/
  provider.go
  github/
  bitbucket/

internal/providers/codehost/
  provider.go
  github/
  bitbucket/

internal/runners/
  runner.go
  router.go
  opencode/
  claude/
  pythoncompat/

internal/workflow/
  runner.go
  shell.go

internal/git/
  repo.go
  branch.go
  worktree.go

internal/notify/
  notifier.go
  noop.go
  slack/

internal/observability/
  logging.go
  metrics.go
```

Публичные Go packages на старте не нужны. Пока продукт не стабилизирован, лучше держать код в `internal`, чтобы не обещать внешний SDK.

## Observability

Минимум для MVP:

- structured logs с `run_id`, `issue_id`, `pr_id`, `repo`, `stage`, `attempt`;
- state comments для внешнего audit trail;
- dry-run вывод плана действий;
- doctor report с PASS/WARN/FAIL;
- failure report с evidence и next action.

Для daemon:

- health endpoint;
- counters: tasks claimed, succeeded, blocked, failed, merged;
- duration histograms по стадиям;
- budget/cost summary, если backend возвращает usage.

## Миграционный план

### Фаза 1. Go CLI wrapper

- Создать Go CLI с командами `doctor`, `run issue`, `run pr`.
- Внутри вызывать текущий Python-runner как `pythoncompat` runner.
- Сохранить совместимость с существующими флагами там, где это важно.
- Не менять источник истины: state comments остаются текущего формата v1.

### Фаза 2. Вынести config/state/policy в Go

- Перенести чтение project/local config.
- Перенести scope evaluation.
- Перенести state comment parsing/writing.
- Python оставить только для agent/git/PR execution path.

### Фаза 3. Перенести GitHub provider и Git layer

- Реализовать `github-gh` adapter в Go.
- Перенести branch prepare, sync, commit, push, PR create/reuse.
- Покрыть edge cases тестами: reused branch, linked PR, dirty tree, conflict fallback.

### Фаза 4. Перенести runner и workflow execution

- Реализовать native OpenCode/Claude runners.
- Перенести workflow checks.
- Добавить presets, retry и escalation.

### Фаза 5. Daemon и recovery

- Добавить polling loop.
- Ввести claim comments/labels.
- Добавить isolated worktrees.
- Реализовать resume для `waiting-for-ci`, `failed`, `ready-for-review`.

### Фаза 6. Multi-provider и merge loop

- Разделить GitHub-specific assumptions.
- Добавить Bitbucket/custom proxy adapters.
- Добавить auto-merge policy.
- Добавить notifier adapters.

## MVP boundaries

Первый production-worthy Go MVP должен уметь:

- запускаться как бинарник;
- выполнять `init`, `doctor`, `run issue`, `run pr`;
- работать с GitHub;
- использовать OpenCode и Claude;
- читать/писать state comments v1;
- применять scope rules;
- создавать/переиспользовать branch и PR;
- запускать workflow checks;
- читать PR review feedback;
- читать CI checks и переводить PR в `waiting-for-ci`, `ready-to-merge` или `blocked`;
- безопасно восстанавливаться при повторном one-shot запуске.

Не стоит включать в первый Go MVP:

- Bitbucket;
- custom API proxy;
- messenger integrations;
- auto-deploy;
- сложную внутреннюю БД;
- полностью автономный multi-worker daemon;
- автоматическое закрытие устаревших PR без отдельной policy.

## Главные архитектурные риски

- Split-brain между comments и внутренним storage. Решение: comments являются source of truth, DB только cache/analytics.
- Слишком ранняя генерализация provider interfaces. Решение: начать с GitHub, но держать разделение tracker/code host в core API.
- Опасные git/merge действия в daemon. Решение: isolated worktrees, explicit policies, no bypass branch protection.
- Непредсказуемые agent outputs. Решение: stage-level state, retry limits, structured prompts, workflow checks, failure reports.
- Рост сложности state machine. Решение: хранить transitions явно и тестировать каждый переход отдельно.

## Итоговое решение

Go-оркестратор стоит строить как небольшой orchestration core с plugin-like adapters вокруг него. Core управляет state machine, policy, routing и recovery. Внешний мир представлен интерфейсами: tracker, code host, runner, workflow, notifier и state store.

Для ближайшей реализации самый безопасный путь - Go CLI wrapper поверх текущего Python-прототипа с постепенным переносом state/config/policy/git/provider logic в Go. Это дает быстрый переход к продуктовой форме (`init`, `doctor`, `run issue`, `run daemon`) без потери уже реализованных возможностей: state comments, scope rules, workflow checks, PR review mode и CI status read.
