from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from restaurant_bot.domain.models import CatalogProduct, DepartmentQuantities
from restaurant_bot.integrations import google_sheets as google_sheets_module
from restaurant_bot.integrations.google_sheets import (
    GoogleSheetsError,
    GoogleSheetsGateway,
    PreparedOrderSubmission,
)

VENUE_SPREADSHEET_ID = "venue-sheet"


def test_gateway_rejects_missing_venue_spreadsheet(settings) -> None:  # type: ignore[no-untyped-def]
    """Не обращается к Google Sheets без таблицы активного заведения."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()

    with pytest.raises(GoogleSheetsError, match="spreadsheet ID is required"):
        gateway.load_catalog("")

    gateway.service.spreadsheets.assert_not_called()


def test_product_add_write_uses_only_exact_user_description(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что товар добавление запись использует только точный пользователь описание."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()

    gateway.append_product_request(
        {"description": "Креветки Polar, 1 тонна", "telegram_user_id": "77"},
        VENUE_SPREADSHEET_ID,
    )

    gateway.service.spreadsheets.return_value.values.return_value.append.assert_called_once_with(
        spreadsheetId=VENUE_SPREADSHEET_ID,
        range=f"'{settings.google_product_add_sheet}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [["Креветки Polar, 1 тонна"]]},
    )


def test_product_add_write_reuses_first_empty_formatted_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Записывает запрос в первую пустую строку листа «Добавить»."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    gateway._get_values = MagicMock(  # type: ignore[method-assign]
        return_value=[["Запрос"], ["Старый запрос"], [], [], ["Другой запрос"]]
    )

    gateway.append_product_request(
        {"description": "Креветки королевские", "telegram_user_id": "77"},
        VENUE_SPREADSHEET_ID,
    )

    values_api = gateway.service.spreadsheets.return_value.values.return_value
    values_api.append.assert_not_called()
    values_api.update.assert_called_once_with(
        spreadsheetId=VENUE_SPREADSHEET_ID,
        range=f"'{settings.google_product_add_sheet}'!A3",
        valueInputOption="USER_ENTERED",
        body={"values": [["Креветки королевские"]]},
    )


def test_product_add_retries_ssl_failure_only_after_verifying_no_write(settings, mocker) -> None:  # type: ignore[no-untyped-def]
    """Повторяет SSL-сбой только когда строка точно не появилась в листе."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    gateway._get_values = MagicMock(side_effect=[[], []])  # type: ignore[method-assign]
    execute = (
        gateway.service.spreadsheets.return_value.values.return_value.append.return_value.execute
    )
    execute.side_effect = [OSError("[SSL] record layer failure"), {}]
    sleep = mocker.patch.object(google_sheets_module, "sleep")

    gateway.append_product_request(
        {"description": "Креветки королевские", "telegram_user_id": "77"},
        VENUE_SPREADSHEET_ID,
    )

    assert execute.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_product_add_accepts_verified_write_after_lost_ssl_response(settings, mocker) -> None:  # type: ignore[no-untyped-def]
    """Не создаёт дубль, если запись появилась до разрыва SSL-ответа."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    sheet_text = "Креветки королевские"
    gateway._get_values = MagicMock(side_effect=[[], [[sheet_text]]])  # type: ignore[method-assign]
    execute = (
        gateway.service.spreadsheets.return_value.values.return_value.append.return_value.execute
    )
    execute.side_effect = OSError("[SSL] record layer failure")
    sleep = mocker.patch.object(google_sheets_module, "sleep")

    gateway.append_product_request(
        {"description": "Креветки королевские", "telegram_user_id": "77"},
        VENUE_SPREADSHEET_ID,
    )

    execute.assert_called_once()
    sleep.assert_not_called()


def test_read_rows_keeps_first_duplicate_header_value(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что чтение строки сохраняет первый дубликат header значение."""
    gateway = GoogleSheetsGateway(settings)
    gateway._get_values = MagicMock(  # type: ignore[method-assign]
        return_value=[["№ Заявки", "Стадия", "Стадия"], ["A-1", "Новая заявка", ""]]
    )

    assert gateway.read_rows("История", VENUE_SPREADSHEET_ID)[0]["Стадия"] == "Новая заявка"


