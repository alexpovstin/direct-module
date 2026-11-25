import json
import sqlite3
from datetime import date, timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from direct_module.config import Settings
from direct_module.web import create_app

ADMIN = "/api/v1/admin"
ADMIN_HEADERS = {"X-Direct-Module": "local-admin"}
TEST_OAUTH = "test-oauth-not-a-real-token"
TEST_KEY = "LocalInterfaceTestKey00000000000000000"
TODAY = date(2026, 11, 25)


class YandexStub:
    def __init__(self):
        self.calls = []
        self.kind = "CLIENT"
        self.client_id = 101
        self.fail_auth = False
        self.balance = 840
        self.balance_currency = "RUB"
        self.balance_error = None
        self.shared_account = "YES"
        self.chief_login = None
        self.report_cost = "100000000"
        self.report_status = 200

    @staticmethod
    def client(login, kind, client_id, shared="YES"):
        return {
            "Login": login,
            "ClientId": client_id,
            "Currency": "RUB",
            "Type": kind,
            "Settings": [{"Option": "SHARED_ACCOUNT_ENABLED", "Value": shared}],
        }

    def __call__(self, request):
        self.calls.append(request)
        data = json.loads(request.content)
        login = request.headers.get("Client-Login")
        path = request.url.path
        if path == "/live/v4/json/":
            assert request.url.host == "api.direct.yandex.ru"
            assert data["method"] == "AccountManagement"
            assert data["param"]["Action"] == "Get"
            assert data["token"] == TEST_OAUTH
            if self.balance_error is not None:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "ActionResult": [
                                {
                                    "Login": "alpha",
                                    "Errors": [
                                        {
                                            "FaultCode": self.balance_error,
                                            "FaultDetail": TEST_OAUTH,
                                        },
                                    ],
                                }
                            ],
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Accounts": [
                            {
                                "Login": "alpha",
                                "AccountID": 55,
                                "Currency": self.balance_currency,
                                "Amount": self.balance,
                                "AmountAvailableForTransfer": 0,
                            }
                        ]
                    }
                },
            )
        assert request.url.host == "api.direct.yandex.com"
        assert request.headers["Authorization"] == f"Bearer {TEST_OAUTH}"
        if path == "/json/v501/clients":
            assert data["method"] == "get"
            if self.fail_auth:
                return httpx.Response(
                    200,
                    json={
                        "error": {"error_code": 53, "error_detail": TEST_OAUTH},
                    },
                )
            if login == "denied":
                return httpx.Response(200, json={"error": {"error_code": 54}})
            if login == "ignored":
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "Clients": [
                                self.client("ignored", "CLIENT", self.client_id),
                            ]
                        }
                    },
                )
            client = self.client(
                login or ("agency-admin" if self.kind == "AGENCY" else "alpha"),
                ("SUBCLIENT" if self.kind == "AGENCY" else "CLIENT") if login else self.kind,
                202 if login else self.client_id,
                self.shared_account,
            )
            if self.chief_login is not None:
                client["Representatives"] = [{"Role": "CHIEF", "Login": self.chief_login}]
            return httpx.Response(200, json={"result": {"Clients": [client]}})
        if path == "/json/v501/agencyclients":
            assert self.kind == "AGENCY"
            assert login is None
            assert data["params"]["SelectionCriteria"] == {}
            offset = data["params"]["Page"]["Offset"]
            result = {"Clients": [self.client("alpha", "SUBCLIENT", 202)]}
            if offset == 0:
                result["LimitedBy"] = 1
            else:
                assert offset == 1
                result["Clients"] = [self.client("beta", "SUBCLIENT", 203)]
            return httpx.Response(200, json={"result": result})
        assert path == "/json/v501/reports"
        assert "method" not in data
        assert request.headers["processingMode"] == "auto"
        assert request.headers["skipReportHeader"] == "true"
        assert request.headers["skipReportSummary"] == "true"
        params = data["params"]
        assert params["ReportType"] == "ACCOUNT_PERFORMANCE_REPORT"
        assert params["FieldNames"] == ["Date", "Cost"]
        if self.report_status != 200:
            return httpx.Response(self.report_status, headers={"retryIn": "20"})
        start = date.fromisoformat(params["SelectionCriteria"]["DateFrom"])
        end = date.fromisoformat(params["SelectionCriteria"]["DateTo"])
        lines = ["Date\tCost", f"{end}\t{self.report_cost}"]
        if start < end:
            lines.append(f"{end - timedelta(days=1)}\t40000000")
        return httpx.Response(200, text="\n".join(lines) + "\n")


