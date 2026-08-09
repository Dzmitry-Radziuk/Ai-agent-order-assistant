from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import cached_property
from time import sleep
from typing import Any, cast

import httpx
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import CatalogProduct, DepartmentQuantities
from restaurant_bot.services.text import (
    clean_text,
    normalize_department,
    normalize_text,
    to_float,
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class GoogleSheetsError(RuntimeError):
    """Сообщает об ошибке интеграции с Google Sheets."""

    pass


@dataclass(frozen=True, slots=True, repr=False)
class PreparedOrderSubmission:
    """Хранит проверенный запрос к центральному Web App."""

    url: str
    payload: dict[str, Any]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class OrderSubmissionResult:
    """Описывает созданную центральным Web App заявку."""

    order_number: str
    base_rows: int
    request_rows: int
    notifications: dict[str, Any]


class CatalogMutationVerification(StrEnum):
    """Описывает результат чтения ячеек после изменения каталога."""

    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


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

    def prepare_catalog_mutation(
        self,
        rows: list[dict[str, Any]],
        spreadsheet_id: str,
        *,
        operation_id: str,
        order_no: str,
    ) -> dict[str, Any]:
        """Строит детерминированный план записи каталога без внешней записи."""
        target_id = self._require_spreadsheet_id(spreadsheet_id)
        catalog = self.load_catalog(target_id)
        by_id = {product.product_id: product for product in catalog}
        increments: dict[tuple[str, str], float] = defaultdict(float)
        comments: dict[str, str] = {}
        for row in rows:
            product_id = clean_text(row.get("ID товара"))
            department = (
                normalize_department(row.get("_department")) or self.settings.default_department
            )
            quantity = to_float(row.get("Кол-во", row.get("Количество"))) or 0
            if product_id and quantity:
                increments[(product_id, department)] += quantity
                product = by_id.get(product_id)
                comments[product_id] = self._merge_comment(
                    comments.get(product_id) or (product.comment if product else ""),
                    clean_text(row.get("Комментарий")),
                )

        if not increments:
            return {
                "schema_version": 1,
                "operation_id": operation_id,
                "order_no": order_no,
                "spreadsheet_id": target_id,
                "mutations": [],
            }
        headers_values = self._get_values(f"'{self.settings.google_catalog_sheet}'!1:1", target_id)
        headers = [clean_text(value) for value in (headers_values[0] if headers_values else [])]
        header_index = {header: index + 1 for index, header in enumerate(headers)}
        mutations: list[dict[str, Any]] = []
        for (product_id, department), increment in increments.items():
            product = by_id.get(product_id)
            column = header_index.get(department)
            if not product or not product.row_number or not column:
                continue
            old = product.department_quantities.for_department(department) or 0
            cell = f"'{self.settings.google_catalog_sheet}'!{self._column_letter(column)}{product.row_number}"
            mutations.append(
                {
                    "kind": "quantity",
                    "range": cell,
                    "product_id": product_id,
                    "department": department,
                    "before": old,
                    "increment": increment,
                    "expected_after": old + increment,
                }
            )
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
                mutations.append(
                    {
                        "kind": "comment",
                        "range": cell,
                        "product_id": product_id,
                        "before": product.comment,
                        "expected_after": comment,
                    }
                )
        return {
            "schema_version": 1,
            "operation_id": operation_id,
            "order_no": order_no,
            "spreadsheet_id": target_id,
            "mutations": mutations,
        }

    def apply_catalog_mutation(self, plan: dict[str, Any]) -> None:
        """Применяет только сохранённые значения плана без повторного чтения каталога."""
        if not self._valid_catalog_mutation_plan(plan):
            raise GoogleSheetsError("Invalid catalog mutation plan")
        target_id = self._require_spreadsheet_id(clean_text(plan.get("spreadsheet_id")))
        mutations = plan.get("mutations") or []
        if not mutations:
            return
        data = [
            {"range": mutation["range"], "values": [[mutation["expected_after"]]]}
            for mutation in mutations
        ]
        (
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=target_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
            .execute()
        )

    def verify_catalog_mutation(
        self,
        plan: dict[str, Any],
    ) -> CatalogMutationVerification:
        """Читает запланированные ячейки одним запросом и классифицирует результат."""
        if not self._valid_catalog_mutation_plan(plan):
            return CatalogMutationVerification.CONFLICT
        mutations = plan["mutations"]
        if not mutations:
            return CatalogMutationVerification.APPLIED
        try:
            target_id = self._require_spreadsheet_id(plan["spreadsheet_id"])
            response = (
                self.service.spreadsheets()
                .values()
                .batchGet(
                    spreadsheetId=target_id,
                    ranges=[mutation["range"] for mutation in mutations],
                )
                .execute()
            )
        except Exception:
            return CatalogMutationVerification.UNAVAILABLE

        value_ranges = response.get("valueRanges")
        if not isinstance(value_ranges, list):
            return CatalogMutationVerification.UNAVAILABLE
        current_values = [
            self._batch_get_cell(value_ranges, index) for index in range(len(mutations))
        ]
        before = all(
            self._catalog_cell_matches(current, mutation["before"], mutation["kind"])
            for current, mutation in zip(current_values, mutations, strict=True)
        )
        expected_after = all(
            self._catalog_cell_matches(current, mutation["expected_after"], mutation["kind"])
            for current, mutation in zip(current_values, mutations, strict=True)
        )
        if expected_after:
            return CatalogMutationVerification.APPLIED
        if before:
            return CatalogMutationVerification.NOT_APPLIED
        return CatalogMutationVerification.CONFLICT

    @staticmethod
    def _valid_catalog_mutation_plan(plan: Any) -> bool:
        """Проверяет минимальную структуру плана до чтения или записи."""
        if not isinstance(plan, dict):
            return False
        if plan.get("schema_version") != 1:
            return False
        if not all(
            clean_text(plan.get(key)) for key in ("operation_id", "order_no", "spreadsheet_id")
        ):
            return False
        mutations = plan.get("mutations")
        if not isinstance(mutations, list):
            return False
        ranges: set[str] = set()
        for mutation in mutations:
            if not isinstance(mutation, dict):
                return False
            kind = mutation.get("kind")
            required = {"kind", "range", "product_id", "before", "expected_after"}
            if kind == "quantity":
                required.update({"department", "increment"})
            elif kind != "comment":
                return False
            if any(key not in mutation for key in required):
                return False
            range_name = clean_text(mutation.get("range"))
            if not range_name or range_name in ranges or not clean_text(mutation.get("product_id")):
                return False
            ranges.add(range_name)
            if kind == "quantity" and any(
                GoogleSheetsGateway._catalog_number(mutation.get(key)) is None
                for key in ("before", "increment", "expected_after")
            ):
                return False
        return True

    @staticmethod
    def _batch_get_cell(value_ranges: list[Any], index: int) -> Any:
        """Извлекает одну ячейку из ответа values.batchGet без подстановки нуля."""
        if index >= len(value_ranges) or not isinstance(value_ranges[index], dict):
            return None
        values = value_ranges[index].get("values")
        if not isinstance(values, list) or not values or not isinstance(values[0], list):
            return None
        return values[0][0] if values[0] else None

    @staticmethod
    def _catalog_cell_matches(current: Any, expected: Any, kind: str) -> bool:
        """Сравнивает число или текст ячейки с сохранённым значением плана."""
        if kind == "quantity":
            current_number = GoogleSheetsGateway._catalog_number(current)
            expected_number = GoogleSheetsGateway._catalog_number(expected)
            return (
                current_number is not None
                and expected_number is not None
                and current_number == expected_number
            )
        return clean_text(current) == clean_text(expected)

    @staticmethod
    def _catalog_number(value: Any) -> float | None:
        """Нормализует числовую ячейку, сохраняя различие нуля и пустоты."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return to_float(value)

    def increment_catalog_quantities(self, rows: list[dict[str, Any]], spreadsheet_id: str) -> None:
        """Совместимо обновляет каталог через одноразовый план и его применение."""
        plan = self.prepare_catalog_mutation(
            rows,
            spreadsheet_id,
            operation_id="legacy-catalog-mutation",
            order_no="legacy",
        )
        self.apply_catalog_mutation(plan)

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

    def read_order_statuses(
        self,
        order_numbers: list[str],
        spreadsheet_id: str,
        venue_name: str = "",
    ) -> list[dict[str, Any]]:
        """Читает статусы заявок из автоматически формируемого листа."""
        if not order_numbers:
            return []
        wanted = {clean_text(order_number) for order_number in order_numbers}
        rows = self._filter_history_by_venue(
            self.read_rows(self.settings.google_order_status_sheet, spreadsheet_id),
            venue_name,
        )
        return [
            row
            for row in rows
            if clean_text(row.get("Номер заявки") or row.get("№ Заявки") or row.get("ID заявки"))
            in wanted
        ]

    def read_recent_order_statuses(
        self,
        spreadsheet_id: str,
        venue_name: str = "",
        *,
        offset: int = 0,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Читает строки последних заявок текущего заведения из «Истории»."""
        if offset < 0 or limit < 1:
            return []
        rows = self._filter_history_by_venue(
            self.read_rows(self.settings.google_order_status_sheet, spreadsheet_id),
            venue_name,
        )
        order_numbers = list(
            dict.fromkeys(
                clean_text(row.get("Номер заявки") or row.get("№ Заявки") or row.get("ID заявки"))
                for row in rows
                if clean_text(
                    row.get("Номер заявки") or row.get("№ Заявки") or row.get("ID заявки")
                )
            )
        )
        selected = set(order_numbers[offset : offset + limit])
        if not selected:
            return []
        return [
            row
            for row in rows
            if clean_text(row.get("Номер заявки") or row.get("№ Заявки") or row.get("ID заявки"))
            in selected
        ]

    def read_latest_order_statuses(
        self,
        spreadsheet_id: str,
        venue_name: str = "",
    ) -> list[dict[str, Any]]:
        """Читает последнюю заявку текущего заведения из «Истории»."""
        return self.read_recent_order_statuses(
            spreadsheet_id,
            venue_name,
            limit=1,
        )

    @staticmethod
    def _filter_history_by_venue(
        rows: list[dict[str, Any]],
        venue_name: str,
    ) -> list[dict[str, Any]]:
        """Исключает заявки других заведений при наличии данных о заведении."""
        expected = normalize_text(venue_name)
        if not expected:
            return rows

        def row_venue(row: dict[str, Any]) -> str:
            """Возвращает название заведения из поддерживаемого столбца."""
            return clean_text(
                row.get("Условное наз-ие заведения")
                or row.get("Условное название заведения")
                or row.get("Заведение")
                or row.get("venue_name")
            )

        rows_with_venue = [row for row in rows if row_venue(row)]
        if not rows_with_venue:
            return rows
        return [row for row in rows_with_venue if normalize_text(row_venue(row)) == expected]

    def prepare_order_submission(
        self,
        spreadsheet_id: str,
        client_request_id: str,
    ) -> PreparedOrderSubmission:
        """Готовит запрос отправки без выполнения внешнего POST."""
        target_id = self._require_spreadsheet_id(spreadsheet_id)
        url = self.settings.google_order_submission_url.strip()
        secret = self.settings.google_order_submission_secret.get_secret_value()
        if not url:
            raise GoogleSheetsError("GOOGLE_ORDER_SUBMISSION_URL is not configured")
        if not secret:
            raise GoogleSheetsError("GOOGLE_ORDER_SUBMISSION_SECRET is not configured")
        metadata = (
            self.service.spreadsheets()
            .get(
                spreadsheetId=target_id,
                fields="properties.title",
            )
            .execute()
        )
        spreadsheet_name = clean_text((metadata.get("properties") or {}).get("title"))
        if not spreadsheet_name:
            raise GoogleSheetsError("Venue spreadsheet title is missing")
        return PreparedOrderSubmission(
            url=url,
            payload={
                "secret": secret,
                "sourceSpreadsheetId": target_id,
                "sourceSpreadsheetName": spreadsheet_name,
                "sourceSpreadsheetUrl": (
                    f"https://docs.google.com/spreadsheets/d/{target_id}/edit"
                ),
                "requestedAt": datetime.now(UTC).isoformat(),
                "clientRequestId": clean_text(client_request_id),
            },
            timeout_seconds=self.settings.google_order_submission_timeout_seconds,
        )

    @staticmethod
    def send_order_submission(request: PreparedOrderSubmission) -> OrderSubmissionResult:
        """Отправляет один заранее подготовленный запрос в центральный Web App."""
        response = httpx.post(
            request.url,
            json=request.payload,
            timeout=request.timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoogleSheetsError("Order submission script returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GoogleSheetsError("Order submission script returned invalid payload")
        if payload.get("ok") is False:
            message = clean_text(payload.get("message"))
            raise GoogleSheetsError(message or "Order submission script returned ok=false")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise GoogleSheetsError("Order submission script did not return result")
        order_number = clean_text(result.get("orderNumber"))
        if not order_number:
            raise GoogleSheetsError("Order submission script did not return orderNumber")
        notifications = result.get("notifications")
        return OrderSubmissionResult(
            order_number=order_number,
            base_rows=int(to_float(result.get("baseRows")) or 0),
            request_rows=int(to_float(result.get("requestRows")) or 0),
            notifications=notifications if isinstance(notifications, dict) else {},
        )

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