def test_order_statuses_are_read_from_aggregated_history_sheet(settings) -> None:  # type: ignore[no-untyped-def]
    """Читает статусы из «Истории», не используя старый лист товарных строк."""
    gateway = GoogleSheetsGateway(settings)
    gateway.read_rows = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {"Номер заявки": "A-1", "Стадия": "Новая заявка"},
            {"Номер заявки": "A-2", "Стадия": "Подтверждена"},
        ]
    )

    rows = gateway.read_order_statuses([" A-1 "], VENUE_SPREADSHEET_ID)

    assert rows == [{"Номер заявки": "A-1", "Стадия": "Новая заявка"}]
    gateway.read_rows.assert_called_once_with(
        settings.google_order_status_sheet,
        VENUE_SPREADSHEET_ID,
    )
    assert settings.google_order_status_sheet == "История"


def test_order_statuses_accept_legacy_order_number_columns(settings) -> None:  # type: ignore[no-untyped-def]
    """Поддерживает старые названия номера заявки во время перехода."""
    gateway = GoogleSheetsGateway(settings)
    gateway.read_rows = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {"№ Заявки": "A-1", "Стадия": "Новая заявка"},
            {"ID заявки": "A-2", "Стадия": "Подтверждена"},
        ]
    )

    rows = gateway.read_order_statuses(["A-1", "A-2"], VENUE_SPREADSHEET_ID)

    assert [row.get("№ Заявки") or row.get("ID заявки") for row in rows] == ["A-1", "A-2"]


def test_latest_order_statuses_use_first_complete_order_from_history(settings) -> None:  # type: ignore[no-untyped-def]
    """Возвращает все строки самой новой заявки для локальной проверки."""
    gateway = GoogleSheetsGateway(settings)
    gateway.read_rows = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {"Номер заявки": "", "Стадия": ""},
            {"Номер заявки": "A-25", "Поставщик": "Первый"},
            {"Номер заявки": "A-25", "Поставщик": "Второй"},
            {"Номер заявки": "A-24", "Поставщик": "Старый"},
        ]
    )

    rows = gateway.read_latest_order_statuses(VENUE_SPREADSHEET_ID)

    assert [row["Поставщик"] for row in rows] == ["Первый", "Второй"]
    gateway.read_rows.assert_called_once_with(
        settings.google_order_status_sheet,
        VENUE_SPREADSHEET_ID,
    )


def test_latest_order_statuses_return_empty_for_history_without_order_numbers(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает служебные или старые строки без номера заявки за статус."""
    gateway = GoogleSheetsGateway(settings)
    gateway.read_rows = MagicMock(  # type: ignore[method-assign]
        return_value=[{"Номер заявки": "", "Стадия": "replace"}]
    )

    assert gateway.read_latest_order_statuses(VENUE_SPREADSHEET_ID) == []


def test_latest_order_statuses_filter_foreign_venue_before_selecting_order(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает более свежую заявку другого заведения за свою."""
    gateway = GoogleSheetsGateway(settings)
    gateway.read_rows = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "Номер заявки": "FOREIGN-1",
                "Условное наз-ие заведения": "Качели",
            },
            {
                "Номер заявки": "OWN-2",
                "Условное наз-ие заведения": "Тестовое кафе",
            },
            {
                "Номер заявки": "OWN-1",
                "Условное наз-ие заведения": "Тестовое кафе",
            },
        ]
    )

    rows = gateway.read_latest_order_statuses(
        VENUE_SPREADSHEET_ID,
        "Тестовое кафе",
    )

    assert [row["Номер заявки"] for row in rows] == ["OWN-2"]


def test_recent_order_statuses_support_venue_pagination(settings) -> None:  # type: ignore[no-untyped-def]
    """Возвращает выбранные номера страницы со всеми их поставщиками."""
    gateway = GoogleSheetsGateway(settings)
    gateway.read_rows = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "Номер заявки": "A-3",
                "Поставщик": "Первый",
                "Условное наз-ие заведения": "Кафе",
            },
            {
                "Номер заявки": "A-3",
                "Поставщик": "Второй",
                "Условное наз-ие заведения": "Кафе",
            },
            {
                "Номер заявки": "A-2",
                "Поставщик": "Старый",
                "Условное наз-ие заведения": "Кафе",
            },
            {
                "Номер заявки": "A-1",
                "Поставщик": "Очень старый",
                "Условное наз-ие заведения": "Кафе",
            },
        ]
    )

    rows = gateway.read_recent_order_statuses(
        VENUE_SPREADSHEET_ID,
        "Кафе",
        offset=1,
        limit=1,
    )

    assert [row["Номер заявки"] for row in rows] == ["A-2"]


