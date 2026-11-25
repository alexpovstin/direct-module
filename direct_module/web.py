import hmac
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog import (
    account_view,
    audit,
    connection_view,
    organization_view,
    read_token,
    require_row,
    save_organization,
    save_permission,
    update_account,
)
from .config import Settings
from .db import open_engine, transaction
from .errors import ModuleError
from .executor import Executor
from .finance import cached_summary
from .interpreter import DeterministicInterpreter, Interpreter
from .models import (
    Account,
    AuditEntry,
    Connection,
    Contact,
    ImportReference,
    Organization,
    Permission,
)
from .schemas import (
    AccountInput,
    AccountSettings,
    Command,
    ConnectionInput,
    ConnectionName,
    ContactInput,
    OrganizationInput,
    PermissionBatch,
    PermissionInput,
    TextInput,
    TokenInput,
)
from .secrets import EncryptedSecrets, SecretStore
from .yandex_common import YandexTransport
from .yandex_live4 import DirectLive4
from .yandex_v5 import DirectV5

STATIC = Path(__file__).parent / "static"


def local_operator(request: Request) -> None:
    origin = request.headers.get("origin")
    host_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if (
        request.client is None
        or request.client.host not in {"127.0.0.1", "::1"}
        or request.headers.get("x-direct-module") != "local-admin"
        or (origin is not None and origin != host_origin)
        or request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}
    ):
        raise ModuleError("local_admin_required", "Откройте панель на локальном адресе.", 403)


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    secrets: SecretStore | None = None,
    interpreter: Interpreter | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = open_engine(settings.database)
    secrets = secrets or EncryptedSecrets(settings.master_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            with httpx.Client(
                transport=transport,
                timeout=httpx.Timeout(10, connect=5),
                trust_env=False,
                follow_redirects=False,
            ) as http:
                yandex = YandexTransport(http)
                app.state.executor = Executor(
                    engine,
                    secrets,
                    DirectV5(yandex),
                    DirectLive4(yandex),
                    interpreter or DeterministicInterpreter(),
                )
                yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="Direct Module",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def secure_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(ModuleError)
    def domain_error(_request: Request, error: ModuleError):
        return JSONResponse({"error": error.public()}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    def validation_error(_request: Request, _error: RequestValidationError):
        return domain_error(
            _request, ModuleError("invalid_request", "Проверьте поля и формат запроса.", 422)
        )

    @app.exception_handler(IntegrityError)
    def database_conflict(_request: Request, _error: IntegrityError):
        return domain_error(
            _request,
            ModuleError(
                "record_conflict",
                "Данные уже изменились. Обновите страницу и повторите действие.",
                409,
            ),
        )

    def session_scope() -> Iterator[Session]:
        with transaction(engine) as session:
            yield session

    def get_executor(request: Request) -> Executor:
        return request.app.state.executor

    interface_bearer = HTTPBearer(auto_error=False, scheme_name="DirectModuleKey")

    def interface_access(
        credentials: HTTPAuthorizationCredentials | None = Depends(interface_bearer),
    ) -> None:
        if settings.api_key is None:
            raise ModuleError(
                "interface_disabled", "Ключ центрального интерфейса не настроен.", 503
            )
        supplied = credentials.credentials.encode("utf-8") if credentials else b""
        expected = settings.api_key.encode("ascii")
        if not hmac.compare_digest(supplied, expected):
            raise ModuleError("invalid_api_key", "Требуется ключ центрального интерфейса.", 401)

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(interface_access)])
    admin = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(local_operator)])

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @api.post("/commands")
    def command(data: Command, executor: Executor = Depends(get_executor)):
        return executor.execute(data, "interface")

    @api.post("/commands/text")
    def text_command(data: TextInput, executor: Executor = Depends(get_executor)):
        return executor.text(data.text, "interface")

    @admin.post("/commands")
    def local_command(data: Command, executor: Executor = Depends(get_executor)):
        return executor.execute(data, "local")

    @admin.post("/commands/text")
    def local_text_command(data: TextInput, executor: Executor = Depends(get_executor)):
        return executor.text(data.text, "local")

    @admin.get("/catalog")
    def catalog(session: Session = Depends(session_scope)):
        return {
            "organizations": [
                {**organization_view(row), "notes": row.notes}
                for row in session.scalars(select(Organization).order_by(Organization.id))
            ],
            "contacts": [
                {
                    "id": row.id,
                    "organization_id": row.organization_id,
                    "name": row.name,
                    "phone": row.phone,
                }
                for row in session.scalars(select(Contact).order_by(Contact.name, Contact.id))
            ],
            "import_references": [
                {
                    "organization_id": row.organization_id,
                    "archive_id": row.archive_id,
                    "sheet": row.sheet_number,
                    "row": row.row_number,
                }
                for row in session.scalars(
                    select(ImportReference).order_by(
                        ImportReference.archive_id,
                        ImportReference.sheet_number,
                        ImportReference.row_number,
                    )
                )
            ],
            "accounts": [
                account_view(row) for row in session.scalars(select(Account).order_by(Account.id))
            ],
            "connections": [
                connection_view(row)
                for row in session.scalars(select(Connection).order_by(Connection.id))
            ],
            "permissions": [
                {
                    "principal": row.principal,
                    "operation": row.operation,
                    "target_id": row.target_id or None,
                }
                for row in session.scalars(
                    select(Permission).order_by(
                        Permission.principal,
                        Permission.operation,
                        Permission.target_id,
                    )
                )
            ],
        }

    @admin.post("/organizations", status_code=201)
    def add_organization(data: OrganizationInput, session: Session = Depends(session_scope)):
        return save_organization(session, data, None)

    @admin.put("/organizations/{identifier}")
    def edit_organization(
        identifier: int,
        data: OrganizationInput,
        session: Session = Depends(session_scope),
    ):
        return save_organization(session, data, identifier)

    @admin.post("/organizations/{identifier}/contacts", status_code=201)
    def add_contact(identifier: int, data: ContactInput, session: Session = Depends(session_scope)):
        require_row(session, Organization, identifier)
        row = Contact(organization_id=identifier, **data.model_dump())
        session.add(row)
        session.flush()
        audit(session, "local", "add_contact", identifier)
        return {"id": row.id, "organization_id": identifier, **data.model_dump()}

    @admin.put("/organizations/{identifier}/contacts/{contact_id}")
    def edit_contact(
        identifier: int,
        contact_id: int,
        data: ContactInput,
        session: Session = Depends(session_scope),
    ):
        row = require_row(session, Contact, contact_id)
        if row.organization_id != identifier:
            raise ModuleError("not_found", "Контакт организации не найден.", 404)
        row.name, row.phone = data.name, data.phone
        audit(session, "local", "edit_contact", identifier)
        return {"id": row.id, "organization_id": identifier, **data.model_dump()}

    @admin.post("/connections", status_code=201)
    def add_connection(data: ConnectionInput, session: Session = Depends(session_scope)):
        token = read_token(data.oauth_token.get_secret_value())
        row = Connection(name=data.name, kind=data.kind, token_ciphertext=secrets.encrypt(token))
        session.add(row)
        session.flush()
        audit(session, "local", "add_connection", row.id)
        return connection_view(row)

    @admin.put("/connections/{identifier}")
    def rename_connection(
        identifier: int,
        data: ConnectionName,
        session: Session = Depends(session_scope),
    ):
        row = require_row(session, Connection, identifier)
        row.name = data.name
        audit(session, "local", "rename_connection", row.id)
        return connection_view(row)

    @admin.put("/connections/{identifier}/token")
    def replace_token(identifier: int, data: TokenInput, session: Session = Depends(session_scope)):
        row = require_row(session, Connection, identifier)
        row.token_ciphertext = secrets.encrypt(read_token(data.oauth_token.get_secret_value()))
        row.status = "unchecked"
        row.identity = row.diagnostic = row.checked_at = None
        audit(session, "local", "replace_token", row.id)
        return connection_view(row)

    @admin.post("/accounts", status_code=201)
    def add_account(data: AccountInput, executor: Executor = Depends(get_executor)):
        try:
            with transaction(engine) as session:
                return executor.register_account(session, data)
        except ModuleError as error:
            with transaction(engine) as session:
                audit(session, "local", "register_account", data.connection_id, "error", error.code)
            raise

    @admin.put("/accounts/{identifier}")
    def edit_account(
        identifier: int,
        data: AccountSettings,
        session: Session = Depends(session_scope),
    ):
        result = update_account(session, require_row(session, Account, identifier), data)
        audit(session, "local", "save_account", identifier)
        return result

    @admin.put("/permissions")
    def permission(data: PermissionInput, session: Session = Depends(session_scope)):
        return save_permission(session, data)

    @admin.put("/permissions/batch")
    def permissions_batch(data: PermissionBatch, session: Session = Depends(session_scope)):
        return {"permissions": [save_permission(session, item) for item in data.permissions]}

    @admin.get("/dashboard")
    def dashboard(
        executor: Executor = Depends(get_executor), session: Session = Depends(session_scope)
    ):
        query = (
            select(Account, Connection, Organization)
            .join(Connection)
            .outerjoin(
                Organization,
                Account.organization_id == Organization.id,
            )
            .order_by(Account.id)
        )
        today = executor.today()
        return {
            "accounts": [
                cached_summary(session, account, connection, organization, today)
                for account, connection, organization in session.execute(query)
            ],
        }

    @admin.get("/log")
    def operation_log(session: Session = Depends(session_scope)):
        return {
            "entries": [
                {
                    "id": item.id,
                    "observed_at": item.observed_at,
                    "principal": item.principal,
                    "operation": item.operation,
                    "target_id": item.target_id,
                    "status": item.status,
                    "code": item.code,
                }
                for item in session.scalars(
                    select(AuditEntry).order_by(AuditEntry.id.desc()).limit(200)
                )
            ]
        }

    app.include_router(api)
    app.include_router(admin)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
