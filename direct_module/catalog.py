import re

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .errors import ModuleError
from .models import Account, AuditEntry, Connection, Organization, Permission
from .schemas import AccountSettings, OrganizationInput, PermissionInput

GLOBAL_OPERATIONS = frozenset({"list_organizations", "list_accounts"})
CONNECTION_OPERATIONS = frozenset({"check_connection", "discover_accounts"})


def require_row(session: Session, model, identifier: int):
    row = session.get(model, identifier)
    if row is None:
        raise ModuleError("not_found", "Запись с указанным ID не найдена.", 404)
    return row


def permitted(session: Session, principal: str, operation: str, target_id: int = 0) -> bool:
    return session.get(Permission, (principal, operation, target_id)) is not None


def require_grant(session: Session, principal: str, operation: str, target_id: int = 0) -> None:
    if not permitted(session, principal, operation, target_id):
        raise ModuleError("permission_denied", "Выдайте разрешение в одноимённом разделе.", 403)


def audit(
    session: Session,
    principal: str,
    operation: str,
    target_id: int | None = None,
    status: str = "ok",
    code: str | None = None,
) -> None:
    session.add(
        AuditEntry(
            principal=principal,
            operation=operation,
            target_id=target_id,
            status=status,
            code=code,
        )
    )


def organization_view(row: Organization) -> dict:
    return {"id": row.id, "name": row.name, "aliases": row.aliases, "website": row.website}


def connection_view(row: Connection) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "status": row.status,
        "identity": row.identity,
        "diagnostic": row.diagnostic,
        "checked_at": row.checked_at,
    }


def account_view(row: Account) -> dict:
    return {
        "id": row.id,
        "connection_id": row.connection_id,
        "organization_id": row.organization_id,
        "login": row.login,
        "route": row.route,
        "yandex_client_id": row.yandex_client_id,
        "currency": row.currency,
        "shared_account_id": row.shared_account_id,
        "balance_vat": row.balance_vat,
    }


def save_organization(session: Session, data: OrganizationInput, identifier: int | None) -> dict:
    if identifier is None:
        row = Organization(name=data.name, aliases=[])
        session.add(row)
        session.flush()
    else:
        row = require_row(session, Organization, identifier)
    row.name = data.name
    row.aliases = list(dict.fromkeys(data.aliases))
    row.website = str(data.website) if data.website else None
    if "notes" in data.model_fields_set:
        row.notes = data.notes
    selected = set(data.account_ids)
    attached = list(session.scalars(select(Account).where(Account.organization_id == row.id)))
    accounts = [require_row(session, Account, account_id) for account_id in sorted(selected)]
    for account in attached:
        if account.id not in selected:
            account.organization_id = None
    for account in accounts:
        account.organization_id = row.id
    audit(session, "local", "save_organization", row.id)
    return organization_view(row)


def update_account(session: Session, account: Account, settings: AccountSettings) -> dict:
    if settings.organization_id is not None:
        require_row(session, Organization, settings.organization_id)
    account.organization_id = settings.organization_id
    account.shared_account_id = settings.shared_account_id
    account.balance_vat = settings.balance_vat
    return account_view(account)


def save_permission(session: Session, command: PermissionInput) -> dict:
    op, target = command.operation, command.target_id
    if (target is None) != (op in GLOBAL_OPERATIONS):
        raise ModuleError(
            "invalid_permission_scope", "Выберите подходящую область разрешения.", 422
        )
    if target is not None:
        require_row(session, Connection if op in CONNECTION_OPERATIONS else Account, target)
    if command.allowed:
        session.execute(
            insert(Permission)
            .values(
                principal=command.principal,
                operation=op,
                target_id=target or 0,
            )
            .on_conflict_do_nothing()
        )
    else:
        session.execute(
            delete(Permission).where(
                Permission.principal == command.principal,
                Permission.operation == op,
                Permission.target_id == (target or 0),
            )
        )
    audit(session, "local", "grant_permission" if command.allowed else "revoke_permission", target)
    return command.model_dump()


def read_token(raw: str) -> str:
    token = raw.strip()
    if not re.fullmatch(r"[A-Za-z0-9._~+/=\-]{8,4096}", token):
        raise ModuleError("invalid_token_format", "Введите OAuth-токен Директа.", 422)
    return token