def test_venue_filter_keeps_legacy_history_without_venue_values(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет чтение старой Истории, если в ней нет названия заведения."""
    gateway = GoogleSheetsGateway(settings)
    gateway.read_rows = MagicMock(  # type: ignore[method-assign]
        return_value=[{"Номер заявки": "LEGACY-1", "Стадия": "Отправлена"}]
    )

    rows = gateway.read_latest_order_statuses(
        VENUE_SPREADSHEET_ID,
        "Кафе",
    )

    assert [row["Номер заявки"] for row in rows] == ["LEGACY-1"]


def test_live_catalog_headers_fill_submission_metadata(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что реальная каталог заголовки fill отправка заявки metadata."""
    gateway = GoogleSheetsGateway(settings)
    gateway.read_rows = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "ID товара": "rose",
                "Наименование у Поставщика": "Сироп Роза, 1л",
                "Ед.Изм. для заказа": "шт",
                "Основной поставщик (Условное наз-ие)": "Сиропы",
                "Цена за Ед,Изм, для заказа": "125",
                "Минимальная Кратность в заказе": "2",
                "Полез. V или М Нетто Ед.Изм.в Заказ": "1",
                "Мин сумма для заказа поставщику": "5000",
                "Заведения": "Кафе",
                "Сумма заказанных товаров в заявке по поставщику": "250",
                "Комментарий": "хранить в холоде",
                "__row_number": 7,
            }
        ]
    )

    product = gateway.load_catalog(VENUE_SPREADSHEET_ID)[0]

    assert product.supplier == "Сиропы"
    assert product.price == 125
    assert product.minimum_multiple == 2
    assert product.useful_volume == 1
    assert product.supplier_minimum_amount == 5000
    assert product.restaurant == "Кафе"
    assert product.supplier_current_sum == 250


def test_catalog_update_writes_quantity_and_merged_comment(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что каталог update записывает количество и merged комментарий."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    gateway.load_catalog = MagicMock(  # type: ignore[method-assign]
        return_value=[
            CatalogProduct(
                product_id="rose",
                name="Сироп Роза",
                unit="шт",
                comment="хранить в холоде",
                department_quantities=DepartmentQuantities(kitchen=3),
                row_number=7,
            )
        ]
    )
    gateway._get_values = MagicMock(  # type: ignore[method-assign]
        return_value=[["ID товара", "Зал", "Бар", "Кухня", "Комментарий"]]
    )

    gateway.increment_catalog_quantities(
        [
            {
                "ID товара": "rose",
                "Кол-во": 2,
                "_department": "Кухня",
                "Комментарий": "хранить в холоде; без замены",
            }
        ],
        VENUE_SPREADSHEET_ID,
    )

    call = (
        gateway.service.spreadsheets.return_value.values.return_value.batchUpdate.call_args.kwargs
    )
    assert call["body"]["data"] == [
        {"range": f"'{settings.google_catalog_sheet}'!D7", "values": [[5]]},
        {
            "range": f"'{settings.google_catalog_sheet}'!E7",
            "values": [["хранить в холоде; без замены"]],
        },
    ]


