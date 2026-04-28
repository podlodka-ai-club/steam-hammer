# Отличие текущего состояния от North Star

Документ фиксирует оставшийся разрыв между текущим состоянием репозитория и целевым состоянием из `northstar.md` после North Star batch.

## Краткая оценка

MVP уже в основном собран.

- Есть Go CLI surface: `init`, `doctor`, `autodoctor`, `run issue`, `run pr`, `run daemon`.
- Есть рабочий GitHub runner с issue flow, PR review flow, state comments, recovery, decomposition, child issues, scope guards, workflow checks, detached worker registry/status surfaces и базовым daemon polling.
- После #204 / #207 branch/repo ownership guards и smoke criteria для detached batches больше не являются отсутствующей частью дизайна; они уже зафиксированы в текущем поведении и operator docs.
- Оставшийся разрыв теперь уже не про базовую безопасность worker ownership, а про автоматизацию, production confidence и архитектурный перенос.

## Что больше не является gap

Следующие пункты больше нельзя описывать как отсутствующие:

- нет Go CLI entrypoint;
- нет `init` / `doctor` / `autodoctor` / `run issue` / `run pr` / `run daemon`;
- нет state comments и recovery из tracker comments;
- нет failure reporting и machine-readable orchestration states;
- нет decomposition preflight и child issue creation;
- нет project config для scope/workflow/readiness/merge/presets/budgets;
- нет базового daemon/polling mode;
- нет branch/repo ownership guards для detached workers;
- нет traceable worker registry/status surface для проверки `issue -> branch -> clone_path -> linked PR`.

## Оставшиеся реальные gaps

### 1. Реальный smoke execution для detached/autonomous режима

Документация и safety criteria уже есть, но устойчивый подтвержденный runtime path еще не закрыт автоматикой.

Остается сделать:

- регулярно прогоняемый smoke path на чистом `main`, а не только задокументированный checklist;
- подтвержденный сценарий для 2-3 detached workers с проверкой ownership boundaries перед merge;
- более надежный post-restart path для long-running autonomous runs, чтобы smoke был repeatable, а не ad-hoc.

### 2. Autonomous merge queue

PR readiness и verification states уже есть, но оркестратор пока не доводит несколько задач через автономную очередь merge как обычный рабочий режим.

Остается сделать:

- явную merge-queue/promotion логику поверх существующих readiness signals;
- policy-complete переход от `ready-to-merge` к фактическому merge без ручного диспетчерирования каждого PR;
- безопасную batch/post-batch verification связку, чтобы merge queue опиралась на наблюдаемый verification signal.

### 3. Полный перенос execution core в Go

Go CLI уже есть, но основной orchestration runtime все еще живет в Python runner'е.

Остается сделать:

- перенести policy/state/execution loop из Python в Go без потери текущего внешнего поведения;
- сократить зависимость новых возможностей от монолитного Python runner'а;
- сделать Go core фактическим runtime, а не только CLI/bootstrap layer.

### 4. Provider abstraction

Текущая реализация все еще GitHub-centric, даже если North Star уже описывает несколько tracker/code-host направлений.

Остается сделать:

- отделить provider interfaces от GitHub-specific runtime assumptions;
- довести tracker/code host split до рабочего runtime abstraction layer;
- добавить production-grade adapters beyond current GitHub path.

## Что не стоит больше называть gap'ом

- branch/repo ownership guards для concurrent detached workers;
- worker registry/status surfaces, по которым можно восстановить ownership chain;
- documentation-only описание detached smoke criteria без привязки к текущему поведению;
- базовый daemon/polling entrypoint как таковой.

`northstar.md` может продолжать описывать эти вещи как целевое состояние, но текущие gap docs должны отделять уже реализованные safety invariants от еще не автоматизированного runtime behavior.

## Ближайшие приоритеты

1. Прогнать и закрепить repeatable smoke execution на чистом `main` для detached/autonomous path.
2. Довести autonomous merge queue поверх readiness/verification states.
3. Продолжить перенос execution core из Python в Go.
4. Вынести provider abstraction из GitHub-centric runtime.

## Итог

Главный оставшийся разрыв с North Star больше не в отсутствии базовой оркестрации или ownership guardrails. Он сместился в четыре конкретные зоны: реальный smoke execution для автономного режима, autonomous merge queue, фактический Go execution core и provider-agnostic architecture.
