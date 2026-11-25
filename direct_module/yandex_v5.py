import csv
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from time import sleep

from .errors import ModuleError
from .yandex_common import (
    CURRENCIES,
    YandexTransport,
    login_value,
    money,
    object_list,
    positive_id,
    response_json,
    unexpected,
    upstream_error,
)

BASE = "https://api.direct.yandex.com/json/v501"
CLIENT_FIELDS = ["Login", "ClientId", "Currency", "Type", "Settings"]


@dataclass(frozen=True)
class ClientInfo:
    login: str
    client_id: int
    currency: str | None
    kind: str
    shared_account: bool | None
    chief_login: str | None


def client_info(item: dict) -> ClientInfo:
    currency = item.get("Currency")
    kind = item.get("Type")
    shared = next(
        (
            setting.get("Value")
            for setting in object_list(item.get("Settings", []))
            if setting.get("Option") == "SHARED_ACCOUNT_ENABLED"
        ),
        None,
    )
    chief = next(
        (
            rep.get("Login")
            for rep in object_list(item.get("Representatives", []))
            if rep.get("Role") == "CHIEF"
        ),
        None,
    )
    return ClientInfo(
        login_value(item.get("Login")),
        positive_id(item.get("ClientId")),
        currency if isinstance(currency, str) and currency in CURRENCIES else None,
        kind if kind in ("CLIENT", "SUBCLIENT", "AGENCY") else "UNKNOWN",
        {"YES": True, "NO": False}.get(shared) if isinstance(shared, str) else None,
        login_value(chief) if chief is not None else None,
    )


def headers(token: str, login: str | None) -> dict:
    result = {"Authorization": f"Bearer {token}", "Accept-Language": "ru"}
    if login is not None:
        result["Client-Login"] = login
    return result


class DirectV5:
    def __init__(self, transport: YandexTransport, pause: Callable[[float], None] = sleep):
        self.transport = transport
        self.pause = pause

    def _get(self, service: str, token: str, login: str | None, params: dict) -> dict:
        response = self.transport.post(
            f"{BASE}/{service}",
            headers=headers(token, login),
            json={"method": "get", "params": params},
        )
        payload = response_json(response, required=response.is_success)
        error = payload.get("error")
        if error is not None or not response.is_success:
            code = error.get("error_code") if isinstance(error, dict) else None
            raise upstream_error(code, response.status_code)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise unexpected()
        return result

    def client(self, token: str, login: str | None = None) -> ClientInfo:
        result = self._get(
            "clients",
            token,
            login,
            {
                "FieldNames": [*CLIENT_FIELDS, "Representatives"],
            },
        )
        clients = object_list(result.get("Clients"))
        if len(clients) != 1:
            raise ModuleError(
                "client_unverified",
                "Clients.get не подтвердил единственный рекламный аккаунт.",
                422,
            )
        info = client_info(clients[0])
        if login is not None and info.login != login:
            raise ModuleError("client_mismatch", "Яндекс вернул данные другого логина.", 403)
        return info

    def agency_clients(self, token: str) -> list[ClientInfo]:
        offset = 0
        clients = []
        while True:
            result = self._get(
                "agencyclients",
                token,
                None,
                {
                    "SelectionCriteria": {},
                    "FieldNames": CLIENT_FIELDS,
                    "Page": {"Limit": 1000, "Offset": offset},
                },
            )
            page = object_list(result.get("Clients"))
            clients.extend(client_info(item) for item in page)
            next_offset = result.get("LimitedBy")
            if next_offset is None:
                return clients
            if type(next_offset) is not int or next_offset <= offset or not page:
                raise unexpected()
            offset = next_offset

    def spending(
        self,
        token: str,
        login: str | None,
        start: date,
        end: date,
        vat: str,
    ) -> dict[date, Decimal | None]:
        identity = f"{login}|{start}|{end}|{vat}".encode()
        report = {
            "params": {
                "SelectionCriteria": {"DateFrom": start.isoformat(), "DateTo": end.isoformat()},
                "FieldNames": ["Date", "Cost"],
                "ReportName": "direct-module-" + sha256(identity).hexdigest()[:24],
                "ReportType": "ACCOUNT_PERFORMANCE_REPORT",
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "YES" if vat == "included" else "NO",
                "IncludeSubscriptionDiscount": "YES",
            }
        }
        request_headers = {
            **headers(token, login),
            "processingMode": "auto",
            "skipReportHeader": "true",
            "skipReportSummary": "true",
        }
        wait_budget = 5
        for attempt in range(3):
            response = self.transport.post(f"{BASE}/reports", headers=request_headers, json=report)
            if response.status_code == 200:
                return self._parse_report(response.content, start, end)
            if response.status_code not in {201, 202}:
                error = response_json(response, required=False).get("error")
                code = error.get("error_code") if isinstance(error, dict) else None
                raise upstream_error(code, response.status_code)
            retry_after = response.headers.get("retryIn", "")
            delay = (
                int(retry_after)
                if retry_after.isascii() and retry_after.isdecimal() and (len(retry_after) < 8)
                else 1
            )
            if attempt == 2 or delay > wait_budget:
                raise ModuleError(
                    "report_pending",
                    "Яндекс формирует отчёт. Повторите команду позже.",
                    503,
                    retry_after_seconds=delay,
                )
            self.pause(delay)
            wait_budget -= delay
        raise AssertionError("Цикл ожидания отчета завершился неожиданно.")

    @staticmethod
    def _parse_report(raw: bytes, start: date, end: date) -> dict[date, Decimal | None]:
        days = {
            start + timedelta(days=index): Decimal(0) for index in range((end - start).days + 1)
        }
        seen: set[date] = set()
        try:
            rows = csv.reader(StringIO(raw.decode("utf-8-sig")), delimiter="\t", strict=True)
            if next(rows, None) != ["Date", "Cost"]:
                raise unexpected()
            for row in rows:
                if not row:
                    continue
                if len(row) != 2:
                    raise unexpected()
                day = date.fromisoformat(row[0])
                if day not in days or day in seen:
                    raise unexpected()
                seen.add(day)
                days[day] = None if row[1] == "--" else money(row[1]) / Decimal(1_000_000)
            return days
        except (UnicodeError, csv.Error, ValueError):
            raise unexpected() from None