def test_catalog_mutation_plan_contains_all_writes_and_is_deterministic(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет quantity и comment mutations с before/expected-after."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    gateway.load_catalog = MagicMock(  # type: ignore[method-assign]
        return_value=[
            CatalogProduct(
                product_id="rose",
                name="Сироп Роза",
                unit="шт",
                comment="хранить в холоде",
                department_quantities=DepartmentQuantities(kitchen=10),
                row_number=7,
            )
        ]
    )
    gateway._get_values = MagicMock(  # type: ignore[method-assign]
        return_value=[
            [
                "ID товара",
                "Зал",
                "Бар",
                "Кухня",
                "Комментарий",
            ]
        ]
    )
    rows = [
        {
            "ID товара": "rose",
            "Кол-во": 5,
            "_department": "Кухня",
            "Комментарий": "без замены",
        }
    ]

    plan = gateway.prepare_catalog_mutation(
        rows,
        VENUE_SPREADSHEET_ID,
        operation_id="catalog:ORDER-1",
        order_no="ORDER-1",
    )
    same_plan = gateway.prepare_catalog_mutation(
        rows,
        VENUE_SPREADSHEET_ID,
        operation_id="catalog:ORDER-1",
        order_no="ORDER-1",
    )

    assert plan == same_plan
    assert plan["mutations"] == [
        {
            "kind": "quantity",
            "range": f"'{settings.google_catalog_sheet}'!D7",
            "product_id": "rose",
            "department": "Кухня",
            "before": 10,
            "increment": 5.0,
            "expected_after": 15.0,
        },
        {
            "kind": "comment",
            "range": f"'{settings.google_catalog_sheet}'!E7",
            "product_id": "rose",
            "before": "хранить в холоде",
            "expected_after": "хранить в холоде; без замены",
        },
    ]

    gateway.apply_catalog_mutation(plan)
    call = gateway.service.spreadsheets.return_value.values.return_value.batchUpdate.call_args
    assert call.kwargs["body"]["data"] == [
        {"range": f"'{settings.google_catalog_sheet}'!D7", "values": [[15.0]]},
        {
            "range": f"'{settings.google_catalog_sheet}'!E7",
            "values": [
                ["хранить в холоде; без замены"],
            ],
        },
    ]


def test_order_submission_is_prepared_from_venue_spreadsheet(settings) -> None:  # type: ignore[no-untyped-def]
    """Готовит контракт Web App по метаданным таблицы заведения."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    gateway.service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "properties": {"title": "Тестовый ресторан лист Заказа"}
    }

    request = gateway.prepare_order_submission(VENUE_SPREADSHEET_ID, "internal-order-1")

    gateway.service.spreadsheets.return_value.get.assert_called_once_with(
        spreadsheetId=VENUE_SPREADSHEET_ID,
        fields="properties.title",
    )
    assert request.url == settings.google_order_submission_url
    assert request.timeout_seconds == settings.google_order_submission_timeout_seconds
    assert request.payload["sourceSpreadsheetId"] == VENUE_SPREADSHEET_ID
    assert request.payload["sourceSpreadsheetName"] == "Тестовый ресторан лист Заказа"
    assert request.payload["sourceSpreadsheetUrl"].endswith(f"/d/{VENUE_SPREADSHEET_ID}/edit")
    assert request.payload["clientRequestId"] == "internal-order-1"
    assert request.payload["secret"] == "test-submit-secret"
    assert "test-submit-secret" not in repr(request)


def test_order_submission_calls_web_app_once_and_returns_external_number(
    settings,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """Разбирает успешный ответ центрального Web App."""
    response = MagicMock()
    response.json.return_value = {
        "ok": True,
        "result": {
            "orderNumber": "№00V63II4-000025",
            "baseRows": 3,
            "requestRows": 2,
            "notifications": {"telegram": {"attempted": True, "sent": True}},
        },
    }
    post = MagicMock(return_value=response)
    monkeypatch.setattr(google_sheets_module.httpx, "post", post)
    request = PreparedOrderSubmission(
        url=settings.google_order_submission_url,
        payload={"secret": "secret", "sourceSpreadsheetId": VENUE_SPREADSHEET_ID},
        timeout_seconds=60,
    )

    result = GoogleSheetsGateway.send_order_submission(request)

    post.assert_called_once_with(
        settings.google_order_submission_url,
        json=request.payload,
        timeout=60,
        follow_redirects=True,
    )
    response.raise_for_status.assert_called_once()
    assert result.order_number == "№00V63II4-000025"
    assert result.base_rows == 3
    assert result.request_rows == 2
    assert result.notifications["telegram"]["sent"] is True


def test_order_submission_rejects_response_without_external_number(
    settings,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """Не считает отправку завершённой без номера центральной заявки."""
    response = MagicMock()
    response.json.return_value = {"ok": True, "result": {"baseRows": 3, "requestRows": 2}}
    monkeypatch.setattr(google_sheets_module.httpx, "post", MagicMock(return_value=response))
    request = PreparedOrderSubmission(
        url=settings.google_order_submission_url,
        payload={"secret": "secret"},
        timeout_seconds=60,
    )

    with pytest.raises(GoogleSheetsError, match="orderNumber"):
        GoogleSheetsGateway.send_order_submission(request)


def test_recalculation_uses_the_same_body_as_n8n(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что перерасчёт использует тот же body как n8n."""
    settings.google_recalc_token = SecretStr("secret")
    settings.google_recalc_sheet = "Заявка"
    response = MagicMock()
    response.json.return_value = {"success": True}
    response.text = '{"success":true}'
    post = MagicMock(return_value=response)
    monkeypatch.setattr(google_sheets_module.httpx, "post", post)

    GoogleSheetsGateway(settings).trigger_recalculation("A-1", VENUE_SPREADSHEET_ID)

    post.assert_called_once_with(
        settings.google_recalc_url,
        json={
            "token": "secret",
            "sheetName": "Заявка",
            "spreadsheetId": VENUE_SPREADSHEET_ID,
        },
        timeout=45.0,
        follow_redirects=True,
    )
    response.raise_for_status.assert_called_once()


