# Retro: ключевые наблюдения по всей сессии

## Контекст сессии

Целью сессии было продолжать движение по GitHub issues в `podlodka-ai-club/steam-hammer`, используя GitHub issues/PRs как tracker и `scripts/run_github_issues_to_opencode.py` как основной orchestration runner.

В середине сессии был задан новый предпочтительный worker: **Codex Spark** (`openai/gpt-5.3-codex-spark`) через `opencode` agent `build`.

Основной рабочий паттерн:

```bash
python3 scripts/run_github_issues_to_opencode.py \
  --repo podlodka-ai-club/steam-hammer \
  --issue <N> \
  --runner opencode \
  --agent build \
  --model openai/gpt-5.3-codex-spark \
  --opencode-auto-approve \
  --agent-idle-timeout-seconds 900
```

## Что было успешно завершено

### Core roadmap до Codex Spark

До перехода на Codex Spark уже был закрыт существенный набор задач по Northstar MVP:

- #56 doctor diagnostics → PR #64 merged.
- #57 project config scaffold → PR #65 merged.
- #58 initial scope rules → PR #66 merged.
- #60 workflow command execution → PR #67 merged.
- #62 GitHub CI status reader → PR #68 merged.

Эти задачи укрепили runner как orchestration tool: диагностика, конфигурация, scope rules, workflow checks, чтение CI статусов.

### #10 image attachments

#10 был реализован через PR #69 и смержен.

Позитивные моменты:

- Codex Spark смог доработать PR-review feedback.
- Были добавлены docs для image attachment behavior.
- Полный unittest suite проходил локально перед merge.
- PR был clean/mergeable и был squash-merged.

Важное наблюдение:

- В логах agent claimed, что добавлен `tests/test_issue_image_support.py`, но merged diff в итоге не содержал этот файл.
- Это ранний сигнал той же категории проблем, что позже явно проявилась в #12: agent-created new files могут не попадать в commit/PR.

## Что не получилось завершить

### #61 presets/retry policy

#61 несколько раз не дошёл до результата:

- обычный Codex run завис без вывода на 900s;
- Codex Spark run тоже не завершился в outer timeout, оставив partial uncommitted changes;
- изменения были отброшены, задача осталась open/failed.

Наблюдение:

- #61 выглядит слишком широкой для текущего runner/worker setup.
- Её стоит разделить на меньшие issues:
  1. presets config only;
  2. retry policy only;
  3. escalation/failure reporting integration.

### #11 Jira tracker support

#11 был запущен через Codex Spark и создал PR #76, но PR оказался крайне неполным.

Фактический результат PR #76:

- добавлены tracker constants/defaults;
- добавлены helper functions для issue key normalization;
- не реализованы acceptance criteria:
  - нет полноценного `--tracker github|jira` CLI flow;
  - нет Jira env validation;
  - нет `fetch_jira_issue` / `fetch_jira_issues`;
  - нет runtime dispatch;
  - нет README docs;
  - нет tests.

Наблюдение:

- Agent repeatedly produced planning summaries instead of finishing implementation.
- PR-review retry на тот же PR не исправил ситуацию: diff остался scaffold-only.
- Для таких широких integrations Codex Spark нуждается в более узком issue или более сильном prompt with exact files/functions/tests.

### #12 Jira issue template

#12 был простым docs task и Codex Spark частично справился:

- создал `docs/jira-issue-template.md` локально;
- обновил README;
- создал PR #80.

Но PR #80 оказался incomplete:

- в PR попал только `README.md`;
- `docs/jira-issue-template.md` остался untracked локально;
- acceptance criteria не выполнены.

Это привело к blocker #81.

## Главный системный баг: intentional new files не попадают в PR

Наиболее важное открытие сессии — runner может терять новые файлы, созданные agent’ом.

Симптомы:

- #12: required file `docs/jira-issue-template.md` создан, но не committed.
- #10: в output упоминался новый test file, но merged diff его не содержал.
- После PR creation локально появляются untracked files, которые выглядят как intended artifacts.

Вероятная причина:

- staging path слишком осторожный после #7 и не staging’ит untracked files.
- Это защищает от случайных pre-existing untracked files, но ломает legitimate agent-created files.

Правильная модель:

1. До agent run сохранить baseline untracked files.
2. После agent run вычислить new untracked files.
3. В commit включать:
   - modified/deleted tracked files;
   - newly created files, которых не было в baseline.
