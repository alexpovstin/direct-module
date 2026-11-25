from dataclasses import dataclass
from decimal import Decimal

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

URL = "https://api.direct.yandex.ru/live/v4/json/"


@dataclass(frozen=True)
class Balance:
    account_id: int
    amount: Decimal
    currency: str


class DirectLive4:
    def __init__(self, transport: YandexTransport):
        self.transport = transport

    def balance(
        self,
        token: str,
        owner_login: str,
        *,
        agency: bool,
        account_id: int | None = None,
        alternate_login: str | None = None,
    ) -> Balance:
        params: dict = {"Action": "Get"}
        if account_id is not None:
            params["SelectionCriteria"] = {"AccountIDS": [account_id]}
        elif agency:
            params["SelectionCriteria"] = {"Logins": [owner_login]}
        response = self.transport.post(
            URL,
            json={
                "method": "AccountManagement",
                "param": params,
                "locale": "ru",
                "token": token,
            },
        )
        payload = response_json(response, required=response.is_success)
        if "error_code" in payload or not response.is_success:
            raise upstream_error(payload.get("error_code"), response.status_code, balance=True)
        result = payload.get("data")
        if not isinstance(result, dict):
            raise unexpected()

        # В примере JSON и таблице Яндекса названы разные варианты поля.
        for key in ("ActionsResult", "ActionResult"):
            for outcome in object_list(result.get(key, [])):
                for error in object_list(outcome.get("Errors", [])):
                    raise upstream_error(error.get("FaultCode"), response.status_code, balance=True)

        accounts = object_list(result.get("Accounts", []))
        matching = [
            row
            for row in accounts
            if login_value(row.get("Login")) in {owner_login, alternate_login}
            and (account_id is None or row.get("AccountID") == account_id)
        ]
        if accounts and not matching:
            raise ModuleError(
                "balance_owner_mismatch",
                "Яндекс вернул счёт, который не совпал с логином или указанным ID общего счёта.",
                422,
            )
        if not matching:
            raise ModuleError(
                "balance_unavailable",
                "API Яндекса не вернул доступных общих счетов. Уточните права на чтение баланса.",
                404,
            )
        if len(matching) > 1:
            raise ModuleError(
                "ambiguous_shared_account",
                "Яндекс вернул несколько общих счетов. Укажите ID нужного счёта в настройках.",
                422,
            )
        row = matching[0]
        currency = row.get("Currency")
        if not isinstance(currency, str) or currency not in CURRENCIES:
            raise unexpected()
        return Balance(positive_id(row.get("AccountID")), money(row.get("Amount")), currency)
