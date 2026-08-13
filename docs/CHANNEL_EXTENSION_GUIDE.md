# Как добавить новый канал

Этот документ описывает границу расширения без реализации MAX, Web или REST API.

## Контракт

Новый адаптер преобразует входной payload в
`application.conversation.ConversationInput`:

- `interaction_id` — стабильный идентификатор входа канала;
- `conversation_id` — область сериализации одного разговора;
- `actor_id` — идентификатор пользователя;
- `channel` — имя канала;
- `kind`, `text`, `action`, `media_reference` — нейтральная семантика;
- `metadata` — только дополнительные данные адаптера.

Затем адаптер вызывает `ConversationApplication.process()` с обычными
`ParsedCommand`, `ConversationState` и каталогом заведения. Парсер и state machine
не должны импортировать payload нового канала.

## Рендеринг и доставка

Адаптер переводит `ConversationResult.view` в формат канала, кодируя
`SemanticAction` по собственному протоколу. Telegram сохраняет текущие `v2:*`
callback values в `presentation/telegram/conversation.py`; новый канал не должен
копировать quantity, comment, catalog или submission rules.

Последовательность подключения:

1. создать adapter входного payload;
2. преобразовать его в `ConversationInput`;
3. вызвать общий conversation use case;
4. отрендерить `ConversationView` и `SemanticAction`;
5. реализовать доставку и подтверждение канала;
6. подключить adapter в composition root с его собственными retry/lease правилами.

Telegram durable update claim, callback acknowledgement, редактирование сообщений
и checkpoint протокол не переносятся в общий use case. Для внешних каталогов,
AI, уведомлений и отправки заявки используются существующие integration seams;
новый порт создаётся только после доказанной внешней зависимости.