4. Не включать pre-existing unrelated untracked files.
5. После commit проверять residual untracked:
   - если есть новые files from agent, которые не попали в commit — fail/comment blocker.

## Проблемы с validation перед merge

Сессия показала, что `mergeable + tests pass` недостаточно.

Нужны дополнительные проверки:

### 1. Changed files vs acceptance criteria

Для #12 acceptance criteria явно требовал:

- `docs/jira-issue-template.md` exists.

Но PR files были:

- `README.md` only.

Такой mismatch должен автоматически или полуавтоматически блокировать merge.

### 2. Residual untracked files после runner

Если после runner остаются untracked files — это не всегда мусор.
Нужно классифицировать:

- pre-existing untracked before run → не трогать;
- new untracked after run → вероятный intended output или agent mistake;
- new untracked matching acceptance criteria → blocker, если не included in PR.

### 3. Verify PR diff, not только local test output

Agent output может утверждать, что tests/docs добавлены, но PR diff может отличаться.
Надо проверять:

```bash
gh pr diff <N> --name-only
git diff --stat origin/main...HEAD
```

## Наблюдения по Codex Spark как worker

Плюсы:

- Хорошо справляется с локальными docs/simple edits.
- Может реагировать на PR-review comments.
- Может запускать unittest и валидировать изменения.

Минусы:

- На широких архитектурных задачах часто уходит в exploration/planning.
- Может заявлять Done, когда PR фактически incomplete.
- Не всегда замечает, что new files остались untracked.
- Иногда начинает с слишком узкого поиска (`git add -u`) и не расширяет search strategy после zero matches.

Рекомендации:

- Для Codex Spark давать smaller scoped issues.
- В issue body добавлять exact suspected functions/files.
- В PR-review comments указывать explicit missing files/tests/docs.
- После каждого run проверять PR diff independently.

## Наблюдения по runner orchestration

### Хорошо работает

- Создание branches/PRs.
- State comments в issues/PRs.
- Reusing linked PR для PR-review mode.
- Комментарии с failure/blocked evidence.
- Merge flow для clean/mergeable PRs.

### Требует улучшения

1. **Commit/staging model**
   - Нужна поддержка intentional new files.

2. **Completion detection**
   - Runner считает task processed, если PR создан, даже если acceptance criteria incomplete.

3. **Agent output trust boundary**
   - Нельзя доверять только тексту agent summary.
   - Нужно проверять actual diff.

4. **Review retry effectiveness**
   - PR-review retry может оставить PR почти без изменений.
   - Нужен критерий “review comment addressed” через diff/expected checks.

5. **Issue splitting**
   - Большие tasks (#11, #61) надо автоматически предлагать split/blocker subtasks.

## Рекомендованный порядок дальнейших действий

1. **Сначала закрыть #81**
   - Без fix intentional new files дальнейшие docs/test tasks будут ненадёжны.

2. **После #81 возобновить #12 / PR #80**
   - Добавить missing `docs/jira-issue-template.md` в PR.
   - Проверить PR files before merge.

3. **Потом вернуться к #11**
   - Не пытаться закрыть всё одним PR, если Spark продолжает scaffold-only.
   - Разбить на подзадачи:
     - `--tracker` parser/config only;
     - Jira env validation only;
     - Jira single issue fetch only;
     - Jira list/search only;
     - docs/tests.

4. **#61 оставить split-candidate**
   - presets и retry/escalation policy лучше делать отдельными small PRs.

## Concrete follow-up checks для каждого будущего PR

Перед merge проверять:

```bash
gh pr view <N> --json mergeable,mergeStateStatus,statusCheckRollup,closingIssuesReferences
gh pr diff <N> --name-only
git status --short --branch
```

И дополнительно спросить:

- Есть ли required files from acceptance criteria в PR diff?
- Остались ли untracked files после runner?
- Совпадает ли agent summary с фактическим diff?
- Есть ли tests/docs, если они были явно requested?

## Summary

Сессия была продуктивной для core roadmap и #10, но выявила два ключевых системных риска:

1. Runner может создавать PR без intentional new files.
2. Agent/runner может считать задачу completed по факту PR creation, не проверив acceptance criteria.

Главный следующий engineering improvement — #81: безопасный staging новых файлов через baseline untracked tracking и post-run residual validation.
