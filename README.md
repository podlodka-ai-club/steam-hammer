# Оркестратор AI-фиксов для Issue и PR

## О проекте

Этот репозиторий автоматизирует цикл работы с задачами: берёт issue и комментарии ревью, запускает AI-агента, ведёт оркестрационное состояние и подготавливает изменения для merge-процесса.

## Быстрый старт

Требования:

- Python 3.10+
- `gh` (GitHub CLI) с авторизацией
- `claude` (по умолчанию) или `opencode`

Примеры запуска:

```bash
python scripts/run_github_issues_to_opencode.py --repo owner/repo --limit 1
python scripts/run_github_issues_to_opencode.py --repo owner/repo --issue 31 --runner opencode --agent build
```

## Основные команды

- Проверка окружения: `python scripts/run_github_issues_to_opencode.py --doctor --repo owner/repo`
- Запуск по issue: `python scripts/run_github_issues_to_opencode.py --repo owner/repo --issue 31`
- Запуск по PR-комментариям: `python scripts/run_github_issues_to_opencode.py --repo owner/repo --pr 72 --from-review-comments`
- Статус автономной сессии: `python scripts/run_github_issues_to_opencode.py --repo owner/repo --status --autonomous-session-file .orchestrator/session.json`

## Документация

- Полная (расширенная) версия README: [`docs/readme-full.md`](docs/readme-full.md)
- Архитектура Go-обёртки: [`docs/orchestrator-go-architecture.md`](docs/orchestrator-go-architecture.md)
- Границы оркестрационного состояния: [`docs/orchestration-state-boundaries.md`](docs/orchestration-state-boundaries.md)
- Чеклист smoke-проверок daemon: [`docs/daemon-smoke-test.md`](docs/daemon-smoke-test.md)
- Шаблон Jira-issue для QA: [`docs/jira-issue-template.md`](docs/jira-issue-template.md)
- Ретроспективы запусков: [`retro/`](retro/)
