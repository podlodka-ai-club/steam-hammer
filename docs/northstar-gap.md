# Отличие текущего состояния от North Star

Документ фиксирует оставшийся разрыв между текущим состоянием репозитория и целевым состоянием из `northstar.md` после North Star batch.

## Краткая оценка

MVP уже в основном собран.

- Есть Go CLI surface: `init`, `doctor`, `autodoctor`, `run issue`, `run pr`, `run daemon`.
- Есть рабочий GitHub runner с issue flow, PR review flow, state comments, recovery, decomposition, child issues, scope guards, workflow checks и базовым daemon polling.
- Основной execution core пока все еще Python-based и GitHub-centric, а beta/target-state gaps остаются вокруг надежности автономного режима, наблюдаемости и архитектурного переноса.

## Что больше не является gap

Следующие пункты больше нельзя описывать как отсутствующие:

- нет Go CLI entrypoint;
- нет `init` / `doctor` / `autodoctor` / `run issue` / `run pr` / `run daemon`;
- нет state comments и recovery из tracker comments;
- нет failure reporting и machine-readable orchestration states;
- нет decomposition preflight и child issue creation;
- нет project config для scope/workflow/readiness/merge/presets/budgets;
- нет базового daemon/polling mode.

## Оставшиеся beta gaps

### 1. Надежность daemon режима

Daemon mode уже есть, но его зрелость пока ниже MVP issue/PR flows.

Остается сделать:

- smoke-tested operator path на чистом `main`;
- более явную статусную поверхность для daemon runs и последних task states;
- более уверенное resume/retry поведение после сбоев long-running batch runs;
- понятные guardrails для post-restart continuation.

### 2. Conflict recovery для reused branches

Повторный запуск и sync branch уже реализованы, но batch retro показал, что этого недостаточно для плотных merge waves.

Остается сделать:

- dedicated conflict-recovery mode, который занимается только sync/rebase/merge resolution;
- отделение conflict recovery от полного revisit issue scope;
- более короткий и читаемый output вокруг rebase/merge fallback.

### 3. Visibility и operator ergonomics

Machine-readable state уже публикуется, но operator UX еще сырой.

Остается сделать:

- удобный `status`-style view для последнего состояния issue/PR/daemon cycle;
- краткий сводный прогресс по batch/daemon runs;
- более явные next actions для blocked/waiting states.

### 4. Verification и test signal

Workflow checks и readiness logic уже есть, но post-batch confidence и test ergonomics еще не закрыты.

Остается сделать:

- автоматический post-batch verification/checklist flow;
- follow-up issue creation при обнаружении регрессий после merge batch;
- снижение шума full Python suite, особенно вокруг ожидаемых mocked `gh` warnings;
- более быстрое разделение smoke/full verification modes.

### 5. Runner structure и переносимость

Функциональность выросла быстрее, чем внутренняя модульность Python runner'а.

Остается сделать:

- разбить крупный Python runner на меньшие модули;
- сократить conflict surface в hot files;
- упростить перенос execution core из Python в Go без повторной сборки всего поведения с нуля.

### 6. Routing и escalation как зрелая система

Presets, budgets, retry и routing config уже появились, но пока это не fully mature orchestration policy layer.

Остается сделать:

- довести automatic preset selection и escalation до предсказуемого default behavior;
- сделать retries/model escalation более прозрачными в status comments;
- связать budget decisions с реальным execution loop, а не только с config surface.

## Оставшиеся target-state gaps

### 1. Полный Go execution core

- Go CLI уже есть, но core orchestration logic все еще живет в Python runner'е.
- Еще не завершен перенос policy/state/provider/workflow execution в Go.

### 2. Multi-provider architecture

- Реализация все еще GitHub-centric.
- Нет production adapters для Bitbucket и custom API proxy.
- Provider split tracker vs code host пока не является рабочим runtime core.

### 3. Полный CI/review/merge/deploy loop

- Review mode и readiness states уже есть.
- Но еще нет полноценного autonomous cycle до merge/deploy с policy-complete execution.

### 4. Full autonomy with strong policy layer

- Scope and config groundwork уже заложены.
- Но еще нет зрелого task selection/claiming/concurrency/policy engine, который устойчиво ведет mixed workloads без ручного сопровождения.

## Ближайшие приоритеты

1. Daemon smoke test на чистом `main`.
2. Status visibility для one-shot и daemon runs.
3. Снижение test noise и выделение быстрых verification paths.
4. Dedicated conflict-recovery mode для reused branches.
5. Post-batch verification и follow-up issue/checklist automation.
6. Runner modularization перед следующим большим шагом переноса в Go.

## Итог

Главный оставшийся разрыв с North Star больше не в отсутствии базовой оркестрации. Он сместился в надежность, прозрачность и архитектурную зрелость: daemon confidence, status UX, conflict recovery, verification discipline, modularization и постепенный перенос core logic в provider-agnostic Go orchestrator.
