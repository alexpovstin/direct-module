from sqlalchemy import JSON, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .db import timestamp


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list[str]] = mapped_column(JSON)
    website: Mapped[str | None] = mapped_column(String(2048))
    notes: Mapped[str | None] = mapped_column()


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(80))


class ImportArchive(Base):
    __tablename__ = "import_archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    observed_at: Mapped[str] = mapped_column(String(32), default=timestamp)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary)
    encrypted_workbook: Mapped[bytes] = mapped_column(LargeBinary)


class ImportReference(Base):
    __tablename__ = "import_references"

    archive_id: Mapped[int] = mapped_column(ForeignKey("import_archives.id"), primary_key=True)
    sheet_number: Mapped[int] = mapped_column(primary_key=True)
    row_number: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(20), default="unchecked")
    identity: Mapped[dict | None] = mapped_column(JSON)
    diagnostic: Mapped[dict | None] = mapped_column(JSON)
    checked_at: Mapped[str | None] = mapped_column(String(32))


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("connection_id", "login"),
        UniqueConstraint("connection_id", "yandex_client_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    login: Mapped[str] = mapped_column(String(255))
    route: Mapped[str] = mapped_column(String(12))
    yandex_client_id: Mapped[int] = mapped_column()
    currency: Mapped[str | None] = mapped_column(String(12))
    shared_account_id: Mapped[int | None] = mapped_column()
    balance_vat: Mapped[str] = mapped_column(String(12), default="unknown")


class Permission(Base):
    __tablename__ = "permissions"

    principal: Mapped[str] = mapped_column(String(20), primary_key=True)
    operation: Mapped[str] = mapped_column(String(40), primary_key=True)
    target_id: Mapped[int] = mapped_column(primary_key=True)


class FinancialRecord(Base):
    __tablename__ = "financial_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    metric: Mapped[str] = mapped_column(String(20))
    observed_at: Mapped[str] = mapped_column(String(32), default=timestamp)
    details: Mapped[dict] = mapped_column(JSON)


class AuditEntry(Base):
    __tablename__ = "operation_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    observed_at: Mapped[str] = mapped_column(String(32), default=timestamp)
    principal: Mapped[str] = mapped_column(String(20))
    operation: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[int | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(20))
    code: Mapped[str | None] = mapped_column(String(60))
