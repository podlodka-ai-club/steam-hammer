# Верхнеуровневый план движения к North Star

Документ описывает обновленную траекторию после North Star batch. Базовый MVP больше не нужно планировать с нуля: его нужно закрепить, сделать наблюдаемым и постепенно перенести из Python-centered runner'а в product-grade Go orchestrator.

## Текущая точка

Сейчас репозиторий уже имеет:

- Go CLI surface: `init`, `doctor`, `autodoctor`, `run issue`, `run pr`, `run daemon`;
- рабочий GitHub issue/PR orchestration flow;
- state comments, recovery, failure reporting и decomposition;
- project config для scope/workflow/readiness/presets/budgets;
- ранний daemon mode поверх текущего Python runner'а.

Это означает, что MVP mostly implemented. Дальнейшая дорожная карта должна концентрироваться не на повторном добавлении уже существующих функций, а на hardening и beta/target-state gaps.

## Этап 1. Hardening текущего MVP

Цель: сделать уже реализованный surface надежным для повседневного использования.

- Прогнать и задокументировать daemon smoke test на чистом `main`.
- Упростить и стабилизировать full verification path.
- Снизить шум Python suite и expected mocked `gh` warnings.
- Убедиться, что docs о текущем поведении, gap'ах и roadmap остаются синхронизированы с кодом.

Результат: MVP не только существует, но и дает предсказуемый operational baseline.

## Этап 2. Status visibility и operator UX

Цель: сделать orchestration state легко читаемым без ручного разбора issue/PR comments и длинных логов.

- Добавить `status`-style summary для issue/PR/daemon state.
- Показать краткий прогресс batch/daemon runs и явный `next_action`.
- Сделать blocked/waiting states проще для чтения оператором.

Результат: оператор быстрее понимает, где застряла задача и что делать дальше.

## Этап 3. Conflict recovery и post-batch verification

Цель: уменьшить ручную боль после merge waves и при reused branches.

- Добавить dedicated conflict-recovery mode.
- Разделить branch sync recovery и повторный full issue run.
- Автоматизировать post-batch verification.
- При необходимости автоматически создавать follow-up issue/checklist на найденные регрессии.

Результат: крупные batch merges перестают требовать столько ручного контроля и повторных полных rerun'ов.

## Этап 4. Modularization перед переносом core в Go

Цель: сократить conflict surface и подготовить безопасный перенос логики.

- Разбить крупный Python runner на меньшие модули по зонам ответственности.
- Выделить boundaries для state, policy, workflow, recovery и provider access.
- Сохранить текущее поведение тестами перед переносом.

Результат: дальнейший перенос в Go становится инженерно управляемым, а не big-bang rewrite.

## Этап 5. Beta autonomy polish

Цель: довести автономный режим до устойчивого beta-level поведения.

- Усилить daemon resume/retry semantics.
- Сделать более зрелый task selection, claiming и concurrency control.
- Довести presets, escalation и budget-aware routing до понятного default behavior.
- Улучшить CI/review progression до более надежного self-serve loop.

Результат: оркестратор стабильно ведет задачи без постоянного ручного подталкивания.

## Этап 6. Product-grade Go core

Цель: перейти от Go wrapper + Python core к полноценному Go orchestrator.

- Перенести execution core, policy engine и state handling в Go.
- Сохранить совместимость на уровне существующего CLI surface.
- Постепенно убрать зависимость от монолитного Python runner'а.

Результат: основной orchestration runtime становится компилируемым, модульным и легче расширяемым.

## Этап 7. Multi-provider и end-to-end target state

Цель: выйти за пределы текущего GitHub-only execution path.

- Ввести production-grade provider split: tracker, code host, runner, notifier, workflow.
- Добавить Bitbucket и custom API proxy adapters.
- Довести CI/review/merge/deploy loop до policy-driven automation.
- Добавить messenger/notifier integrations там, где это реально помогает операторам.

Результат: оркестратор приближается к полному North Star из `northstar.md`.

## Ближайший приоритетный список

1. Daemon smoke test.
2. Status visibility.
3. Test noise reduction и fast/full verification split.
4. Conflict-recovery mode.
5. Post-batch verification automation.
6. Runner modularization.
7. Beta autonomy polish.
8. Go core migration.

## Итог

Следующий этап развития репозитория уже не про “добавить базовый orchestration flow”. Он про то, чтобы сделать существующий MVP надежным, наблюдаемым и удобным для автономной эксплуатации, а затем спокойно перенести его в provider-agnostic Go core.
