import json
import re
from decimal import Decimal, InvalidOperation

import httpx

from .errors import ModuleError

CURRENCIES = frozenset({"RUB", "BYN", "CHF", "EUR", "KZT", "TRY", "UAH", "USD"})
LOGIN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.@+\-]{0,254}$"


def unexpected() -> ModuleError:
    return ModuleError("invalid_yandex_response", "Яндекс вернул ответ в неожиданном формате.", 502)


class YandexTransport:
    def __init__(self, client: httpx.Client):
        self.client = client

    def post(self, url: str, **kwargs) -> httpx.Response:
        try:
            return self.client.post(url, **kwargs)
        except httpx.TimeoutException:
            raise ModuleError("yandex_timeout", "Яндекс не ответил вовремя.", 504) from None
        except httpx.RequestError:
            raise ModuleError(
                "yandex_unreachable", "Не удалось связаться с API Яндекс Директа.", 502
            ) from None


def response_json(response: httpx.Response, *, required: bool = True) -> dict:
    try:
        payload = json.loads(response.content, parse_float=Decimal)
        if isinstance(payload, dict):
            return payload
    except (ValueError, UnicodeError):
        pass
    if required:
        raise unexpected()
    return {}


def object_list(value) -> list[dict]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise unexpected()


def positive_id(value) -> int:
    if type(value) is int and 0 < value < 2**63:
        return value
    raise unexpected()


def login_value(value) -> str:
    if isinstance(value, str) and re.fullmatch(LOGIN_PATTERN, value):
        return value.lower()
    raise unexpected()


def money(value) -> Decimal:
    try:
        if not isinstance(value, bool) and isinstance(value, (str, int, Decimal)):
            amount = Decimal(value)
            if amount.is_finite() and (amount == 0 or -12 <= amount.adjusted() <= 17):
                return amount
    except (InvalidOperation, ValueError):
        pass
    raise unexpected()


def upstream_error(code, http_status: int, *, balance: bool = False) -> ModuleError:
    if type(code) is not int:
        code = None
    if code == 53 or http_status == 401:
        return ModuleError("yandex_auth", "Яндекс отклонил OAuth-токен.", 403, code)
    if code == 58:
        return ModuleError(
            "yandex_app_access",
            "Яндекс ещё не одобрил доступ приложения к API Директа.",
            403,
            code,
        )
    if code == 513:
        return ModuleError(
            "yandex_no_account", "У пользователя токена нет аккаунта Директа.", 403, code
        )
    if code in {54, 510, 3000, 3228, 8800} or http_status == 403:
        message = (
            "Live 4 не дал прочитать общий счёт. Проверьте роль, права на финансы, "
            "валюту аккаунта и наличие общего счёта."
            if balance
            else "Яндекс не предоставил доступ к операции. Проверьте роль, логин и настройки API."
        )
        return ModuleError("yandex_access_denied", message, 403, code)
    if balance and code in {55, 59, 508, 509}:
        return ModuleError(
            "balance_api_unavailable",
            "Яндекс сообщил, что метод баланса Live 4 недоступен.",
            502,
            code,
        )
    if code in {56, 57, 152, 506} or http_status == 429:
        return ModuleError(
            "yandex_limit", "Достигнут лимит API Яндекса. Повторите запрос позднее.", 503, code
        )
    return ModuleError("yandex_error", "Яндекс отклонил запрос к Директу.", 502, code)
