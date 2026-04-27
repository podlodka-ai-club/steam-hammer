# Текущее поведение оркестратора (фактическое состояние)

Этот документ описывает наблюдаемое поведение скрипта `scripts/run_github_issues_to_opencode.py` в текущем состоянии репозитория.

## Оглавление

- [1. Назначение и зона ответственности](#1-назначение-и-зона-ответственности)
- [2. Предусловия запуска](#2-предусловия-запуска)
- [3. Основные режимы работы](#3-основные-режимы-работы)
- [4. Сценарий issue-flow](#4-сценарий-issue-flow)
- [5. Сценарий pr-review](#5-сценарий-pr-review)
- [6. Автопереключение issue -> pr-review](#6-автопереключение-issue---pr-review)
- [7. Поведение при повторных запусках и синхронизации веток](#7-поведение-при-повторных-запусках-и-синхронизации-веток)
- [8. Конфигурация и приоритет настроек](#8-конфигурация-и-приоритет-настроек)
- [9. Важные флаги и наблюдаемое поведение](#9-важные-флаги-и-наблюдаемое-поведение)
- [10. Ограничения и ранние выходы](#10-ограничения-и-ранние-выходы)
- [11. Recovery контекста из orchestration-state](#11-recovery-контекста-из-orchestration-state)
- [12. Failure reporting и label](#12-failure-reporting-и-label)

## 1. Назначение и зона ответственности

Скрипт автоматизирует цикл работы с GitHub issue/PR и AI-агентом:

- получает контекст из GitHub через `gh`;
- формирует промпт и запускает агент (`claude` или `opencode`);
- при наличии изменений делает commit/push и создает или переиспользует PR.
- публикует append-only state-комментарии в issue/PR с маркером `<!-- orchestration-state:v1 -->`.
- при per-issue ошибках публикует структурированный failure report и ставит label `auto:agent-failed`.

Факт: скрипт сам выполняет git-операции и ожидает чистое состояние рабочего дерева до старта.

## 2. Предусловия запуска

Перед основной логикой проверяется:

- путь `--dir` существует и является git-репозиторием (`.git/` присутствует);
- рабочее дерево чистое (иначе ошибка: `Git working tree must be clean before running this script.`);
- валидность комбинаций флагов для PR-режима (`--pr` и `--from-review-comments` должны использоваться вместе).

Если `--repo` не передан, репозиторий определяется через текущий контекст `gh`.

## 3. Основные режимы работы

Поддерживаются два режима:

- `issue-flow`: обработка issue, подготовка issue-ветки, запуск агента, commit/push, создание или переиспользование PR;
- `pr-review`: обработка одного PR по ревью-комментариям, запуск агента по агрегированному фидбеку, commit/push в текущую или follow-up ветку.

Отдельно есть `--dry-run`: действия печатаются, но агент и git-команды не выполняются.

## 4. Сценарий issue-flow

Наблюдаемая последовательность:

1. Выбирается базовая ветка issue-flow:
   - по умолчанию (`--base default`) берется default branch репозитория из GitHub;
   - в opt-in stacked режиме (`--base current` / `--base-branch current`) берется текущая локальная ветка.
2. Загружается одна issue (`--issue`) или список (`--state` + `--limit`).
3. Выполняются pre-checks идемпотентности:
   - в batch-режиме issue с linked open PR пропускается по умолчанию (`--skip-if-pr-exists`);
   - issue с deterministic remote branch пропускается по умолчанию (`--skip-if-branch-exists`);
   - `--force-reprocess` отключает оба skip guard;
   - для одиночного `--issue` linked open PR не hard-skip'ается, а используется state-aware mode selection / PR-review progression.
4. Для issue с пустым body выполняется пропуск, если не включен `--include-empty` **и** нет найденных ссылок на изображения.
   - Изображения извлекаются из тел issue (`![](...)`, `<img src=...>`, прямые URL).
   - По найденным ссылкам создаются локальные файлы в временной директории и добавляются в prompt как входные изображения для Claude через `--image`.
   - Если загрузка изображения падает, это логируется, и обработка продолжается в text-only режиме.
5. Выполняется planning-only decomposition preflight (`--decompose auto` по умолчанию): большие/epic/multi-step задачи получают proposed plan comment и останавливаются до запуска агента.
6. Выбирается/создается рабочая ветка по шаблону `<prefix>/<issue-number>-<slug-title>` (по умолчанию prefix: `issue-fix`).
7. Для переиспользованной ветки может выполняться синхронизация с базой (по умолчанию включена).
8. Запускается агент с issue-контекстом.
9. Если изменений нет:
   - обычно commit/PR пропускаются;
   - исключение: если ветка была синхронизирована и изменена только синком, эти изменения пушатся и PR обновляется.
10. Если изменения есть: commit `Fix issue #N: <title>`, push, затем создание или переиспользование PR.
11. На ключевых переходах публикуются state-комментарии в issue:
    - `in-progress` перед запуском агента (когда известен branch context);
    - `ready-for-review` после создания/переиспользования PR;
    - `failed` при ошибках (stage/error/next_action);
    - `waiting-for-author`, если изменений нет.
12. После успешного/no-op завершения для processed issue скрипт пытается снять label `auto:agent-failed`, если он был поставлен раньше.

После обработки issue скрипт возвращается на базовую ветку (кроме `dry-run`).

## 5. Сценарий pr-review

Режим включается парой `--pr <number> --from-review-comments`.

Наблюдаемое поведение:

1. Загружается PR и проверяется, что состояние `OPEN`.
2. Загружаются review threads (GraphQL) и список review summary.
3. Из фидбека исключаются:
   - resolved/outdated threads;
   - outdated comments;
   - пустые комментарии;
   - комментарии и review summary от автора PR;
   - review summary со state, отличным от `CHANGES_REQUESTED` и `COMMENTED`.
4. Для review summary берется только последняя запись каждого автора.
5. Строится единый промпт: PR + связанный issue-контекст + отфильтрованные пункты фидбека.
6. Рабочая ветка выбирается по `headRefName` целевого PR:
   - если текущая ветка отличается от target branch, запуск по умолчанию останавливается safeguard'ом;
   - `--allow-pr-branch-switch` разрешает переключить текущий worktree на target branch;
   - `--isolate-worktree` выполняет PR-mode во временном worktree и не переключает основную рабочую ветку;
   - `--pr-followup-branch-prefix` создает follow-up branch от target branch.
7. Агент запускается на target/follow-up branch.
8. При наличии изменений выполняется commit `Address review comments for PR #N` и push выбранной ветки.
9. Опционально публикуется короткий комментарий в PR (`--post-pr-summary`).
10. На ключевых переходах публикуются state-комментарии в PR:
    - `in-progress` при старте обработки;
    - `waiting-for-ci` после push изменений;
    - `waiting-for-author`, если нет actionable-комментариев или агент не дал изменений;
    - `failed` при ошибках.

Если actionable комментариев нет, скрипт завершает работу успешно без запуска агента.

## 6. Автопереключение issue -> pr-review

Только при запуске с `--issue` (одна задача) скрипт проверяет, есть ли связанный открытый PR.

- если PR найден, одиночный запуск выбирает state-aware `pr-review`/check path вместо duplicate issue-flow;
- если не найден, остается `issue-flow`;
- если задан `--force-issue-flow`, автопереключение отключается.

В batch-режиме linked open PR по умолчанию является причиной skip (`--skip-if-pr-exists`), чтобы не тратить agent runs на задачи, которые уже находятся в работе. Это отличается от одиночного `--issue`, где existing PR может означать необходимость review/CI progression.

Причина выбранного режима печатается в лог (включая `dry-run`).

## 7. Поведение при повторных запусках и синхронизации веток

Для issue-flow:

- существующая локальная/удаленная issue-ветка переиспользуется;
- при `--fail-on-existing` повторный запуск с существующей веткой или PR завершится ошибкой;
- синхронизация переиспользованной ветки включена по умолчанию (`--sync-reused-branch`).
- по умолчанию batch-runs пропускают issue с linked open PR (`--skip-if-pr-exists`) или deterministic remote branch (`--skip-if-branch-exists`);
- `--force-reprocess` отключает оба skip guard и разрешает intentional rerun;
- run summary содержит отдельные счетчики `processed`, `skipped_existing_pr`, `skipped_existing_branch`, `failures`.

Стратегии синхронизации (`--sync-strategy`):

- `rebase` (по умолчанию): `git fetch` + `git rebase origin/<base>`;
- `merge`: `git fetch` + `git merge --no-edit -X theirs origin/<base>`.

Если `rebase` конфликтует, скрипт автоматически делает fallback на merge-синхронизацию.
Если merge конфликтует, есть авторазрешение в пользу базовой ветки (`checkout --theirs` по всем конфликтным файлам, затем commit).
Если авторазрешение невозможно, выполнение issue прерывается с ошибкой и агент не запускается.

При push после синхронизации rebase может использоваться `--force-with-lease` (для sync-only и для обычного случая после изменений агента, если история была переписана синком).

## 8. Конфигурация и приоритет настроек

Поддерживается локальный JSON-конфиг (по умолчанию `local-config.json` в `--dir`, можно переопределить через `--local-config`).

Приоритет значений:

1. CLI-флаги;
2. локальный конфиг;
3. встроенные defaults.

Скрипт валидирует типы и допустимые значения ключей локального конфига и падает при неподдерживаемых ключах.

## 9. Важные флаги и наблюдаемое поведение

- `--runner claude|opencode`: выбор раннера агента;
- `--agent`: имя агента для `opencode`;
- `--model`: переопределение модели;
- `--agent-timeout-seconds`: жесткий таймаут выполнения агента;
- `--agent-idle-timeout-seconds`: аварийный останов при отсутствии вывода агента;
- `--opencode-auto-approve`: добавляет `--dangerously-skip-permissions` для OpenCode;
- `--include-empty`: не пропускать issue с пустым body; image-only issue теперь обрабатывается автоматически при обнаружении приложений.
- `--stop-on-error`: остановка после первой ошибки;
- `--fail-on-existing`: строгий режим без переиспользования существующих branch/PR;
- `--skip-if-pr-exists` / `--no-skip-if-pr-exists`: skip guard для linked open PR;
- `--skip-if-branch-exists` / `--no-skip-if-branch-exists`: skip guard для deterministic remote branch;
- `--force-reprocess`: отключает оба skip guard;
- `--sync-reused-branch` / `--no-sync-reused-branch`: включить/выключить синхронизацию переиспользованных веток;
- `--sync-strategy rebase|merge`: стратегия синхронизации переиспользованной ветки;
- `--base default|current` (`--base-branch`): выбор базовой ветки для issue-flow (стабильная default-ветка или stacked запуск от текущей ветки);
- `--decompose auto|never|always`: planning-only decomposition preflight перед issue-flow agent run; `auto` предлагает план для больших задач, `always` форсирует plan-only режим, `never` отключает preflight;
- `--create-child-issues`: при утвержденном плане (`status=approved` / `status=execution_plan`) создает связанные child issue по `proposed_children` и сохраняет ссылки в `created_children` внутри `<!-- orchestration-decomposition:v1 -->` payload
- `--allow-pr-branch-switch`: в PR-mode разрешает переключить текущий worktree на target PR branch;
- `--isolate-worktree`: в PR-mode запускает работу во временном worktree без переключения текущей ветки;
- `--dry-run`: печать планируемых действий без выполнения.
- `--dry-run`: state-комментарии не публикуются, только печатается план публикации (куда и с каким статусом).

## 10. Ограничения и ранние выходы

Текущие ограничения и условия, когда скрипт завершает работу без выполнения полного цикла:

- грязное рабочее дерево -> немедленная ошибка;
- `--pr` без `--from-review-comments` (или наоборот) -> ошибка валидации аргументов;
- PR в состоянии не `OPEN` -> выход без изменений;
- отсутствуют actionable review comments -> успешный выход без запуска агента;
- issue body пустой и нет `--include-empty` и нет распознанных image-ссылок -> issue пропускается;
- batch issue с linked open PR или deterministic remote branch -> skip по умолчанию;
- issue-flow с найденной большой/epic/multi-step задачей при `--decompose auto|always` -> публикуется `<!-- orchestration-decomposition:v1 -->` plan comment, state `waiting-for-author` stage `decomposition_plan`, агент не запускается;
- если план помечен как `approved`/`execution_plan` и указан `--create-child-issues`, скрипт идемпотентно создает отсутствующие `proposed_children` как отдельные issues и хранит результат в `created_children`
- PR-mode из нецелевой ветки без `--allow-pr-branch-switch` или `--isolate-worktree` -> ошибка safeguard;
- если агент не внес изменения и не было sync-only обновления -> commit/push/PR пропускаются.

- Текущая поддержка изображений покрывает ссылки/вложения из GitHub issue bodies и generic attachment metadata. Явная Jira attachment API интеграция пока остается follow-up.

## 11. Recovery контекста из orchestration-state

Первый срез recovery-логики реализован консервативно:

- для одиночной обработки (`--issue <n>` и `--pr <n> --from-review-comments`) скрипт читает комментарии через GitHub API и ищет блоки с маркером `<!-- orchestration-state:v1 -->`;
- из найденных комментариев выбирается **последний корректно распарсенный JSON** по `created_at`;
- некорректные state-комментарии безопасно игнорируются, в лог печатается warning;
- найденный recovery-контекст печатается в лог, включая `dry-run`;
- если recovered status = `waiting-for-author` или `blocked`, issue по умолчанию пропускается (можно переопределить через `--force-issue-flow`);
- если recovered status = `ready-for-review` или `waiting-for-ci` и есть открытый связанный PR, предпочтение отдается существующему PR-review/check пути;
- если recovered status = `failed`, повторный запуск разрешен, а контекст прошлой ошибки добавляется в лог и в prompt агента.

## 12. Failure reporting и label

Для per-issue ошибок реализован отдельный failure-reporting слой поверх state comments:

- при ошибке скрипт публикует обычный orchestration state `failed` с маркером `<!-- orchestration-state:v1 -->`;
- дополнительно публикуется structured failure report с маркером `<!-- orchestration-agent-failure:v1 -->`;
- failure report содержит stage, error, branch/base branch, runner, agent, model, run id, timestamp и hints для rerun/debug;
- issue получает label `auto:agent-failed`; label создается автоматически, если отсутствует;
- при успешном/no-op завершении processed issue скрипт пытается снять `auto:agent-failed`, если он был ранее поставлен;
- в `--dry-run` комментарии и labels не меняются, но печатается план действий.

Документ фиксирует текущее поведение по коду и тестам в репозитории на момент создания, без описания будущих изменений.
