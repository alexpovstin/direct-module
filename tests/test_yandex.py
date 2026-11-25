import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from direct_module.errors import ModuleError
from direct_module.yandex_common import YandexTransport
from direct_module.yandex_live4 import DirectLive4
from direct_module.yandex_v5 import DirectV5


def test_queued_reports_repeat_the_same_specification():
    requests = []
    delays = []
    responses = iter(
        [
            httpx.Response(201, headers={"retryIn": "2"}),
            httpx.Response(202, headers={"retryIn": "3"}),
            httpx.Response(200, text="Date\tCost\n2026-11-24\t10001000\n"),
        ]
    )

    def handler(request):
        requests.append(request)
        return next(responses)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        yandex = DirectV5(YandexTransport(http), pause=delays.append)
        day = date(2026, 11, 24)
        assert yandex.spending("test-token", "client", day, day, "excluded") == {
            day: Decimal("10.001"),
        }

    assert delays == [2, 3]
    assert requests[0].url == "https://api.direct.yandex.com/json/v501/reports"
    assert all(request.content == requests[0].content for request in requests)
    assert all(request.headers["Client-Login"] == "client" for request in requests)


def test_direct_balance_keeps_real_zero_and_requires_shared_account_selection():
    calls = []

    def handler(request):
        params = json.loads(request.content)["param"]
        calls.append(params)
        rows = [
            {"Login": "alpha", "AccountID": 1, "Currency": "RUB", "Amount": 0},
            {"Login": "alpha", "AccountID": 2, "Currency": "RUB", "Amount": 125.01},
        ]
        if params.get("SelectionCriteria") == {"AccountIDS": [1]}:
            rows = rows[:1]
        return httpx.Response(200, json={"data": {"Accounts": rows}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        yandex = DirectLive4(YandexTransport(http))
        with pytest.raises(ModuleError, match="несколько общих счетов") as error:
            yandex.balance("test-token", "alpha", agency=False)
        assert error.value.code == "ambiguous_shared_account"
        assert yandex.balance("test-token", "alpha", agency=False, account_id=1).amount == 0
    assert calls == [{"Action": "Get"}, {"Action": "Get", "SelectionCriteria": {"AccountIDS": [1]}}]
