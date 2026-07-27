from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from restaurant_bot.domain.models import CatalogProduct, DepartmentQuantities
from restaurant_bot.integrations import google_sheets as google_sheets_module
from restaurant_bot.integrations.google_sheets import (
    HISTORY_HEADERS,
    GoogleSheetsError,
    GoogleSheetsGateway,
)

VENUE_SPREADSHEET_ID = "venue-sheet"


def test_history_value_uses_source_contract_headers(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что история значение использует исходный контракт заголовки."""
    gateway = GoogleSheetsGateway(settings)
    row = {"№ Заявки": "20260722-001", "Кол-во": 10, "_department": "Кухня"}
    assert gateway._history_value(row, "№ Заявки") == "20260722-001"
    assert gateway._history_value(row, "Кол-во") == 10


def test_history_value_does_not_export_internal_fields(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что история значение выполняет не экспортирует внутренние fields."""
    gateway = GoogleSheetsGateway(settings)
    assert gateway._history_value({"_department": "Кухня"}, "Неизвестная колонка") == ""


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


def test_history_write_uses_first_empty_a_to_s_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что история запись использует первый пустой результат a в s строка."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    gateway._get_values = MagicMock(  # type: ignore[method-assign]
        return_value=[[*HISTORY_HEADERS, "Стадия"], [], [], ["A-OLD"]]
    )

    gateway.append_history(
        [{"№ Заявки": "A-1", "Комментарий": "только охлаждённое", "Стадия": "Новая заявка"}],
        VENUE_SPREADSHEET_ID,
    )

    call = gateway.service.spreadsheets.return_value.values.return_value.update.call_args.kwargs
    assert call["range"] == f"'{settings.google_history_sheet}'!A2:S2"
    assert len(call["body"]["values"][0]) == 19
    assert call["body"]["values"][0][14] == "только охлаждённое"
    gateway.service.spreadsheets.return_value.values.return_value.append.assert_not_called()


def test_history_write_appends_only_when_there_is_no_empty_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что история запись appends только когда there является без пустой результат строка."""
    gateway = GoogleSheetsGateway(settings)
    gateway.service = MagicMock()
    gateway._get_values = MagicMock(  # type: ignore[method-assign]
        return_value=[list(HISTORY_HEADERS), ["A-OLD"]]
    )

    gateway.append_history([{"№ Заявки": "A-NEW"}], VENUE_SPREADSHEET_ID)

    call = gateway.service.spreadsheets.return_value.values.return_value.append.call_args.kwargs
    assert call["range"] == f"'{settings.google_history_sheet}'!A:S"


def test_read_rows_keeps_first_duplicate_header_value(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что чтение строки сохраняет первый дубликат header значение."""
    gateway = GoogleSheetsGateway(settings)
    gateway._get_values = MagicMock(  # type: ignore[method-assign]
        return_value=[["№ Заявки", "Стадия", "Стадия"], ["A-1", "Новая заявка", ""]]
    )

    assert gateway.read_rows("История", VENUE_SPREADSHEET_ID)[0]["Стадия"] == "Новая заявка"


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
