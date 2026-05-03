# Текущее поведение оркестратора

Документ фиксирует фактическое состояние репозитория после North Star batch. Он описывает то, что уже работает сегодня, без смешивания с целевым состоянием из `northstar.md`.

## Кратко

- Пользовательская точка входа уже есть в Go CLI: `orchestrator init`, `doctor`, `autodoctor`, `run issue`, `run pr`, `run daemon`.
- Go уже владеет execution-mode/router решениями для one-shot issue flow, daemon selection/claim logic, detached worker surfaces и post-batch verification; Python runner больше не должен заново принимать эти orchestration decisions за Go.
- `scripts/run_github_issues_to_opencode.py` остается compatibility adapter'ом для еще не перенесенных runtime paths, а не единственным execution core для всех сценариев.
- MVP для GitHub largely implemented: one-shot issue flow, PR review flow, state comments, recovery, базовый daemon polling, project config, scope guards, workflow checks и PR readiness.
- Auto-merge, multi-provider execution core, полноценная conflict recovery orchestration и richer operator UX еще не завершены.

## Что уже реализовано

### CLI и режимы запуска

- Есть Go entrypoint `cmd/orchestrator`.
- `init` создает scaffold для `project-config.json` и `local-config.json`.
- `doctor` и `autodoctor` проверяют окружение, конфиг и могут делать runner smoke check.
- `run issue` запускает Go-first one-shot issue orchestration для fresh-branch и reused-branch GitHub path.
- `run batch` запускает явный список issue ID как batch entrypoint: в `--dry-run` режиме показывает per-issue запуск, а в `--detach` стартует отдельный worker на каждый issue.
- `run pr` запускает review-comments flow для существующего PR.
- `run daemon` запускает Go-owned polling/claim loop и затем dispatch'ит совместимый worker path для каждого выбранного таска.

### Provider support matrix

- Поддерживаемая tracker/code host комбинация для Go-native runtime paths: `github/github`.
- `run issue --repo ...` и `run pr --repo ...` при `--tracker`/`--codehost`, отличных от `github/github`, делают явный fallback с понятной причиной в stderr.
- Daemon Go-policy selection включается только для `github` tracker; для остальных tracker execution остается на compatibility path.
- Jira tracker может быть передан в Python compatibility adapter flags, но Go-native issue/PR runtime под Jira пока не поддерживается.

### Issue flow

- Поддерживаются одиночный запуск по `--issue` / `run issue --id` и batch/polling обработка списка issue.
- Для нового GitHub issue без существующего linked PR Go сам выбирает mode, готовит fresh/reused branch, при необходимости синхронизирует reused branch с base, запускает agent, коммитит, пушит и создает PR без вызова Python runner'а.
- Скрипт создает или переиспользует deterministic issue branch и PR.
- Для reused branch есть sync с base branch, стратегии `rebase` и `merge`, fallback с `rebase` на `merge` и conservative auto-resolution части merge conflicts.
- Для image-only или image-backed issue attachments могут попадать в prompt.

### PR review и post-PR progression

- Есть `pr-review` режим по unresolved review feedback.
- Одинарный запуск по issue умеет автоматически переключаться в PR/review or CI progression path, если у issue уже есть связанный открытый PR.
- Это переключение теперь принимается в Go: `run issue` при linked PR маршрутизирует выполнение в явный PR compatibility adapter (`run pr` semantics), а не передает issue-level decision обратно в Python.
- После push orchestrator может ждать CI status, оценивать readiness и публиковать состояния `waiting-for-ci`, `ready-for-review`, `blocked`, `ready-to-merge`.
- Auto-merge policy в config уже описывается, но полный merge loop еще не является основным завершенным сценарием.

### State comments, recovery и failure reporting

