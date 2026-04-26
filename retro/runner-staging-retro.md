# Retro: запуск blocker #81 и точки улучшения

## Что произошло

- Создан blocker #81: runner не коммитит intentional new files, потому что использует или использовал подход вроде `git add -u`.
- Запуск был начат командой:

```bash
python3 scripts/run_github_issues_to_opencode.py \
  --repo podlodka-ai-club/steam-hammer \
  --issue 81 \
  --runner opencode \
  --agent build \
  --model openai/gpt-5.3-codex-spark \
  --opencode-auto-approve \
  --agent-idle-timeout-seconds 900
```

- Runner создал ветку:

```text
issue-fix/81-fix-runner-staging-so-intentional-new-fi
```

- Codex Spark начал exploration:
  - сделал `Glob "*"`;
  - сделал grep по `git add -u|git add --update|git add`;
  - получил `0 matches`.
- После этого запуск был abort’нут пользователем.

## Главные проблемы

1. **#12 показал реальный баг runner’а**
   - Агент создал `docs/jira-issue-template.md`.
   - Но PR #80 включил только `README.md`.
   - Новый файл остался untracked локально.
   - Значит runner/commit path не умеет безопасно включать intentional new files.

2. **PR validation не поймал неполный результат**
   - PR #80 был `MERGEABLE/CLEAN`, но не удовлетворял acceptance criteria.
   - Проверка “PR mergeable + tests pass” недостаточна.
   - Нужно минимум проверять residual untracked files после agent run.

3. **Codex Spark начал с неудачного поиска**
   - Он искал literal `git add -u`, но получил 0 matches.
   - Возможные причины:
     - код уже изменился после fast-forward main;
     - staging logic переехала/переименована;
     - нужно искать `commit_changes`, `stage`, `git`, `add`, `run_command([...])`, а не только exact string.

4. **Issue #81 недостаточно указывает кодовую точку**
   - В issue описан симптом, но нет точного файла/функции.
   - Для агента лучше сразу указать:
     - где находится commit/staging logic;
     - что нужно сравнить baseline untracked до agent run и после.

## Точки улучшения runner’а

1. **Baseline untracked tracking**
   - Перед agent run сохранить список untracked файлов.
   - После agent run вычислить новые untracked файлы.
   - Коммитить:
     - modified/deleted tracked files;
     - newly created files, которых не было в baseline.
   - Не коммитить pre-existing unrelated untracked.

2. **Fail/warn on residual untracked after commit**
   - После commit перед PR:
     - если остались новые untracked файлы, созданные agent’ом — fail или comment blocker.
   - Это бы сразу поймало #12.

3. **PR content validation**
   - Перед merge проверять changed files against acceptance criteria хотя бы вручную или heuristic.
   - Для #12 должно было быть очевидно:
     - expected: `docs/jira-issue-template.md`;
     - actual PR files: only `README.md`.

4. **Better issue prompts for blockers**
   - В blocker issue добавлять:
     - observed branch/PR;
     - exact missing file;
     - suspected function/path;
     - suggested algorithm.
   - Это снизит шанс, что Spark уйдёт в пустой grep.

5. **Agent search strategy**
   - Если grep по exact phrase дал 0 matches, agent должен автоматически искать broader terms:
     - `commit_changes`;
     - `run_command`;
     - `git`;
     - `stage`;
     - `has_changes`;
     - `status --porcelain`.
   - Запуск остановился до такой адаптации.

## Текущий статус на момент retro

- #11 / PR #76 — open, incomplete, не merge’ить.
- #12 / PR #80 — open, incomplete, не merge’ить.
- #81 — blocker, started, но запуск abort’нут до изменений.
- Локальная ветка после запуска могла остаться на `issue-fix/81-...`; перед продолжением лучше проверить status/branch.