@pytest.fixture
def module(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite3"
    monkeypatch.setenv("DIRECT_MODULE_DB", str(db))
    command.upgrade(Config("alembic.ini"), "head")
    stub = YandexStub()
    settings = Settings(database=db, master_key=Fernet.generate_key().decode(), api_key=TEST_KEY)
    app = create_app(settings, transport=httpx.MockTransport(stub))
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        app.state.executor.today = lambda: TODAY
        yield client, stub, db


def admin(client, path, method="GET", body=None, status=200):
    kwargs = {} if body is None else {"json": body}
    response = client.request(method, ADMIN + path, headers=ADMIN_HEADERS, **kwargs)
    assert response.status_code == status, response.text
    assert TEST_OAUTH not in response.text
    return response.json()


def grant(client, operation, target=None, principal="local", allowed=True):
    return admin(
        client,
        "/permissions",
        "PUT",
        {
            "operation": operation,
            "target_id": target,
            "principal": principal,
            "allowed": allowed,
        },
    )


def run(client, operation, status=200, **kwargs):
    return admin(client, "/commands", "POST", {"operation": operation, **kwargs}, status)


def connection(client, kind="own"):
    row = admin(
        client,
        "/connections",
        "POST",
        {
            "name": "Проверка",
            "kind": kind,
            "oauth_token": TEST_OAUTH,
        },
        201,
    )
    return row["id"]


def own_account(client, balance_vat="excluded", organization_id=None):
    cid = connection(client)
    grant(client, "discover_accounts", cid)
    account = admin(
        client,
        "/accounts",
        "POST",
        {
            "connection_id": cid,
            "balance_vat": balance_vat,
            "organization_id": organization_id,
        },
        201,
    )
    return cid, account["id"]


def finance_grants(client, account):
    for operation in ("get_balance", "get_spending", "get_financial_summary"):
        grant(client, operation, account)


def test_local_workflow_and_permissions(module):
    client, stub, db = module
    home = client.get("/")
    assert home.status_code == 200
    assert "Организации" in home.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get(ADMIN + "/catalog").status_code == 403
    assert (
        client.get(
            ADMIN + "/catalog",
            headers={
                **ADMIN_HEADERS,
                "Origin": "http://another.invalid",
            },
        ).status_code
        == 403
    )

    org = admin(
        client,
        "/organizations",
        "POST",
        {
            "name": "Альфа",
            "aliases": ["Альфочка"],
            "website": "https://alpha.example",
        },
        201,
    )
    cid = connection(client)
    denied = run(client, "check_connection", 403, connection_id=cid)
    assert denied["error"]["code"] == "permission_denied"
    assert not stub.calls
    grant(client, "check_connection", cid)
    grant(client, "discover_accounts", cid)
    checked = run(client, "check_connection", connection_id=cid)["data"]["connection"]
    assert checked["status"] == "authorized"
    found = run(client, "discover_accounts", connection_id=cid)["data"]
    assert found["accounts"][0]["login"] == "alpha"

    account = admin(
        client,
        "/accounts",
        "POST",
        {
            "connection_id": cid,
            "login": "ALPHA",
            "balance_vat": "excluded",
        },
        201,
    )
    account_id = account["id"]
    updated = admin(
        client,
        f"/organizations/{org['id']}",
        "PUT",
        {
            "name": "Альфа Реклама",
            "aliases": ["Альфа", "Альфочка"],
            "website": "https://new-alpha.example",
            "account_ids": [account_id],
        },
    )
    assert updated["website"] == "https://new-alpha.example/"
    assert (
        run(client, "get_balance", 403, account_id=account_id)["error"]["code"]
        == "permission_denied"
    )
    finance_grants(client, account_id)
    summary = run(client, "get_financial_summary", account_id=account_id)["data"]["accounts"][0]
    assert summary["organization_name"] == "Альфа Реклама"
    assert summary["balance"]["amount"] == "840"
    assert summary["spending_yesterday"] == "100"
    assert summary["spending_7_days"] == "140"
    assert summary["average_daily_spending"] == "20.000000"
    assert summary["forecast_days"] == "42.00"
    assert summary["connection_status"] == "authorized"
    assert summary["diagnostics"] == {}
    assert admin(client, "/dashboard")["accounts"][0] == summary
    text_result = admin(
        client,
        "/commands/text",
        "POST",
        {
            "text": "Проверь баланс организации «Альфочка»!",
        },
    )
    assert text_result["data"]["accounts"][0]["balance"]["amount"] == "840"
    assert run(client, "list_organizations", 403)["error"]["code"] == "permission_denied"
    grant(client, "list_organizations")
    assert run(client, "list_organizations")["data"]["organizations"][0] == updated
    grant(client, "list_accounts")
    assert (
        run(client, "list_accounts", organization_id=org["id"])["data"]["accounts"][0]["id"]
        == account_id
    )
    log = admin(client, "/log")["entries"]
    assert any(entry["code"] == "permission_denied" for entry in log)
    with sqlite3.connect(db) as sqlite:
        raw = sqlite.execute("SELECT token_ciphertext FROM connections").fetchone()[0]
        assert raw.startswith(b"fernet:") and TEST_OAUTH.encode() not in raw
        assert sqlite.execute("SELECT count(*) FROM financial_history").fetchone()[0] == 3


def test_partial_diagnostics_and_expired_token(module):
    client, stub, _ = module
    cid, account = own_account(client)
    finance_grants(client, account)
    stub.balance_error = 54
    result = run(client, "get_financial_summary", account_id=account)["data"]["accounts"][0]
    assert result["balance"] is None
    assert result["spending_7_days"] == "140"
    assert result["diagnostics"]["balance"]["yandex_code"] == 54
    assert result["forecast_days"] is None
    cached = admin(client, "/dashboard")["accounts"][0]
    assert cached["diagnostics"]["balance"] == result["diagnostics"]["balance"]
    assert cached["spending_7_days"] == "140"
    last = admin(client, "/log")["entries"][0]
    assert (last["status"], last["code"]) == ("partial", "yandex_access_denied")
    grant(client, "get_spending", account, allowed=False)
    result = run(client, "get_financial_summary", account_id=account)["data"]["accounts"][0]
    assert result["diagnostics"]["spending"]["code"] == "permission_denied"
    assert admin(client, "/dashboard")["accounts"][0]["spending"] is None

    grant(client, "check_connection", cid)
    stub.fail_auth = True
    failure = run(client, "check_connection", 403, connection_id=cid)
    assert failure["error"]["code"] == "yandex_auth"
    row = admin(client, "/catalog")["connections"][0]
    assert row["status"] == "error"
    assert row["diagnostic"] == failure["error"]


@pytest.mark.parametrize(
    ("scenario", "expected_reason"),
    [
        ("vat", "vat_unconfirmed"),
        ("currency", "currency_mismatch"),
        ("zero_spending", "nonpositive_spending"),
        ("missing_cost", "insufficient_data"),
        ("pending_report", "insufficient_data"),
        ("zero_balance", None),
    ],
)
def test_forecast_requires_comparable_data(module, scenario, expected_reason):
    client, stub, _ = module
    _, account = own_account(client, "unknown" if scenario == "vat" else "included")
    finance_grants(client, account)
    if scenario == "currency":
        stub.balance_currency = "USD"
    if scenario == "zero_spending":
        stub.report_cost = "0"
        result = run(
            client, "get_spending", account_id=account, date_from="2026-11-24", date_to="2026-11-24"
        )["data"]["accounts"][0]
        assert result["spending"]["total"] == "0"
        stub.report_cost = "-40000000"
    if scenario == "missing_cost":
        stub.report_cost = "--"
    if scenario == "pending_report":
        stub.report_status = 201
    if scenario == "zero_balance":
        stub.balance = 0
    row = run(client, "get_financial_summary", account_id=account)["data"]["accounts"][0]
    assert row["forecast_reason"] == expected_reason
    assert row["forecast_days"] == ("0.00" if scenario == "zero_balance" else None)
    if scenario == "pending_report":
        assert row["diagnostics"]["spending"]["retry_after_seconds"] == 20
    else:
        assert json.loads(stub.calls[-1].content)["params"]["IncludeVAT"] == (
            "NO" if scenario == "vat" else "YES"
        )


def test_agency_manager_and_separate_client_routes(module):
    client, stub, _ = module
    agency = connection(client, "agency")
    stub.kind = "AGENCY"
    grant(client, "discover_accounts", agency)
    result = run(client, "discover_accounts", connection_id=agency)["data"]
    assert result["mode"] == "agency"
    assert [row["login"] for row in result["accounts"]] == ["alpha", "beta"]
    account = admin(
        client,
        "/accounts",
        "POST",
        {
            "connection_id": agency,
            "login": "alpha",
        },
        201,
    )["id"]
    grant(client, "get_balance", account)
    balance = run(client, "get_balance", account_id=account)["data"]["accounts"][0]["balance"]
    assert balance["shared_account_id"] == 55
    assert json.loads(stub.calls[-1].content)["param"]["SelectionCriteria"] == {"Logins": ["alpha"]}

    stub.kind = "CLIENT"
    manager = connection(client, "manager")
    grant(client, "discover_accounts", manager)
    result = run(client, "discover_accounts", connection_id=manager)["data"]
    assert result["diagnostic"]["code"] == "managed_discovery_unavailable"
    denied = run(client, "discover_accounts", 403, connection_id=manager, login="denied")
    assert denied["error"]["code"] == "yandex_access_denied"
    unverified = run(client, "discover_accounts", 403, connection_id=manager, login="ignored")
    assert unverified["error"]["code"] == "client_unverified"
    account = admin(
        client,
        "/accounts",
        "POST",
        {
            "connection_id": manager,
            "login": "beta",
        },
        201,
    )["id"]
    finance_grants(client, account)
    row = run(client, "get_financial_summary", account_id=account)["data"]["accounts"][0]
    assert row["diagnostics"]["balance"]["code"] == "managed_balance_unavailable"
    assert row["spending_7_days"] == "140"
    assert stub.calls[-1].headers["Client-Login"] == "beta"

    stub.kind = "SUBCLIENT"
    separate = connection(client, "client")
    grant(client, "discover_accounts", separate)
    row = admin(client, "/accounts", "POST", {"connection_id": separate}, 201)
    assert row["route"] == "own"


def test_interface_and_text_commands(module):
    client, stub, _ = module
    org = admin(client, "/organizations", "POST", {"name": "Альфа"}, 201)
    _, account = own_account(client, organization_id=org["id"])
    path = "/api/v1/commands"
    payload = {"operation": "get_balance", "account_id": account}
    assert client.post(path, json=payload).status_code == 401
    headers = {"Authorization": f"Bearer {TEST_KEY}"}
    assert client.post(path, json=payload, headers=headers).status_code == 403
    grant(client, "get_balance", account, principal="interface")

    response = client.post(
        path + "/text",
        headers=headers,
        json={
            "text": "Проверь баланс организации Альфа",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["accounts"][0]["account"]["id"] == account
    for phrase in (
        "Сколько денег осталось у всех клиентов?",
        "На сколько дней хватит рекламного бюджета?",
        "Покажи расходы за неделю",
    ):
        response = client.post(path + "/text", headers=headers, json={"text": phrase})
        assert response.status_code == (200 if "денег" in phrase else 403)
    before = len(stub.calls)
    assert (
        client.post(path, headers=headers, json={"operation": "stop_campaign"}).status_code == 422
    )
    result = client.post(path + "/text", headers=headers, json={"text": TEST_OAUTH})
    assert result.status_code == 422 and TEST_OAUTH not in result.text
    assert len(stub.calls) == before


def test_token_validation_and_rotation_do_not_echo_secret(module):
    client, stub, _ = module
    cid, account = own_account(client)
    grant(client, "get_balance", account)
    grant(client, "check_connection", cid)
    run(client, "check_connection", connection_id=cid)
    admin(client, "/connections", "POST", {"oauth_token": [TEST_OAUTH]}, 422)
    rotated = admin(client, f"/connections/{cid}/token", "PUT", {"oauth_token": TEST_OAUTH})
    assert (rotated["status"], rotated["checked_at"]) == ("unchecked", None)
    stub.client_id = 999
    row = run(client, "get_balance", account_id=account)["data"]["accounts"][0]
    assert row["balance"] is None
    assert row["diagnostics"]["balance"]["code"] == "account_identity_changed"


def test_connection_rename_preserves_verified_account_and_permissions(module):
    client, stub, db = module
    cid, account_id = own_account(client)
    grant(client, "check_connection", cid)
    checked = run(client, "check_connection", connection_id=cid)["data"]["connection"]
    with sqlite3.connect(db) as sqlite:
        token_before = sqlite.execute("SELECT token_ciphertext FROM connections").fetchone()[0]

    updated = admin(client, f"/connections/{cid}", "PUT", {"name": "  Test connection  "})
    assert updated == {**checked, "name": "Test connection"}
    catalog = admin(client, "/catalog")
    assert catalog["accounts"][0]["id"] == account_id
    assert any(row["target_id"] == cid for row in catalog["permissions"])
    assert admin(client, "/log")["entries"][0]["operation"] == "rename_connection"
    assert len(stub.calls) == 2
    with sqlite3.connect(db) as sqlite:
        assert (
            sqlite.execute("SELECT token_ciphertext FROM connections").fetchone()[0] == token_before
        )


def test_permission_batch_applies_to_both_sources_and_rolls_back_invalid_scope(module):
    client, _, _ = module
    cid = connection(client)
    local = {"operation": "list_organizations", "principal": "local", "allowed": True}
    interface = {
        "operation": "check_connection",
        "principal": "interface",
        "target_id": cid,
        "allowed": True,
    }
    rejected = admin(
        client,
        "/permissions/batch",
        "PUT",
        {
            "permissions": [local, {**interface, "target_id": None}],
        },
        422,
    )
    assert rejected["error"]["code"] == "invalid_permission_scope"
    assert admin(client, "/catalog")["permissions"] == []
    assert run(client, "list_organizations", 403)["error"]["code"] == "permission_denied"
    admin(client, "/permissions/batch", "PUT", {"permissions": [local, interface]})
    assert run(client, "list_organizations")["data"]["organizations"] == []
    assert (
        run(client, "check_connection", 403, connection_id=cid)["error"]["code"]
        == "permission_denied"
    )
    assert (
        client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {TEST_KEY}"},
            json={
                "operation": "check_connection",
                "connection_id": cid,
            },
        ).status_code
        == 200
    )


def test_client_token_labeled_manager_does_not_try_another_login(module):
    client, stub, _ = module
    stub.kind = "SUBCLIENT"
    cid = connection(client, "manager")
    grant(client, "discover_accounts", cid)
    found = run(client, "discover_accounts", connection_id=cid)["data"]
    assert found["mode"] == "own"
    assert found["diagnostic"]["code"] == "manager_unconfirmed"
    body = run(client, "discover_accounts", 422, connection_id=cid, login="beta")
    assert body["error"]["code"] == "unsupported_client_route"
    assert all("Client-Login" not in request.headers for request in stub.calls)


def test_direct_balance_can_match_verified_token_login_instead_of_chief(module):
    client, stub, _ = module
    stub.kind = "SUBCLIENT"
    stub.chief_login = "agency-chief"
    _, account_id = own_account(client)
    grant(client, "get_balance", account_id)
    row = run(client, "get_balance", account_id=account_id)["data"]["accounts"][0]
    assert row["balance"]["amount"] == "840"
    assert row["diagnostics"] == {}
    assert "SelectionCriteria" not in json.loads(stub.calls[-1].content)["param"]


def test_contact_is_scoped_to_its_local_organization(module):
    client, _, _ = module
    one = admin(client, "/organizations", "POST", {"name": "One"}, 201)["id"]
    two = admin(client, "/organizations", "POST", {"name": "Two"}, 201)["id"]
    row = admin(
        client,
        f"/organizations/{one}/contacts",
        "POST",
        {
            "name": "Сотрудник",
            "phone": "+7 000 123-45-67",
        },
        201,
    )
    assert admin(client, "/catalog")["contacts"] == [row]
    admin(client, f"/organizations/{two}/contacts/{row['id']}", "PUT", {"name": "Other"}, 404)
    admin(client, f"/organizations/{one}/contacts/{row['id']}", "PUT", {"name": "Контакт"})
    grant(client, "list_organizations", principal="interface")
    response = client.post(
        "/api/v1/commands",
        headers={"Authorization": f"Bearer {TEST_KEY}"},
        json={"operation": "list_organizations"},
    )
    assert response.status_code == 200
    assert "Контакт" not in response.text and "contacts" not in response.text
