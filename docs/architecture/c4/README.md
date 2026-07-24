<!-- generated-by: gsd-doc-writer -->

# Просмотр и проверка C4

Файл `workspace.dsl` — единственный источник истины для всех C4-представлений проекта. Не исправляйте экспортированные изображения вручную: сначала измените DSL, затем повторите проверку и экспорт.

## Представления

| Ключ | Уровень | Назначение | Mermaid |
|---|---|---|---|
| `C1-SystemContext` | C1 System Context | Пользователи, бот и внешние системы | [файл](diagrams/structurizr-C1-SystemContext.mmd) |
| `C2-Containers` | C2 Container | FastAPI, Worker, Beat, migration, PostgreSQL и Redis | [файл](diagrams/structurizr-C2-Containers.mmd) |
| `C3-ApiComponents` | C3 Component | Внутреннее устройство FastAPI API | [файл](diagrams/structurizr-C3-ApiComponents.mmd) |
| `C3-WorkerComponents` | C3 Component | Оркестрация диалога, отправка и интеграции Worker | [файл](diagrams/structurizr-C3-WorkerComponents.mmd) |
| `OrderLifecycle` | Dynamic | Путь заказа от сообщения до подтверждения | [файл](diagrams/structurizr-OrderLifecycle.mmd) |
| `ProductionDeployment` | Deployment | Размещение Compose-сервисов и внешних API | [файл](diagrams/structurizr-ProductionDeployment.mmd) |

## Открыть локально

Из корня проекта:

```powershell
docker compose -f .\docs\architecture\c4\docker-compose.yml up
```

После запуска откройте `http://localhost:8081`. Остановить просмотр:

```powershell
docker compose -f .\docs\architecture\c4\docker-compose.yml down
```

Порт можно изменить:

```powershell
$env:C4_PORT = "8091"
docker compose -f .\docs\architecture\c4\docker-compose.yml up
```

## Проверить DSL

PowerShell:

```powershell
$c4 = (Resolve-Path .\docs\architecture\c4).Path
docker run --rm --mount "type=bind,source=$c4,target=/usr/local/structurizr" `
  structurizr/structurizr validate `
  -workspace /usr/local/structurizr/workspace.dsl
```

Bash:

```bash
docker run --rm \
  --mount "type=bind,source=$(pwd)/docs/architecture/c4,target=/usr/local/structurizr" \
  structurizr/structurizr validate \
  -workspace /usr/local/structurizr/workspace.dsl
```

Команда должна завершиться с кодом `0`.

## Экспорт для Wiki

Для GitLab Wiki удобнее экспортировать Mermaid:

```powershell
$c4 = (Resolve-Path .\docs\architecture\c4).Path
docker run --rm --mount "type=bind,source=$c4,target=/usr/local/structurizr" `
  structurizr/structurizr export `
  -workspace /usr/local/structurizr/workspace.dsl `
  -format mermaid `
  -output /usr/local/structurizr/diagrams
```

В Wiki создайте страницу `Архитектура` с кратким описанием и ссылкой на `docs/architecture/README.md`. Репозиторий остаётся источником истины; Wiki используется как навигация.