- В issue/PR публикуются machine-readable state comments с маркером `<!-- orchestration-state:v1 -->`.
- Для decomposition используется отдельный маркер `<!-- orchestration-decomposition:v1 -->`.
- Для agent failures публикуется structured failure report и ставится label `auto:agent-failed`.
- Повторные one-shot запуски читают последний корректный orchestration state и умеют продолжать работу из `failed`, `waiting-for-ci`, `ready-for-review`, `ready-to-merge` и related states.
- Если восстановленный issue state или linked PR state указывает на другой issue/PR/branch ownership chain, runner публикует `blocked` state c `stage=ownership_validation` и ссылкой на конфликтующий tracker comment вместо автоматического продолжения.
- Границы между issue/PR state comments, detached worker metadata, verification verdicts и autonomous session checkpoint теперь отдельно зафиксированы в `docs/orchestration-state-boundaries.md`, чтобы перенос в Go не менял внешнее поведение.

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
- Текущий daemon режим остается осторожным: GitHub-only, с ограниченной concurrency; selection/claim policy выполняется в Go, а per-task execution для совместимых worker paths пока еще может идти через Python adapter.
- Для `run issue`, `run pr` и `run daemon` появился first-class `--detach` path: worker стартует в фоне и пишет `worker.json`/`worker.log` в `.orchestrator/workers/<issue-N|pr-N|daemon>/`.
- Во время autonomous batch loop сохраняется session-level checkpoint в `--autonomous-session-file`: done/current/next, issue/PR actions, blockers, счетчики и next checkpoint.
- `status` теперь умеет читать не только issue/PR state comments, но и session-level checkpoint из `--autonomous-session-file`, а также detached worker metadata через `--worker` и `--workers`, чтобы оператор мог проверить pid/process state, log progress, clone path, linked branch, linked issue/PR state и session checkpoint без ручных `ps`/`wc -l`/path lookup. Для worker surfaces доступен `--json`, а `status --worker issue-N` для detached batch child дополнительно сводит batch-level done/current/next, child workers, linked PRs, conflicts, verification и failures.
- Safe bounded smoke path для текущего entrypoint задокументирован в `docs/daemon-smoke-test.md`, включая post-#204 checklist для маленького detached batch.
- Финальный North Star smoke checklist с recorded commands/results хранится в `docs/final-smoke-checklist.md`.
- Это рабочий автономный entrypoint ранней стадии, а не финальный service-grade orchestrator.

### Граница Go/Python после #268

- Go-owned paths:
- `run issue` fresh-branch one-shot flow без linked PR/reused branch;
- execution-mode selection (`issue-flow` / `pr-review` / `skip`) и recovery-based routing;
- daemon issue selection, claim/release comments, session checkpoint wiring и detached worker preparation;
- detached worker registry/status surfaces и post-batch verification.
- Python compatibility adapter:
- `run pr` fallback paths, которые еще не перенесены в Go: `--dry-run`, `--isolate-worktree`, `--detach`, `--post-pr-summary`, follow-up branch mode и conflict-recovery/sync-only варианты;
- `doctor`, `autodoctor`, `status` и другие еще не перенесенные CLI compatibility surfaces;
- batch/daemon worker execution paths, которые все еще запускаются через script adapter;
- issue-path blockers, еще не реализованные в Go: linked PR reuse internals beyond adapter routing и dedicated conflict-recovery-only runtime.

### Post-#204 safety invariants

- Detached concurrency сейчас считается безопасной только при явной проверке branch/repo ownership на уровне каждого issue worker.
- Перед live merge оператор должен иметь ожидаемое соответствие `issue -> branch -> clone_path/repo root -> linked PR` и сверить его через `status --workers` плюс `status --worker issue-N`.
- Для `run batch --detach` допустим либо один fresh repo root на весь batch, либо внешне подготовленное соответствие `issue -> fresh clone`; в обоих случаях `clone_path` у worker'а обязан совпадать с заранее ожидаемым root и не может "переехать" на clone другого issue.
- PR ownership считается корректным только если linked PR у worker'а указывает на branch этого же issue; появление branch/PR другого issue в summary, worker registry или linked state считается cross-contamination и блокирует merge.
- Verification остается merge gate: перед merge нужен чистый linked readiness state и успешный verification verdict (`merge-result verification` для PR progression и/или `verify` / post-batch verification для batch-level follow-up).

## Что еще не доведено до North Star

- Часть PR review runtime уже перенесена в Go для explicit-repo non-isolated execution, но fallback-only режимы все еще живут в Python compatibility adapter вместе с reused-branch issue runtime и частью autonomous worker execution.
- Нет еще полноценной interactive `resume` поверхности и richer operator UX поверх сохраненного session checkpoint.
- Нет dedicated conflict-recovery mode, который чинит только branch divergence без полного повторного issue run.
- Нет обязательного post-batch verification workflow, который сам создает follow-up issue/checklist после крупных merge batches.
- Нет полного auto-merge/deploy loop.
- Нет multi-provider tracker/code-host architecture beyond GitHub-centric implementation.

## Ближайшие практические зоны улучшения

1. Прогнать daemon smoke test на чистом `main` и зафиксировать expected operator path.
2. Закрепить post-#204 branch-isolation smoke discipline как стандартный preflight для любых batch'ей шире 1 worker.
3. Уменьшить шум и длительность full Python verification.
4. Добавить dedicated conflict-recovery path для reused branches и stacked batches.
5. Автоматизировать post-batch verification и follow-up issue creation.
6. Продолжить сужать Python compatibility adapter до PR/reuse-only responsibilities и переносить оставшиеся runtime loops в Go.
