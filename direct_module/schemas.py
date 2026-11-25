from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr
from pydantic.types import StringConstraints

from .yandex_common import LOGIN_PATTERN

Id = Annotated[int, Field(strict=True, gt=0, lt=2**63)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Login = Annotated[str, StringConstraints(strip_whitespace=True, pattern=LOGIN_PATTERN)]
Principal = Literal["local", "interface"]
Vat = Literal["excluded", "included"]
BalanceVat = Literal["unknown", "excluded", "included"]
ConnectionKind = Literal["own", "agency", "manager", "client"]
Operation = Literal[
    "list_organizations",
    "list_accounts",
    "check_connection",
    "discover_accounts",
    "get_balance",
    "get_spending",
    "get_financial_summary",
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class OrganizationInput(Input):
    name: Name
    aliases: list[Name] = Field(default_factory=list, max_length=50)
    website: HttpUrl | None = None
    account_ids: list[Id] = Field(default_factory=list, max_length=1000)
    notes: str | None = Field(default=None, max_length=4000)


class ContactInput(Input):
    name: Name
    phone: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, pattern=r"^[+0-9() .#xX/\-]{3,80}$")
        ]
        | None
    ) = None


class TokenInput(Input):
    oauth_token: SecretStr


class ConnectionInput(TokenInput):
    name: Name
    kind: ConnectionKind


class ConnectionName(Input):
    name: Name


class AccountSettings(Input):
    organization_id: Id | None = None
    shared_account_id: Id | None = None
    balance_vat: BalanceVat = "unknown"


class AccountInput(AccountSettings):
    connection_id: Id
    login: Login | None = None


class PermissionInput(Input):
    principal: Principal
    operation: Operation
    target_id: Id | None = None
    allowed: bool


class PermissionBatch(Input):
    permissions: list[PermissionInput] = Field(min_length=1, max_length=20)


class ListOrganizations(Input):
    operation: Literal["list_organizations"] = "list_organizations"


class ListAccounts(Input):
    operation: Literal["list_accounts"] = "list_accounts"
    organization_id: Id | None = None


class CheckConnection(Input):
    operation: Literal["check_connection"] = "check_connection"
    connection_id: Id


class DiscoverAccounts(Input):
    operation: Literal["discover_accounts"] = "discover_accounts"
    connection_id: Id
    login: Login | None = None


class FinancialScope(Input):
    account_id: Id | None = None
    organization_id: Id | None = None


class GetBalance(FinancialScope):
    operation: Literal["get_balance"] = "get_balance"


class GetSpending(FinancialScope):
    operation: Literal["get_spending"] = "get_spending"
    date_from: date | None = None
    date_to: date | None = None
    vat: Vat = "excluded"


class GetFinancialSummary(FinancialScope):
    operation: Literal["get_financial_summary"] = "get_financial_summary"


Command = Annotated[
    ListOrganizations
    | ListAccounts
    | CheckConnection
    | DiscoverAccounts
    | GetBalance
    | GetSpending
    | GetFinancialSummary,
    Field(discriminator="operation"),
]


class TextInput(Input):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
