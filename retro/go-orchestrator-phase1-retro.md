# Retro: Go orchestrator phase 1 и merge stacked PRs

## Контекст сессии

Сессия началась с проектирования архитектуры Go-оркестратора по `northstar.md`. Результат был оформлен в `docs/orchestrator-go-architecture.md`, открыт PR #70 и смержен в `main`.

После этого были заведены и выполнены задачи первой фазы Go wrapper migration:

- #71 Create Go CLI skeleton for orchestrator → PR #75 merged.
- #72 Add pythoncompat runner adapter → PR #77 merged.
- #73 Preserve compatibility with existing runner flags → PR #78 merged.
- #74 Keep orchestration state comments v1 compatible → PR #79 merged.

Все задачи запускались через текущий Python runner `scripts/run_github_issues_to_opencode.py` с OpenCode backend.

## Что прошло хорошо

- Оркестратор успешно создал issue branches и PRs для всех задач первой фазы.
- State comments и recovery context помогали повторно заходить в уже созданные PRs.
- PR-review/check path корректно распознавал linked PR и мог делать sync-only rerun.
- `go test ./...` использовался как основной verification gate для Go wrapper changes.
- Для state compatibility был добавлен focused Python regression test на round-trip `<!-- orchestration-state:v1 -->`.
- Все PRs первой фазы были в итоге смержены, а issues #71-#74 закрылись автоматически.

## Главные проблемы сессии

### 1. Runner не поддерживал linked worktree preflight

В текущем opencode worktree `.git` является file, а не directory. Python runner проверял repo через `os.path.isdir(path/.git)` и поэтому `doctor` падал с:

```text
not a git repository
```

Локально был сделан минимальный compatibility fix через `os.path.exists(path/.git)`, но он не был включен в phase 1 PR chain.

Правильное улучшение: заменить проверку `.git` на `git rev-parse --is-inside-work-tree` во всех preflight paths.

### 2. Новые файлы агента сначала не попали в PR #75

В issue #71 агент создал:

- `go.mod`;
- `cmd/orchestrator/main.go`;
- `internal/cli/app.go`;
- `internal/cli/app_test.go`.

Но первый PR #75 содержал только `README.md`. Новые файлы остались untracked в локальном worktree и были добавлены вручную отдельным commit.

Это подтверждает уже зафиксированный systemic risk из других retros: runner должен безопасно staging'ить intentional new files, созданные agent'ом, а не только modified tracked files.

### 3. Stacked PRs конфликтовали после squash merge

Задачи #72-#74 были запущены stacked друг от друга:

```text
#75 -> #77 -> #78 -> #79
```

После squash merge нижнего PR следующий PR при retarget на `main` становился `CONFLICTING`, потому что GitHub больше не видел исходные stacked commits как уже примененные.

Решение в сессии:

- merge нижнего PR;
- retarget следующего PR на `main`;
- если конфликтует, rerun issue через оркестратор;
- проверка diff/tests;
- merge следующего PR.

Это сработало, но потребовало ручного контроля.

### 4. Sync-only conflict recovery может потерять intended diff

Для PR #78 sync-only rerun разрешил конфликт, но PR diff стал пустым. Это опасное состояние: runner сообщил sync success, но содержательная работа issue была фактически потеряна.

Пришлось перезапустить #73 с `--force-issue-flow`, чтобы agent заново применил изменения поверх актуального `main`.

Улучшение: после conflict recovery runner должен проверять PR diff. Если diff неожиданно пустой или стал меньше expected scope, нельзя считать задачу готовой.

### 5. Rerun может изменить scope реализации

При rerun #73 агент сначала сузил compatibility boundary и сделал `--limit` / `--state` unsupported, хотя они нужны для README batch examples и соответствуют phase 1 compatibility goal.

Это было поймано review pass'ом перед merge и исправлено вручную: `--limit` / `--state` снова forwarding'ятся в Python runner, добавлены tests и README examples.

Вывод: после rerun нельзя доверять только статусу `MERGEABLE`; нужно перечитывать diff и сверять его с acceptance criteria.

## Что улучшить в runner/orchestrator

1. **Git worktree compatibility**
   - Проверять repo через `git rev-parse --is-inside-work-tree`, а не через `.git` directory.

2. **Intentional new files staging**
   - До agent run сохранить baseline untracked files.
   - После agent run добавить в commit modified/deleted tracked files и newly created files, которых не было в baseline.
   - После commit fail/warn, если остались new untracked files from agent.

3. **Post-run PR validation**
   - Проверять `gh pr diff --name-only` после PR creation/update.
   - Сравнивать files с acceptance criteria и agent summary.
   - Блокировать merge, если expected files отсутствуют.

4. **Stack-aware merge workflow**
   - Добавить runbook или команду для stacked PR chain: merge bottom, retarget next, rerun on conflict, validate, continue.
   - Для squash merge автоматически ожидать conflict/rebase work on upper PRs.

5. **Diff preservation after conflict recovery**
   - После sync/rebase/merge conflict recovery проверять, что PR diff не стал пустым неожиданно.
   - Если diff потерян, запускать issue-flow rerun или блокироваться с evidence.

6. **Isolated worktrees by default**
   - Не переключать один и тот же worktree между `main` и issue branches.
   - Для one-shot reruns и daemon mode использовать отдельный worktree на задачу.

7. **Better acceptance criteria tracking**
   - Issue body должен превращаться в checklist expected outputs.
   - Runner должен проверять хотя бы простые criteria: expected files, commands, docs/tests.

## Практический merge runbook из сессии

Для stacked PRs, если используется squash merge:

```text
1. Проверить top branch локально: go test ./..., focused tests.
2. Merge lowest PR.
3. Retarget next PR to main.
4. Если PR conflicting, rerun issue через runner с --force-reprocess.
5. Проверить PR diff после rerun.
6. Если diff пустой или scope изменился, rerun с --force-issue-flow или исправить вручную.
7. Повторить до конца stack.
```

## Итог

Phase 1 Go wrapper был доведен до merge, но сессия показала, что основной риск сейчас находится не в генерации кода, а в delivery mechanics:

- корректное staging новых файлов;
- поддержка linked worktrees;
- безопасный merge stacked PRs;
- проверка фактического PR diff после rerun/sync;
- недоверие к agent summary без независимой validation.

Эти улучшения стоит закрыть до расширения daemon/autonomous mode, иначе автономный runner будет создавать внешне успешные PRs с неполным или потерянным содержимым.
