# Верхнеуровневый план движения к North Star

Документ описывает последовательность крупных этапов, которые переводят текущий GitHub/Python-прототип в автономный Go-оркестратор из `northstar.md`.

## Принцип движения

Идём итеративно: сначала усиливаем уже работающий GitHub-based прототип, затем оформляем продуктовый слой вокруг него, потом переносим ядро в Go и расширяем автономность.

Главный критерий прогресса — не количество интеграций, а способность оркестратора всё чаще доводить задачу от issue до готового PR/MR, а затем до merge без ручного управления.

## Этап 1. Зафиксировать текущее ядро

Цель: сделать текущий прототип надёжной основой для дальнейших задач.

- Зафиксировать `northstar.md` и `docs/northstar-gap.md` как продуктовые ориентиры.
- Поддерживать актуальность `docs/current-behavior.md`.
- Укрепить тесты вокруг текущих GitHub issue/PR сценариев.
- Явно описать поддерживаемые режимы: issue-flow, pr-review, dry-run.
- Сделать поведение повторных запусков предсказуемым и документированным.

Результат: текущий runner понятен, тестируем и безопасен для дальнейшего развития.

## Этап 2. Добавить state и прозрачность через issue/PR comments

Цель: сделать tracker источником истины и подготовить восстановление после сбоев.

- Ввести стандартизированные комментарии состояния в issue/PR.
- Фиксировать branch, PR, runner, model, статус, попытку, next action.
- Добавить статусы: `in-progress`, `blocked`, `waiting-for-author`, `waiting-for-ci`, `ready-for-review`, `ready-to-merge`.
- Писать краткие happy-path статусы и подробные failure reports при проблемах.
- Научиться восстанавливать контекст из issue/PR comments.

Результат: оркестратор может продолжать работу без отдельной базы состояния.

## Этап 3. Project config, scope и doctor

Цель: сделать подключение к проекту управляемым и безопасным.

- Добавить проектный конфиг для scope rules.
- Разрешить allow/deny правила по labels, author, типу задачи, area, workflow.
- Добавить базовые workflow команды: setup/test/lint/build.
- Сделать `doctor`: проверка git, auth, `gh`, runner binaries, clean worktree, config.
- Сделать `autodoctor`: диагностика и предложения, что настроить дальше.
- Подготовить `init`, который создаёт минимальный config scaffold.

Результат: новый проект можно подключить предсказуемо, а оркестратор понимает, какие задачи можно брать.

## Этап 4. Presets, routing и эскалация

Цель: управлять стоимостью, качеством и сложностью выполнения.

- Ввести presets: например `cheap`, `default`, `hard`.
- Привязать presets к runner/model/agent.
- Добавить routing rules по labels, типам задач и project config.
- Добавить retry policy и max attempts.
- Добавить эскалацию модели/runner'а после неудач.
- Добавить базовые budget limits: время, количество попыток, допустимый уровень модели.

Результат: оркестратор выбирает исполнителя не вручную, а по правилам проекта и сложности задачи.

## Этап 5. CI и review loop

Цель: приблизиться к самостоятельному доведению PR до готовности.

- Научиться ждать GitHub checks.
- Читать статусы и логи failing CI.
- Отличать transient failure от реальной ошибки.
- Запускать агента на исправление CI failure с контекстом логов.
- Продолжить развитие PR review mode.
- Комментировать неожиданные failures с evidence и next hypothesis.

Результат: оркестратор не просто создаёт PR, а пытается довести его до зелёного состояния.

## Этап 6. Go CLI как продуктовая оболочка

Цель: перейти от скрипта к устанавливаемому CLI-приложению.

- Спроектировать Go CLI: `init`, `doctor`, `run issue`, `run pr`, `run daemon`.
- На первом шаге Go CLI может вызывать существующий Python runner как compatibility layer.
- Постепенно переносить core logic в Go.
- Вынести интерфейсы: tracker, code host, runner, workflow, notifier.
- Оставить Python-прототип как reference implementation до завершения миграции.

Результат: появляется стабильная продуктовая точка входа и путь к одному компилируемому бинарнику.

## Этап 7. Daemon/autonomous mode

Цель: перейти от one-shot выполнения к автономной работе.

- Добавить polling loop.
- Выбирать задачи по project rules.
- Claim/lock задачи через tracker state comments или labels.
- Соблюдать лимиты параллельности.
- Восстанавливаться после рестарта.
- Закрывать или помечать устаревшие PR по правилам проекта.

Результат: оркестратор может периодически брать задачи и вести их без ручного запуска каждой issue.

## Этап 8. Multi-provider architecture

Цель: выйти за пределы GitHub.

- Разделить tracker provider и code hosting provider.
- Стабилизировать GitHub adapter.
- Добавить Bitbucket adapter.
- Добавить custom API proxy adapter.
- Не завязывать core workflow на `gh` как единственный способ интеграции.

Результат: оркестратор можно подключать к разным системам управления задачами и кодом.

## Этап 9. Merge, deploy и messenger integrations

Цель: довести цикл доставки до конца.

- Добавить `ready-to-merge` policy.
- Добавить автоматический merge, если project rules это разрешают.
- Добавить deploy hooks после merge.
- Добавить Slack/Telegram/Discord или другой notifier abstraction.
- Использовать мессенджеры для уточнений, статусов, review requests и blockers.

Результат: оркестратор закрывает полный путь от задачи до merge/deploy, вовлекая человека только при необходимости.

## Предлагаемый порядок ближайших задач

1. State comments в issue/PR.
2. Failure comments с evidence и next hypothesis.
3. `doctor` для текущего Python-прототипа.
4. Project config scaffold.
5. Scope rules.
6. Presets и retry/escalation policy.
7. GitHub CI status reader.
8. Go CLI wrapper.
9. `init` и `run issue` в Go CLI.
10. Daemon polling mode.

## Итоговая траектория

Текущий прототип уже решает важную часть MVP: GitHub issue/PR + Claude/OpenCode + branch/PR automation.

Дальше нужно добавлять не просто новые команды, а продуктовый слой автономности: состояние, scope, диагностику, routing, CI/review loop, Go CLI, daemon и provider abstractions.
