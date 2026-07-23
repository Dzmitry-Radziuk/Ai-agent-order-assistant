# Security checklist

- [ ] Отозвать Telegram bot token, находившийся в n8n export.
- [ ] Выпустить новый token в BotFather.
- [ ] Сгенерировать случайный `TELEGRAM_WEBHOOK_SECRET` длиной не менее 32 символов.
- [ ] Не коммитить `.env` и Google service-account JSON.
- [ ] Ограничить service account конкретной Google Spreadsheet.
- [ ] Хранить production secrets в Docker/Kubernetes secret manager.
- [ ] Не логировать raw OpenAI/Telegram payload без маскирования персональных данных.
- [ ] Ограничить `/health` и ingress rate limits на уровне reverse proxy.
- [ ] Настроить backup PostgreSQL и retention логов.
- [ ] Настроить alerts на Celery retries, `submission_failed` и рост очереди.
