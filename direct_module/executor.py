from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from datetime import date

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from .catalog import (
    account_view,
    audit,
    connection_view,
    organization_view,
    permitted,
    require_grant,
    require_row,
    update_account,
)
from .db import timestamp, transaction
from .errors import ModuleError
from .finance import (
    direct_today,
    parse_period,
    record,
    report_vat,
    reporting_period,
    spending_data,
    summary,
)
from .interpreter import Interpreter, normalize
from .models import Account, Connection, Organization, Permission
from .schemas import (
    AccountInput,
    CheckConnection,
    Command,
    DiscoverAccounts,
    GetBalance,
    GetFinancialSummary,
    GetSpending,
    ListAccounts,
    ListOrganizations,
)
from .secrets import SecretStore
from .yandex_live4 import DirectLive4
from .yandex_v5 import ClientInfo, DirectV5

COMMAND_ADAPTER = TypeAdapter(Command)


class Executor:
    def __init__(
        self,
        engine: Engine,
        secrets: SecretStore,
        v5: DirectV5,
        live4: DirectLive4,
        interpreter: Interpreter,
        today: Callable[[], date] = direct_today,
    ):
        self.engine = engine
        self.secrets = secrets
        self.v5 = v5
        self.live4 = live4
        self.interpreter = interpreter
        self.today = today

    def text(self, text: str, principal: str) -> dict:
        aliases: dict[str, set[int]] = defaultdict(set)
        with transaction(self.engine) as session:
            organizations = select(Organization)
            if principal != "local" and not permitted(session, principal, "list_organizations"):
                organizations = (
                    organizations.join(Account)
                    .join(
                        Permission,
                        Permission.target_id == Account.id,
                    )
                    .where(
                        Permission.principal == principal,
                        Permission.operation.in_(
                            {"get_balance", "get_spending", "get_financial_summary"}
                        ),
                    )
                )
            for organization in session.scalars(organizations):
                for label in [organization.name, *organization.aliases]:
                    aliases[normalize(label)].add(organization.id)
        try:
            interpreted = self.interpreter.interpret(
                text,
                {label: frozenset(ids) for label, ids in aliases.items()},
                self.today(),
            )
            command = COMMAND_ADAPTER.validate_python(interpreted)
        except (ValidationError, ModuleError) as error:
            diagnostic = (
                error
                if isinstance(error, ModuleError)
                else ModuleError(
                    "invalid_interpretation",
                    "Интерпретатор вернул неподдерживаемую команду.",
                    422,
                )
            )
            with transaction(self.engine) as session:
                audit(session, principal, "interpret_text", status="error", code=diagnostic.code)
            raise diagnostic from None
        return self.execute(command, principal)

    def execute(self, command: Command, principal: str) -> dict:
        operation = command.operation
        target = getattr(command, "connection_id", None) or getattr(command, "account_id", None)
        try:
            with transaction(self.engine) as session:
                data = self._dispatch(session, command, principal)
                if "accounts" not in data or isinstance(command, (DiscoverAccounts, ListAccounts)):
                    audit(session, principal, operation, target)
            return {"operation": operation, "data": data}
        except ModuleError as error:
            with transaction(self.engine) as session:
                audit(session, principal, operation, target, "error", error.code)
            raise

    def _dispatch(self, session: Session, command: Command, principal: str) -> dict:
        if isinstance(command, ListOrganizations):
            require_grant(session, principal, command.operation)
            rows = session.scalars(select(Organization).order_by(Organization.id))
            return {"organizations": [organization_view(row) for row in rows]}
        if isinstance(command, ListAccounts):
            require_grant(session, principal, command.operation)
            return {
                "accounts": [
                    account_view(row)
                    for row in self._accounts(session, None, command.organization_id)
                ]
            }
        if isinstance(command, (CheckConnection, DiscoverAccounts)):
            require_grant(session, principal, command.operation, command.connection_id)
            connection = require_row(session, Connection, command.connection_id)
            if isinstance(command, CheckConnection):
                return self._check(session, connection)
            return self._discover(connection, command.login)
        return self._financial(session, command, principal)

    def _check(self, session: Session, connection: Connection) -> dict:
        info, diagnostic = None, None
        try:
            info = self.v5.client(self.secrets.decrypt(connection.token_ciphertext))
        except ModuleError as error:
            diagnostic = error
        result = session.execute(
            update(Connection)
            .where(
                Connection.id == connection.id,
                Connection.token_ciphertext == connection.token_ciphertext,
            )
            .values(
                checked_at=timestamp(),
                status="authorized" if info else "error",
                identity=asdict(info) if info else None,
                diagnostic=diagnostic.public() if diagnostic else None,
            )
        )
        if result.rowcount != 1:
            raise ModuleError("token_changed", "Токен заменён. Повторите проверку.", 409)
        if diagnostic is not None:
            # Сохраняем диагностику независимо от результата команды.
            session.commit()
            raise diagnostic
        return {"connection": connection_view(connection)}

    def _discover(self, connection: Connection, login: str | None) -> dict:
        token = self.secrets.decrypt(connection.token_ciphertext)
        owner = self.v5.client(token)
        if owner.kind == "AGENCY":
            if login is None:
                accounts = self.v5.agency_clients(token)
                return {"mode": "agency", "accounts": [asdict(item) for item in accounts]}
            info = self._verify_target(token, owner, login.lower(), "agency")
            return {"mode": "agency", "accounts": [asdict(info)]}
        if owner.kind not in {"CLIENT", "SUBCLIENT"}:
            raise ModuleError(
                "unsupported_client_type", "Тип аккаунта Яндекса не подтверждён.", 422
            )
        if login is None or login.lower() == owner.login:
            result = {"mode": "own", "accounts": [asdict(owner)]}
            if connection.kind == "manager" and owner.kind == "CLIENT":
                result["diagnostic"] = {
                    "code": "managed_discovery_unavailable",
                    "message": "Чтобы проверить другой аккаунт в Яндексе, введите его логин.",
                }
            elif connection.kind == "manager":
                result["diagnostic"] = {
                    "code": "manager_unconfirmed",
                    "message": "Яндекс распознал подключение как аккаунт клиента агентства.",
                }
            elif connection.kind == "agency":
                result["diagnostic"] = {
                    "code": "agency_unconfirmed",
                    "message": "Clients.get определил токен как токен рекламодателя.",
                }
            return result
        if connection.kind != "manager" or owner.kind != "CLIENT":
            raise ModuleError(
                "unsupported_client_route", "Для этого подключения доступен аккаунт владельца.", 422
            )
        target = self._verify_target(token, owner, login.lower(), "managed")
        return {"mode": "managed", "accounts": [asdict(target)]}

    def _verify_target(self, token: str, owner: ClientInfo, login: str, route: str) -> ClientInfo:
        info = self.v5.client(token, login)
        if (
            info.client_id == owner.client_id
            or info.kind not in {"CLIENT", "SUBCLIENT"}
            or (route == "agency" and info.kind != "SUBCLIENT")
        ):
            raise ModuleError(
                "client_unverified",
                "Clients.get не подтвердил доступ к отдельному рекламодателю.",
                403,
            )
        return info

    def register_account(self, session: Session, data: AccountInput) -> dict:
        require_grant(session, "local", "discover_accounts", data.connection_id)
        connection = require_row(session, Connection, data.connection_id)
        result = self._discover(connection, data.login)
        if len(result["accounts"]) != 1 or (data.login is None and result["mode"] == "agency"):
            raise ModuleError("account_login_required", "Укажите логин рекламного аккаунта.", 422)
        info = result["accounts"][0]
        conflicts = select(Account).where(
            Account.connection_id == connection.id,
            (Account.login == info["login"]) | (Account.yandex_client_id == info["client_id"]),
        )
        if session.scalar(conflicts) is not None:
            raise ModuleError("account_exists", "Этот аккаунт уже добавлен к подключению.", 409)
        row = Account(
            connection_id=connection.id,
            login=info["login"],
            route=result["mode"],
            yandex_client_id=info["client_id"],
            currency=info["currency"],
        )
        update_account(session, row, data)
        session.add(row)
        session.flush()
        audit(session, "local", "register_account", row.id)
        return account_view(row)

    @staticmethod
    def _accounts(
        session: Session,
        account_id: int | None,
        organization_id: int | None,
    ) -> list[Account]:
        if account_id is not None and organization_id is not None:
            raise ModuleError("invalid_target", "Укажите либо аккаунт, либо организацию.", 422)
        if account_id is not None:
            return [require_row(session, Account, account_id)]
        query = select(Account).order_by(Account.id)
        if organization_id is not None:
            require_row(session, Organization, organization_id)
            query = query.where(Account.organization_id == organization_id)
        return list(session.scalars(query))

    def _financial(
        self,
        session: Session,
        command: GetBalance | GetSpending | GetFinancialSummary,
        principal: str,
    ) -> dict:
        operation = command.operation
        if command.account_id is not None:
            require_grant(session, principal, operation, command.account_id)
        assignments = set(
            session.scalars(
                select(Permission.target_id).where(
                    Permission.principal == principal,
                    Permission.operation == operation,
                )
            )
        )
        if not assignments:
            raise ModuleError("permission_denied", "Выдайте разрешение на эту операцию.", 403)
        accounts = [
            row
            for row in self._accounts(session, command.account_id, command.organization_id)
            if row.id in assignments
        ]
        today = self.today()
        period = (
            parse_period(command.date_from, command.date_to, today)
            if isinstance(
                command,
                GetSpending,
            )
            else reporting_period(today)
        )
        results = []
        for account in accounts:
            result = self._financial_account(session, command, principal, account, period)
            results.append(result)
        if not accounts:
            audit(session, principal, operation, command.organization_id)
        return {"accounts": results}

    def _financial_account(
        self,
        session: Session,
        command: GetBalance | GetSpending | GetFinancialSummary,
        principal: str,
        account: Account,
        period: tuple[date, date],
    ) -> dict:
        connection = require_row(session, Connection, account.connection_id)
        organization = (
            session.get(Organization, account.organization_id) if account.organization_id else None
        )
        metrics = {}
        diagnostics = {}
        requested = []
        if not isinstance(command, GetSpending):
            requested.append("balance")
        if not isinstance(command, GetBalance):
            requested.append("spending")

        credentials = None
        credentials_error = None
        vat = command.vat if isinstance(command, GetSpending) else report_vat(account)
        for metric in requested:
            try:
                require_grant(session, principal, f"get_{metric}", account.id)
                if credentials_error is not None:
                    raise credentials_error
                if credentials is None:
                    try:
                        credentials = self._credentials(connection, account)
                    except ModuleError as error:
                        credentials_error = error
                        raise
                token, info, header_login = credentials
                if metric == "balance":
                    data = self._balance(token, info, account)
                else:
                    data = self._spending(token, header_login, info, period, vat)
                metrics[metric] = record(session, account.id, metric, data)
            except ModuleError as error:
                diagnostics[metric] = error.public()
                if error.code != "permission_denied":
                    context = (
                        {"vat": account.balance_vat, "shared_account_id": account.shared_account_id}
                        if metric == "balance"
                        else {
                            "date_from": period[0].isoformat(),
                            "date_to": period[1].isoformat(),
                            "vat": vat,
                        }
                    )
                    record(
                        session,
                        account.id,
                        f"{metric}_error",
                        {
                            **context,
                            "diagnostic": diagnostics[metric],
                        },
                    )

        status = "partial" if diagnostics and metrics else "error" if diagnostics else "ok"
        first = next(iter(diagnostics.values()), {})
        audit(session, principal, command.operation, account.id, status, first.get("code"))
        if isinstance(command, GetFinancialSummary):
            result = summary(
                account,
                connection,
                organization,
                metrics.get("balance"),
                metrics.get("spending"),
                *period,
            )
        else:
            result = {
                "account": account_view(account),
                "organization_name": organization.name if organization else None,
                "connection_status": connection.status,
                **{metric: metrics.get(metric) for metric in requested},
            }
        return {**result, "diagnostics": diagnostics}

    def _credentials(
        self,
        connection: Connection,
        account: Account,
    ) -> tuple[str, ClientInfo, str | None]:
        token = self.secrets.decrypt(connection.token_ciphertext)
        header_login = None if account.route == "own" else account.login
        info = self.v5.client(token, header_login)
        if info.client_id != account.yandex_client_id or (
            account.route == "own" and info.login != account.login
        ):
            raise ModuleError(
                "account_identity_changed",
                "Логин или ID аккаунта больше не совпадает с привязкой.",
                403,
            )
        return token, info, header_login

    def _balance(self, token: str, info: ClientInfo, account: Account) -> dict:
        if account.route == "managed":
            raise ModuleError(
                "managed_balance_unavailable",
                "Live 4 не описывает чтение чужого общего счёта управляющим аккаунтом. "
                "Подключите токен самого рекламодателя либо его агентства.",
                422,
            )
        if info.shared_account is False:
            raise ModuleError("no_shared_account", "Общий счёт отключён в Директе.", 422)
        agency = account.route == "agency"
        owner_login = info.chief_login if agency and info.chief_login else info.login
        balance = self.live4.balance(
            token,
            owner_login,
            agency=agency,
            account_id=account.shared_account_id,
            alternate_login=info.login if agency else info.chief_login,
        )
        return {
            "amount": str(balance.amount),
            "currency": balance.currency,
            "vat": account.balance_vat,
            "shared_account_id": balance.account_id,
        }

    def _spending(
        self,
        token: str,
        header_login: str | None,
        info: ClientInfo,
        period: tuple[date, date],
        vat: str,
    ) -> dict:
        if info.currency is None:
            raise ModuleError(
                "report_currency_unknown",
                "Clients.get не подтвердил валюту отчёта.",
                422,
            )
        daily = self.v5.spending(token, header_login, *period, vat)
        return spending_data(daily, *period, info.currency, vat)
