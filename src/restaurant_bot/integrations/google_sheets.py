from __future__ import annotations

import re
from collections import defaultdict
from functools import cached_property
from time import sleep
from typing import Any, cast

import httpx
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import CatalogProduct, DepartmentQuantities
from restaurant_bot.services.text import clean_text, normalize_text, to_float

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HISTORY_HEADERS = (
    "Время создания заявки",
    "Время изменения",
    "ID заявки",
    "№ Заявки",
    "Условное название поставщика",
    "Условное наз-ие заведения",
    "Роль",
    "Наименование у поставщика",
    "Ед.Изм. для заказа",
    "Минимальная Кратность в заказе",
    "Полезный V, m Нетто Ед.Изм.для Заказа",
    "Цена за Ед.Изм. для заказа",
    "Кол-во",
    "Мин сумма Заказа по Поставщику",
    "Комментарий",
    "Сумма по товару в заказе",
    "Сумма по заявке к поставщику",
    "Стадия",
    "Стадия от Заведения",
)


class GoogleSheetsError(RuntimeError):
    """Сообщает об ошибке интеграции с Google Sheets."""

    pass


class GoogleSheetsGateway:
    """Читает и обновляет таблицы заведений в Google Sheets."""

    def __init__(self, settings: Settings):
        """Инициализирует компонент."""
        self.settings = settings

    @cached_property
    def service(self):  # type: ignore[no-untyped-def]
        """Создаёт клиент Google Sheets API."""
        credentials = Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            str(self.settings.google_service_account_file),
            scopes=SCOPES,
        )
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        reraise=True,
    )
    def _get_values(self, range_name: str, spreadsheet_id: str) -> list[list[Any]]:
        """Читает диапазон значений из Google Sheets."""
        target_id = self._require_spreadsheet_id(spreadsheet_id)
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=target_id,
                range=range_name,
            )
            .execute()
        )
        return cast(list[list[Any]], response.get("values", []))

    def read_rows(self, sheet_name: str, spreadsheet_id: str) -> list[dict[str, Any]]:
        """Читает строки выбранного листа."""
        values = self._get_values(f"'{sheet_name}'!A:ZZ", spreadsheet_id)
        if not values:
            return []
        headers = [clean_text(header) for header in values[0]]
        rows = []
        for row_number, values_row in enumerate(values[1:], start=2):
            padded = list(values_row) + [""] * max(0, len(headers) - len(values_row))
            # A live history sheet contains duplicate technical headers after
            # the n8n-managed A:S block. Keep the first occurrence, matching
            # n8n's explicit column mapping, instead of letting an empty later
            # duplicate overwrite a real status or timestamp.
            row: dict[str, Any] = {}
            for header, value in zip(headers, padded, strict=False):
                if header and header not in row:
                    row[header] = value
            row["__row_number"] = row_number
            rows.append(row)
        return rows

    def load_catalog(self, spreadsheet_id: str) -> list[CatalogProduct]:
        """Загружает каталог товаров заведения."""
        result = []
        for row in self.read_rows(self.settings.google_catalog_sheet, spreadsheet_id):
            product_id = self._first(row, "ID товара", "ID", "product_id")
            name = self._first(
                row,
                "Наименование у Поставщика",
                "Наименование у поставщика",
                "Наименование товара",
                "Товар",
            )
            if not product_id or not name:
                continue
            result.append(
                CatalogProduct(
                    product_id=product_id,
                    name=name,
                    supplier=self._first(
                        row,
                        "Основной поставщик (Условное наз-ие)",
                        "Условное название поставщика",
                        "Поставщик",
                        "Наименование поставщика",
                    ),
                    unit=self._first(
                        row,
                        # Exact header of the live n8n "ЗАЯВКА" sheet.
                        "Ед.Изм. для заказа",
                        "Ед. Изм. для заказа",
                        "Единица измерения",
                        "Ед. изм.",
                        "Единица",
                    ),
                    price=to_float(
                        self._first(
                            row,
                            "Цена за Ед,Изм, для заказа",
                            "Цена за Ед.Изм. для заказа",
                            "Цена за ед. изм. для заказа",
                            "Цена",
                            "Цена товара",
                        )
                    ),
                    minimum_multiple=to_float(
                        self._first(
                            row,
                            "Минимальная Кратность в заказе",
                            "Минимальная кратность в заказе",
                            "Кратность",
                            "Минимальная кратность",
                        )
                    ),
                    useful_volume=to_float(
                        self._first(
                            row,
                            "Полез. V или М Нетто Ед.Изм.в Заказ",
                            "Полезный V, m Нетто Ед.Изм.для Заказа",
                            "Полезный объем",
                            "Полезный объём",
                        )
                    ),
                    supplier_minimum_amount=to_float(
                        self._first(
                            row,
                            "Мин сумма для заказа поставщику",
                            "Мин сумма Заказа по Поставщику",
                            "Минимальная сумма",
                            "Минимальная сумма поставщика",
                        )
                    ),
                    restaurant=self._first(
                        row, "Заведения", "Условное наз-ие заведения", "Заведение", "Ресторан"
                    ),
                    supplier_schedule=self._first(
                        row, "График заказов", "График доставки", "График поставщика"
                    ),
                    department_quantities=DepartmentQuantities(
                        hall=to_float(row.get("Зал")),
                        bar=to_float(row.get("Бар")),
                        kitchen=to_float(row.get("Кухня")),
                    ),
                    supplier_current_sum=to_float(
                        self._first(
                            row,
                            "Сумма заказанных товаров в заявке по поставщику",
                            "Сумма по заявке к поставщику",
                            "Сумма поставщика",
                            "Текущая сумма",
                        )
                    ),
                    comment=self._first(row, "Комментарий", "Примечание"),
                    row_number=int(row["__row_number"]),
                    raw=row,
                )
            )
        return result

    def append_history(self, rows: list[dict[str, Any]], spreadsheet_id: str) -> None:
        """Записывает строки заявки в историю."""
        if not rows:
            return
        target_id = self._require_spreadsheet_id(spreadsheet_id)
        existing = self._get_values(f"'{self.settings.google_history_sheet}'!A:S", target_id)
        actual_headers = [clean_text(value) for value in (existing[0] if existing else [])]
        if actual_headers[: len(HISTORY_HEADERS)] != list(HISTORY_HEADERS):
            raise GoogleSheetsError("History sheet A:S headers do not match the n8n contract")
        values = [[self._history_value(row, header) for header in HISTORY_HEADERS] for row in rows]
        first_empty_row = self._first_empty_history_row(existing, len(values))
        if first_empty_row <= len(existing):
            last_row = first_empty_row + len(values) - 1
            (
                self.service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=target_id,
                    range=(
                        f"'{self.settings.google_history_sheet}'!A{first_empty_row}:S{last_row}"
                    ),
                    valueInputOption="USER_ENTERED",
                    body={"values": values},
                )
                .execute()
            )
            return
        (
            self.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=target_id,
                range=f"'{self.settings.google_history_sheet}'!A:S",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )

    @staticmethod
    def _first_empty_history_row(existing: list[list[Any]], required_rows: int) -> int:
        """Находит первую свободную строку истории."""
        if required_rows <= 0:
            return 2
        last_start_index = len(existing) - required_rows
        for start_index in range(1, last_start_index + 1):
            block = existing[start_index : start_index + required_rows]
            if all(not any(clean_text(cell) for cell in row) for row in block):
                return start_index + 1
        return len(existing) + 1

    def increment_catalog_quantities(self, rows: list[dict[str, Any]], spreadsheet_id: str) -> None:
        """Обновляет количества товаров в каталоге."""
        target_id = self._require_spreadsheet_id(spreadsheet_id)
        catalog = self.load_catalog(target_id)
        by_id = {product.product_id: product for product in catalog}
        increments: dict[tuple[str, str], float] = defaultdict(float)
        comments: dict[str, str] = {}
        for row in rows:
            product_id = clean_text(row.get("ID товара"))
            department = clean_text(row.get("_department")) or self.settings.default_department
            quantity = to_float(row.get("Кол-во", row.get("Количество"))) or 0
            if product_id and quantity:
                increments[(product_id, department)] += quantity
                product = by_id.get(product_id)
                comments[product_id] = self._merge_comment(
                    comments.get(product_id) or (product.comment if product else ""),
                    clean_text(row.get("Комментарий")),
                )

        if not increments:
            return
        headers_values = self._get_values(f"'{self.settings.google_catalog_sheet}'!1:1", target_id)
        headers = [clean_text(value) for value in (headers_values[0] if headers_values else [])]
        header_index = {header: index + 1 for index, header in enumerate(headers)}
        data: list[dict[str, Any]] = []
        for (product_id, department), increment in increments.items():
            product = by_id.get(product_id)
            column = header_index.get(department)
            if not product or not product.row_number or not column:
                continue
            old = product.department_quantities.for_department(department) or 0
            cell = f"'{self.settings.google_catalog_sheet}'!{self._column_letter(column)}{product.row_number}"
            data.append({"range": cell, "values": [[old + increment]]})
        comment_column = header_index.get("Комментарий")
        if comment_column:
            for product_id, comment in comments.items():
                product = by_id.get(product_id)
                if not product or not product.row_number or not comment:
                    continue
                cell = (
                    f"'{self.settings.google_catalog_sheet}'!"
                    f"{self._column_letter(comment_column)}{product.row_number}"
                )
                data.append({"range": cell, "values": [[comment]]})
        if data:
            (
                self.service.spreadsheets()
                .values()
                .batchUpdate(
                    spreadsheetId=target_id,
                    body={"valueInputOption": "USER_ENTERED", "data": data},
                )
                .execute()
            )

    def append_product_request(self, row: dict[str, Any], spreadsheet_id: str) -> None:
        """Записывает запрос на новый товар."""
        sheet_text = clean_text(row.get("description", ""))
        target_id = self._require_spreadsheet_id(spreadsheet_id)
        existing = self._get_values(f"'{self.settings.google_product_add_sheet}'!A:A", target_id)
        initial_count = self._count_product_request_values(existing, sheet_text)
        empty_row = next(
            (
                row_number
                for row_number, value_row in enumerate(existing[1:], start=2)
                if not value_row or not clean_text(value_row[0])
            ),
            None,
        )
        api = self.service.spreadsheets().values()

        for attempt in range(3):
            try:
                if empty_row is None:
                    operation = api.append(
                        spreadsheetId=target_id,
                        range=f"'{self.settings.google_product_add_sheet}'!A1",
                        valueInputOption="USER_ENTERED",
                        insertDataOption="INSERT_ROWS",
                        body={"values": [[sheet_text]]},
                    )
                else:
                    operation = api.update(
                        spreadsheetId=target_id,
                        range=f"'{self.settings.google_product_add_sheet}'!A{empty_row}",
                        valueInputOption="USER_ENTERED",
                        body={"values": [[sheet_text]]},
                    )
                operation.execute()
                return
            except Exception as exc:
                if not self._retryable_product_request_error(exc):
                    raise
                try:
                    current_count = self._product_request_count(sheet_text, target_id)
                except Exception as verification_error:
                    raise exc from verification_error
                if current_count > initial_count:
                    return
                if attempt == 2:
                    raise
                sleep(0.25 * (2**attempt))

    def _product_request_count(self, sheet_text: str, spreadsheet_id: str) -> int:
        """Считает точные совпадения запроса в листе новых товаров."""
        values = self._get_values(f"'{self.settings.google_product_add_sheet}'!A:A", spreadsheet_id)
        return self._count_product_request_values(values, sheet_text)

    @staticmethod
    def _count_product_request_values(values: list[list[Any]], sheet_text: str) -> int:
        """Считает точные совпадения среди прочитанных значений запросов."""
        return sum(
            1 for value_row in values if value_row and clean_text(value_row[0]) == sheet_text
        )

    @staticmethod
    def _retryable_product_request_error(exc: Exception) -> bool:
        """Определяет временный сетевой сбой записи запроса товара."""
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "ssl",
                "record layer",
                "timeout",
                "timed out",
                "connection",
                "network",
                "socket",
                "eof",
                "502",
                "503",
                "504",
            )
        )

    @staticmethod
    def _history_value(row: dict[str, Any], header: str) -> Any:
        """Сопоставляет поля заявки со столбцами истории."""
        aliases = {
            "Условное название поставщика": "Поставщик",
            "Условное наз-ие заведения": "Заведение",
            "Наименование у поставщика": "Наименование товара",
            "Ед.Изм. для заказа": "Единица измерения",
            "Минимальная Кратность в заказе": "Кратность",
            "Полезный V, m Нетто Ед.Изм.для Заказа": "Полезный объем",
            "Цена за Ед.Изм. для заказа": "Цена",
            "Кол-во": "Количество",
            "Мин сумма Заказа по Поставщику": "Минимальная сумма",
            "Сумма по товару в заказе": "Сумма товара",
            "Сумма по заявке к поставщику": "Сумма поставщика",
            "Стадия от Заведения": "Стадия от заведения",
        }
        domain_key = aliases.get(header, header)
        return row.get(header, row.get(domain_key, ""))

    def read_order_statuses(
        self, order_numbers: list[str], spreadsheet_id: str
    ) -> list[dict[str, Any]]:
        """Читает статусы отправленных заявок."""
        if not order_numbers:
            return []
        wanted = set(order_numbers)
        rows = self.read_rows(self.settings.google_history_sheet, spreadsheet_id)
        return [
            row for row in rows if clean_text(row.get("№ Заявки") or row.get("ID заявки")) in wanted
        ]

    def trigger_recalculation(self, order_no: str, spreadsheet_id: str) -> None:
        """Запускает перерасчёт итогов заявки."""
        del order_no  # The source n8n Apps Script recalculates the whole Заявка sheet.
        target_id = self._require_spreadsheet_id(spreadsheet_id)
        token = self.settings.google_recalc_token.get_secret_value()
        if not token:
            raise GoogleSheetsError("GOOGLE_RECALC_TOKEN is not configured")
        response = httpx.post(
            self.settings.google_recalc_url,
            json={
                "token": token,
                "sheetName": self.settings.google_recalc_sheet,
                "spreadsheetId": target_id,
            },
            timeout=45.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and (
            payload.get("ok") is False or payload.get("success") is False or payload.get("error")
        ):
            raise GoogleSheetsError("Recalculation script returned an error")
        if payload is None and re.search(r"(?:error|exception|failed|ошиб)", response.text, re.I):
            raise GoogleSheetsError("Recalculation script returned an error")

    @staticmethod
    def _merge_comment(base: str, addition: str) -> str:
        """Объединяет комментарии без дублирования текста."""
        current = clean_text(base)
        extra = clean_text(addition)
        if not extra:
            return current
        current_normalized = normalize_text(current).rstrip(".")
        extra_normalized = normalize_text(extra).rstrip(".")
        if current_normalized and extra_normalized:
            if extra_normalized in current_normalized:
                return current
            if current_normalized in extra_normalized:
                return extra
        return "; ".join(value for value in (current, extra) if value)

    @staticmethod
    def _require_spreadsheet_id(spreadsheet_id: str) -> str:
        """Отклоняет операцию без таблицы активного заведения."""
        target_id = clean_text(spreadsheet_id)
        if not target_id:
            raise GoogleSheetsError("Venue spreadsheet ID is required")
        return target_id

    def upsert_venue_registration(self, values: dict[str, Any]) -> None:
        """Создаёт или обновляет строку реестра привязок."""
        spreadsheet_id = self.settings.google_registration_spreadsheet_id
        sheet_name = self.settings.google_registration_sheet
        existing = self._get_values(f"'{sheet_name}'!A:ZZ", spreadsheet_id)
        if not existing:
            raise GoogleSheetsError("Registration sheet has no headers")
        headers = [clean_text(value) for value in existing[0]]
        normalized = {normalize_text(header): index for index, header in enumerate(headers)}
        required_aliases = {
            "channel": ("Канал",),
            "code": ("Код",),
            "chat_id": ("Chat ID", "chat_id"),
            "user_id": ("User ID", "user_id"),
        }
        indexes: dict[str, int] = {}
        for field, aliases in required_aliases.items():
            index = next(
                (
                    normalized[normalize_text(alias)]
                    for alias in aliases
                    if normalize_text(alias) in normalized
                ),
                None,
            )
            if index is None:
                raise GoogleSheetsError(f"Registration sheet missing required header: {aliases[0]}")
            indexes[field] = index

        row_number: int | None = None
        for number, row in enumerate(existing[1:], start=2):
            padded = list(row) + [""] * max(0, len(headers) - len(row))
            if all(
                normalize_text(padded[indexes[field]]) == normalize_text(values[field])
                for field in required_aliases
            ):
                row_number = number
                break

        if row_number is None:
            for number, row in enumerate(existing[1:], start=2):
                padded = list(row) + [""] * max(0, len(headers) - len(row))
                if all(not clean_text(padded[indexes[field]]) for field in required_aliases):
                    row_number = number
                    break

        aliases_by_value = {
            "company_type": ("Тип компании",),
            "channel": ("Канал",),
            "code": ("Код",),
            "legal_name": ("Юр. Название компании", "Юр название компании"),
            "chat_id": ("Chat ID", "chat_id"),
            "user_id": ("User ID", "user_id"),
            "username": ("Username",),
            "active": ("Активен",),
            "bound_at": ("Дата привязки",),
            "comment": ("Комментарий",),
            "venue_name": ("Условное название заведения", "Условное наз-ие заведения"),
            "spreadsheet_id": ("ID таблицы заведения",),
            "spreadsheet_url": ("Ссылка на таблицу",),
            "updated_at": ("Дата обновления",),
        }
        output = [""] * len(headers)
        if row_number is not None:
            old = existing[row_number - 1]
            output[: len(old)] = old
        for field, aliases in aliases_by_value.items():
            for alias in aliases:
                index = normalized.get(normalize_text(alias))
                if index is not None:
                    output[index] = values.get(field, "")
                    break

        last_column = self._column_letter(len(headers))
        api = self.service.spreadsheets().values()
        if row_number is None:
            api.append(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A:{last_column}",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [output]},
            ).execute()
        else:
            api.update(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A{row_number}:{last_column}{row_number}",
                valueInputOption="USER_ENTERED",
                body={"values": [output]},
            ).execute()

    def read_venue_registrations(self) -> list[dict[str, Any]]:
        """Читает реестр привязок к заведениям."""
        return self.read_rows(
            self.settings.google_registration_sheet,
            self.settings.google_registration_spreadsheet_id,
        )

    @staticmethod
    def _first(row: dict[str, Any], *keys: str) -> str:
        """Возвращает первое заполненное значение из столбцов."""
        for key in keys:
            value = clean_text(row.get(key))
            if value:
                return value
        return ""

    @staticmethod
    def _column_letter(index: int) -> str:
        """Преобразует номер столбца в обозначение A1."""
        result = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            result = chr(65 + remainder) + result
        return result
