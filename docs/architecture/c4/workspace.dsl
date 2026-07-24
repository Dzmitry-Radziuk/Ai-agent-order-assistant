workspace "Restaurant Procurement Bot" "C4-модель Telegram-бота для закупок ресторана" {
    !identifiers flat
    !impliedRelationships true

    model {
        cafeEmployee = person "Повар / сотрудник кафе" "Создаёт и отправляет заявку на закупку через Telegram."
        procurementManager = person "Сотрудник снабжения" "Обрабатывает заявки и поддерживает каталог в Google Sheets."
        operator = person "Оператор системы" "Разворачивает приложение и контролирует его работоспособность."

        procurementBot = softwareSystem "Restaurant Procurement Bot" "Принимает товары из Telegram, ведёт черновик, проверяет каталог и надёжно отправляет заявку." {
            !docs model-docs
            !adrs decisions

            api = container "FastAPI API" "Принимает Telegram webhook, проверяет секрет, сохраняет update и ставит задачу в очередь." "Python 3.12, FastAPI, Uvicorn" {
                webhookEndpoint = component "Telegram Webhook" "Обрабатывает POST /webhooks/telegram и защищает вход секретным заголовком." "FastAPI route"
                healthEndpoints = component "Health Endpoints" "Проверяют процесс API, PostgreSQL и Redis." "FastAPI routes"
                inputNormalizer = component "Input Normalizer" "Преобразует Telegram payload в доменное событие." "services/input_normalizer.py"
                updateInbox = component "Update Repository" "Идемпотентно сохраняет Telegram updates и контрольные точки обработки." "repositories/updates.py"
                taskPublisher = component "Celery Task Publisher" "Ставит новый update в очередь обработки." "workers/tasks.py"
            }

            worker = container "Celery Worker" "Последовательно обрабатывает диалог, вызывает AI и выполняет возобновляемую отправку заявки." "Python 3.12, Celery" {
                taskHandlers = component "Task Handlers" "Запускают обработку update, отправку заявки, статусы и очистку аудита." "workers/tasks.py"
                orchestrator = component "Update Orchestrator" "Координирует блокировку чата, регистрацию, разбор, переход состояния и отправку ответа." "services/orchestrator.py"
                registration = component "Venue Registration" "Проверяет привязку пользователя и выбирает таблицу заведения." "services/venue_registration.py"
                inputParser = component "Input Parser" "Определяет намерение и извлекает товары, количество и команды." "services/parser.py, integrations/openai_client.py"
                conversationEngine = component "Conversation Engine" "Применяет бизнес-правила и формирует новое состояние и ответ." "services/engine.py"
                submission = component "Submission Service" "Идемпотентно записывает историю, обновляет каталог, запускает перерасчёт и завершает заявку." "services/submission.py"
                stateRepositories = component "State Repositories" "Хранят сессии, входящие updates, привязки и контрольные точки отправки." "repositories/*.py"
                auditRepository = component "Order Audit Repository" "Записывает ограниченный журнал заказа и удаляет устаревшие события." "repositories/order_events.py"
                telegramGateway = component "Telegram Gateway" "Скачивает медиа, подтверждает callback и отправляет или редактирует сообщения." "integrations/telegram.py"
                openaiGateway = component "OpenAI Gateway" "Распознаёт голос и фото, разбирает текст и помогает выбрать товар." "integrations/openai_client.py"
                sheetsGateway = component "Google Sheets Gateway" "Читает каталог и регистрацию, записывает историю и обновляет количество." "integrations/google_sheets.py"
                cacheAndLocks = component "Cache and Chat Locks" "Кэширует каталог и сериализует обработку одного чата." "integrations/cache.py"
            }

            scheduler = container "Celery Beat" "Ежедневно публикует задачу очистки устаревшего аудита." "Python 3.12, Celery Beat"
            migration = container "Database Migration" "Однократно применяет Alembic migrations перед запуском приложения." "Python 3.12, Alembic"

            postgres = container "PostgreSQL" "Хранит сессии, Telegram inbox, привязки заведений, отправки и аудит заказов." "PostgreSQL 17" {
                tags "Database"
            }

            redis = container "Redis" "Временно хранит очередь Celery, результаты задач, кэш и блокировки чатов." "Redis 8" {
                tags "Database"
            }
        }

        telegram = softwareSystem "Telegram Bot API" "Доставляет сообщения, callback, голос и фото; принимает ответы бота." {
            tags "External System"
        }
        openai = softwareSystem "OpenAI API" "Выполняет транскрибацию, разбор текста, анализ фото и выбор кандидата." {
            tags "External System"
        }
        googleSheets = softwareSystem "Google Sheets" "Хранит каталог заведения, регистрацию, историю товаров и заявки на новые товары." {
            tags "External System"
        }
        appsScript = softwareSystem "Google Apps Script" "Пересчитывает рабочий лист заявки после записи заказа." {
            tags "External System"
        }
        langfuse = softwareSystem "Langfuse" "Опционально принимает обезличенные трассировки обработки Telegram updates." {
            tags "External System"
        }

        cafeEmployee -> telegram "Создаёт заказ и получает ответы" "Telegram"
        cafeEmployee -> procurementBot "Использует через Telegram" "Telegram"
        procurementManager -> googleSheets "Обрабатывает заявки и поддерживает каталог" "Web UI"
        procurementManager -> procurementBot "Обрабатывает результаты через Google Sheets" "Google Sheets"
        operator -> procurementBot "Разворачивает, проверяет health и анализирует логи" "Docker Compose, HTTPS"

        telegram -> webhookEndpoint "Доставляет update" "HTTPS/JSON"
        webhookEndpoint -> inputNormalizer "Нормализует payload" "Python call"
        webhookEndpoint -> updateInbox "Сохраняет update один раз" "Python call"
        webhookEndpoint -> taskPublisher "Публикует задачу для нового update" "Python call"
        updateInbox -> postgres "Создаёт и читает Telegram inbox" "SQL"
        taskPublisher -> redis "Публикует Celery task" "Redis protocol"
        healthEndpoints -> postgres "Проверяет доступность" "SQL"
        healthEndpoints -> redis "Проверяет доступность" "Redis PING"

        redis -> taskHandlers "Доставляет Celery tasks" "Redis protocol"
        scheduler -> redis "Публикует ежедневную задачу очистки" "Redis protocol"
        migration -> postgres "Применяет схему БД" "SQL"

        taskHandlers -> orchestrator "Обрабатывает Telegram update" "Python call"
        taskHandlers -> submission "Выполняет отправку, статусы и запрос нового товара" "Python call"
        taskHandlers -> auditRepository "Удаляет устаревшие записи" "Python call"

        orchestrator -> registration "Проверяет регистрацию и контекст заведения" "Python call"
        orchestrator -> inputParser "Разбирает сообщение или callback" "Python call"
        orchestrator -> conversationEngine "Применяет переход состояния" "Python call"
        orchestrator -> stateRepositories "Читает и фиксирует состояние" "Python call"
        orchestrator -> telegramGateway "Скачивает медиа и отправляет ответ" "Python call"
        orchestrator -> openaiGateway "Передаёт текст, голос или фото" "Python call"
        orchestrator -> sheetsGateway "Загружает каталог заведения" "Python call"
        orchestrator -> cacheAndLocks "Получает каталог и блокирует чат" "Python call"

        submission -> stateRepositories "Читает и фиксирует этапы отправки" "Python call"
        submission -> auditRepository "Записывает этапы жизненного цикла заказа" "Python call"
        submission -> sheetsGateway "Записывает заявку и обновляет каталог" "Python call"
        submission -> telegramGateway "Отправляет результат пользователю" "Python call"

        registration -> stateRepositories "Читает и сохраняет привязку" "Python call"
        registration -> sheetsGateway "Читает реестр заведений" "Python call"
        stateRepositories -> postgres "Читает и записывает состояние" "SQL"
        auditRepository -> postgres "Записывает и очищает аудит" "SQL"
        cacheAndLocks -> redis "Использует кэш и distributed locks" "Redis protocol"
        telegramGateway -> telegram "Вызывает Telegram Bot API" "HTTPS/JSON"
        openaiGateway -> openai "Вызывает модели AI" "HTTPS/JSON"
        sheetsGateway -> googleSheets "Читает и изменяет таблицы" "Google Sheets API"
        submission -> appsScript "Запускает перерасчёт" "HTTPS/JSON"
        orchestrator -> langfuse "Отправляет обезличенную трассировку, если включено" "HTTPS"

        production = deploymentEnvironment "Production" {
            deploymentNode "Docker Host" "Хост, на котором объединены docker-compose.yml и docker-compose.prod.yml." "Docker Compose" {
                deploymentNode "Application Containers" "Контейнеры одного Python-образа приложения." "Docker" {
                    containerInstance api
                    containerInstance worker
                    containerInstance scheduler
                    containerInstance migration
                }
                deploymentNode "PostgreSQL Container" "Постоянная база данных с volume postgres_data." "postgres:17-alpine" {
                    containerInstance postgres
                }
                deploymentNode "Redis Container" "Временное хранилище с volume redis_data и политикой noeviction." "redis:8-alpine" {
                    containerInstance redis
                }
            }
            deploymentNode "External Services" "Внешние API, не управляемые приложением." "SaaS" {
                softwareSystemInstance telegram
                softwareSystemInstance openai
                softwareSystemInstance googleSheets
                softwareSystemInstance appsScript
                softwareSystemInstance langfuse
            }
        }
    }

    views {
        systemContext procurementBot "C1-SystemContext" "Пользователи и внешние системы вокруг бота закупок." {
            include *
            autoLayout lr 350 250
            title "C1 — Контекст системы"
        }

        container procurementBot "C2-Containers" "Исполняемые процессы и хранилища Restaurant Procurement Bot." {
            include *
            autoLayout lr 350 250
            title "C2 — Контейнеры"
        }

        component api "C3-ApiComponents" "Компоненты входного FastAPI-процесса." {
            include *
            autoLayout lr 300 220
            title "C3 — Компоненты FastAPI API"
        }

        component worker "C3-WorkerComponents" "Компоненты обработки диалога и отправки заявки." {
            include *
            autoLayout lr 300 220
            title "C3 — Компоненты Celery Worker"
        }

        dynamic procurementBot "OrderLifecycle" "Основной путь заказа от сообщения до подтверждения." {
            cafeEmployee -> telegram "1. Отправляет текст, голос, фото или callback"
            telegram -> api "2. Доставляет защищённый webhook"
            api -> postgres "3. Идемпотентно сохраняет update"
            api -> redis "4. Публикует задачу обработки"
            redis -> worker "5. Доставляет задачу worker"
            worker -> openai "6. При необходимости распознаёт и разбирает ввод"
            worker -> postgres "7. Сохраняет состояние и аудит"
            worker -> googleSheets "8. При подтверждении записывает заявку"
            worker -> appsScript "9. Запускает перерасчёт"
            worker -> telegram "10. Отправляет пользователю результат"
            autoLayout lr 300 220
            title "Динамика — жизненный цикл заказа"
        }

        deployment * production "ProductionDeployment" "Размещение Compose-сервисов и внешних API." {
            include *
            autoLayout lr 350 250
            title "Deployment — Docker Compose"
        }

        styles {
            element "Person" {
                shape Person
                background #08427b
                color #ffffff
            }
            element "Software System" {
                background #1168bd
                color #ffffff
            }
            element "External System" {
                background #6b7280
                color #ffffff
            }
            element "Container" {
                background #438dd5
                color #ffffff
            }
            element "Component" {
                background #85bbf0
                color #111827
            }
            element "Database" {
                shape Cylinder
                background #2f95c7
                color #ffffff
            }
            element "Deployment Node" {
                background #f3f4f6
                color #111827
            }
            relationship "Relationship" {
                color #4b5563
                routing Orthogonal
            }
        }
    }

    configuration {
        scope softwaresystem
    }
}