def test_recalculation_without_token_cannot_be_marked_successful(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что перерасчёт без токен не может be отмечается успешная."""
    settings.google_recalc_token = SecretStr("")
    with pytest.raises(GoogleSheetsError, match="GOOGLE_RECALC_TOKEN"):
        GoogleSheetsGateway(settings).trigger_recalculation("A-1", VENUE_SPREADSHEET_ID)


def test_registration_upsert_uses_existing_headers_only(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что регистрация создание или обновление использует существующий заголовки только."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    gateway._get_values = MagicMock(  # type: ignore[method-assign]
        return_value=[
            [
                "Канал",
                "Код",
                "Chat ID",
                "User ID",
                "Username",
                "Активен",
                "Условное наз-ие заведения",
            ]
        ]
    )

    gateway.upsert_venue_registration(
        {
            "channel": "telegram",
            "code": "6461W6",
            "chat_id": "77",
            "user_id": "77",
            "username": "cook",
            "active": "TRUE",
            "venue_name": "Качели",
            "spreadsheet_id": "not-created-because-header-is-absent",
        }
    )

    call = gateway.service.spreadsheets.return_value.values.return_value.append.call_args.kwargs
    assert call["spreadsheetId"] == settings.google_registration_spreadsheet_id
    assert call["range"] == f"'{settings.google_registration_sheet}'!A:G"
    assert call["body"]["values"] == [["telegram", "6461W6", "77", "77", "cook", "TRUE", "Качели"]]


def test_registration_upsert_reuses_first_empty_formatted_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Записывает новую привязку в первую пустую строку вместо конца листа."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    gateway._get_values = MagicMock(  # type: ignore[method-assign]
        return_value=[
            ["Канал", "Код", "Chat ID", "User ID", "Username", "Активен"],
            ["telegram", "12345", "1", "1", "first", "TRUE"],
            ["", "", "", "", "", "FALSE"],
            ["", "", "", "", "", "FALSE"],
        ]
    )

    gateway.upsert_venue_registration(
        {
            "channel": "telegram",
            "code": "12345",
            "chat_id": "77",
            "user_id": "77",
            "username": "cook",
            "active": "TRUE",
        }
    )

    values_api = gateway.service.spreadsheets.return_value.values.return_value
    values_api.append.assert_not_called()
    call = values_api.update.call_args.kwargs
    assert call["range"] == f"'{settings.google_registration_sheet}'!A3:F3"
    assert call["body"]["values"] == [["telegram", "12345", "77", "77", "cook", "TRUE"]]


def test_registration_upsert_rejects_missing_identity_header(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что регистрация создание или обновление отклоняет отсутствующий идентификационный header."""
    gateway = GoogleSheetsGateway(settings)
    gateway._get_values = MagicMock(return_value=[["Канал", "Код"]])  # type: ignore[method-assign]
    with pytest.raises(GoogleSheetsError, match="Chat ID"):
        gateway.upsert_venue_registration(
            {"channel": "telegram", "code": "6461W6", "chat_id": "77", "user_id": "77"}
        )
