# Текущее поведение оркестратора

Документ фиксирует фактическое состояние репозитория после North Star batch. Он описывает то, что уже работает сегодня, без смешивания с целевым состоянием из `northstar.md`.

## Кратко

- Пользовательская точка входа уже есть в Go CLI: `orchestrator init`, `doctor`, `autodoctor`, `run issue`, `run pr`, `run daemon`.
- Основной execution core пока остается в `scripts/run_github_issues_to_opencode.py`; Go-слой в основном отвечает за CLI-форму, совместимость и bootstrap.
- MVP для GitHub largely implemented: one-shot issue flow, PR review flow, state comments, recovery, базовый daemon polling, project config, scope guards, workflow checks и PR readiness.
- Auto-merge, multi-provider execution core, полноценная conflict recovery orchestration и richer operator UX еще не завершены.

## Что уже реализовано

### CLI и режимы запуска

- Есть Go entrypoint `cmd/orchestrator`.
- `init` создает scaffold для `project-config.json` и `local-config.json`.
- `doctor` и `autodoctor` проверяют окружение, конфиг и могут делать runner smoke check.
- `run issue` запускает one-shot issue orchestration.
- `run pr` запускает review-comments flow для существующего PR.
- `run daemon` запускает polling loop поверх текущего GitHub/Python runner'а.

### Issue flow

- Поддерживаются одиночный запуск по `--issue` / `run issue --id` и batch/polling обработка списка issue.
- Скрипт создает или переиспользует deterministic issue branch и PR.
- Для reused branch есть sync с base branch, стратегии `rebase` и `merge`, fallback с `rebase` на `merge` и conservative auto-resolution части merge conflicts.
- Для image-only или image-backed issue attachments могут попадать в prompt.

### PR review и post-PR progression

- Есть `pr-review` режим по unresolved review feedback.
- Одинарный запуск по issue умеет автоматически переключаться в PR/review or CI progression path, если у issue уже есть связанный открытый PR.
- После push orchestrator может ждать CI status, оценивать readiness и публиковать состояния `waiting-for-ci`, `ready-for-review`, `blocked`, `ready-to-merge`.
- Auto-merge policy в config уже описывается, но полный merge loop еще не является основным завершенным сценарием.

### State comments, recovery и failure reporting

- В issue/PR публикуются machine-readable state comments с маркером `<!-- orchestration-state:v1 -->`.
- Для decomposition используется отдельный маркер `<!-- orchestration-decomposition:v1 -->`.
- Для agent failures публикуется structured failure report и ставится label `auto:agent-failed`.
- Повторные one-shot запуски читают последний корректный orchestration state и умеют продолжать работу из `failed`, `waiting-for-ci`, `ready-for-review`, `ready-to-merge` и related states.

### Decomposition и child issues

- Есть planning-only decomposition preflight.
- Для больших задач orchestrator может остановиться в `waiting-for-author` с decomposition plan comment.
- При подтвержденном плане и включенном флаге могут создаваться child issues с сохранением ссылок в decomposition payload.

### Конфиг, scope, presets и workflow

- Поддерживаются `local-config.json` и `project-config.json` со strict validation.
- В project config уже есть секции для `scope`, `workflow`, `readiness`, `merge`, `retry`, `budgets`, `communication` и `presets`.
- Scope rules могут блокировать out-of-scope issue до запуска агента и публиковать понятный blocked state.
- Workflow commands и hooks могут запускаться вокруг agent run и перед PR-ready states.
- Preset/routing/budget fields уже есть в конфиге, но их зрелость ниже, чем у базового issue/PR flow.

### Daemon mode

- `run daemon` уже существует и повторно вызывает batch issue flow по polling interval.
- Текущий daemon режим остается осторожным: GitHub-only, с ограниченной concurrency и опорой на существующий Python runner.
- Это рабочий автономный entrypoint ранней стадии, а не финальный service-grade orchestrator.

## Что еще не доведено до North Star

- Основной execution core пока не перенесен в Go и остается крупным Python runner'ом.
- Нет product-grade status UX наподобие отдельной `status`/`resume` поверхности для оператора.
- Нет dedicated conflict-recovery mode, который чинит только branch divergence без полного повторного issue run.
- Нет обязательного post-batch verification workflow, который сам создает follow-up issue/checklist после крупных merge batches.
- Нет полного auto-merge/deploy loop.
- Нет multi-provider tracker/code-host architecture beyond GitHub-centric implementation.

## Ближайшие практические зоны улучшения

1. Прогнать daemon smoke test на чистом `main` и зафиксировать expected operator path.
2. Улучшить видимость статуса: отдельный CLI/status summary для текущего orchestration state и daemon progress.
3. Уменьшить шум и длительность full Python verification.
4. Добавить dedicated conflict-recovery path для reused branches и stacked batches.
5. Автоматизировать post-batch verification и follow-up issue creation.
6. Разделить крупный Python runner на более мелкие модули, чтобы снизить conflict pressure и упростить перенос в Go.
