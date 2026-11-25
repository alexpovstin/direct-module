"""Archive the original workbook and index clearly labeled organization rows."""

import argparse
import re
from collections import defaultdict
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from pydantic import HttpUrl, ValidationError
from sqlalchemy import select

from .catalog import audit
from .config import Settings
from .db import open_engine, transaction
from .interpreter import normalize
from .models import Contact, ImportArchive, ImportReference, Organization
from .schemas import ContactInput
from .secrets import EncryptedSecrets

NAMES = {"клиент", "название клиента", "компания", "организация", "название организации"}
CONTACTS = {"фио", "имя контакта", "контактное лицо", "имя контактного лица"}
PHONES = {"телефон", "номер телефона", "телефон клиента", "контактный телефон"}
SITES = {"сайт", "сайт клиента", "сайт компании", "URL сайта".lower()}
NAME_PATTERN = re.compile(r"[\w .«»\"'&()+,–—\-]{2,150}", re.UNICODE)


def cell_text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def valid_name(value) -> str | None:
    name = cell_text(value)
    if (
        NAME_PATTERN.fullmatch(name)
        and any(c.isalpha() for c in name)
        and not re.search(r"\b(?:пароль|логин|доступы?|token|password)\b", name, re.I)
        and not re.search(r"[A-Za-z0-9_\-]{24,}", name)
    ):
        return name
    return None


def safe_site(value) -> str | None:
    site = cell_text(value)
    if not site or any(char in site for char in "@?#"):
        return None
    try:
        url = HttpUrl(site if "://" in site else f"https://{site}")
        if url.path == "/" and "." in url.host:
            return str(url)
    except ValidationError:
        pass
    return None


def read_index(content: bytes):
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(content), read_only=True, data_only=True, keep_links=False)
    result = []
    try:
        for sheet_number, sheet in enumerate(workbook.worksheets, 1):
            # Индексируем только таблицы с явной колонкой клиента.
            position = None
            for row_number, values in enumerate(sheet.values, 1):
                if position is None:
                    if row_number > 8:
                        break
                    fields = {
                        normalize(cell_text(value)): index
                        for index, value in enumerate(values)
                        if cell_text(value)
                    }
                    if len(fields) >= 3 and (names := NAMES.intersection(fields)):
                        primary = next(iter(sorted(names)))
                        position = (
                            fields[primary],
                            *(
                                next((fields[key] for key in sorted(keys) if key in fields), None)
                                for keys in (SITES, CONTACTS, PHONES)
                            ),
                        )
                    continue

                def value_at(index):
                    return values[index] if index is not None and index < len(values) else None

                name = valid_name(value_at(position[0]))
                if not name or normalize(name) in NAMES:
                    continue
                contact, phone = None, cell_text(value_at(position[3]))
                person = valid_name(value_at(position[2]))
                if person and (not phone or 10 <= sum(c.isdigit() for c in phone) <= 15):
                    try:
                        contact = ContactInput(name=person, phone=phone or None)
                    except ValidationError:
                        pass
                result.append(
                    (sheet_number, row_number, name, safe_site(value_at(position[1])), contact)
                )
        return len(workbook.worksheets), result
    finally:
        workbook.close()


def read_source(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", value):
        raise ValueError("Укажите идентификатор Google Sheets.")
    url = f"https://docs.google.com/spreadsheets/d/{value}/export?format=xlsx"
    with (
        httpx.Client(follow_redirects=True, timeout=30) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        if not (
            response.url.host == "docs.google.com"
            or response.url.host.endswith(".googleusercontent.com")
        ):
            raise ValueError("Экспорт Google недоступен.")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > 20 * 1024 * 1024:
                raise ValueError("Книга больше 20 МБ.")
    if content[:4] != b"PK\x03\x04":
        raise ValueError("Google не предоставил файл XLSX.")
    return bytes(content)


def ingest(engine, secrets, content: bytes):
    sheets, rows = read_index(content)
    digest = sha256(content).hexdigest()
    counts = {
        "sheets": sheets,
        "indexed_rows": 0,
        "organizations_added": 0,
        "contacts_added": 0,
        "ambiguous_rows": 0,
    }
    with transaction(engine) as session:
        existing = session.scalar(select(ImportArchive.id).where(ImportArchive.digest == digest))
        if existing is not None:
            return {**counts, "archive": existing, "already_imported": True}

        key = Fernet.generate_key()
        archive = ImportArchive(
            digest=digest,
            encrypted_key=secrets.encrypt(key.decode("ascii")),
            encrypted_workbook=Fernet(key).encrypt(content),
        )
        session.add(archive)
        session.flush()
        organizations = defaultdict(dict)
        for row in session.scalars(select(Organization)):
            for name in [row.name, *row.aliases]:
                organizations[normalize(name)][row.id] = row
        contacts = {
            (row.organization_id, normalize(row.name), row.phone)
            for row in session.scalars(select(Contact))
        }
        for sheet, row, name, site, contact in rows:
            matches = organizations[normalize(name)]
            if len(matches) > 1:
                counts["ambiguous_rows"] += 1
                continue
            if matches:
                org = next(iter(matches.values()))
                if site and not org.website:
                    org.website = site
            else:
                org = Organization(name=name, aliases=[], website=site)
                session.add(org)
                session.flush()
                matches[org.id] = org
                counts["organizations_added"] += 1
            session.add(
                ImportReference(
                    archive_id=archive.id,
                    sheet_number=sheet,
                    row_number=row,
                    organization_id=org.id,
                )
            )
            counts["indexed_rows"] += 1
            if contact:
                marker = (org.id, normalize(contact.name), contact.phone)
                if marker not in contacts:
                    contacts.add(marker)
                    session.add(Contact(organization_id=org.id, **contact.model_dump()))
                    counts["contacts_added"] += 1
        audit(session, "local", "import_sheet", archive.id)
        return {**counts, "archive": archive.id}


def main() -> None:
    parser = argparse.ArgumentParser(description="Защищённый импорт таблицы организаций")
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("import").add_argument("sheet_id")
    restore = sub.add_parser("restore")
    restore.add_argument("archive_id", type=int)
    restore.add_argument("output", type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    engine = open_engine(settings.database)
    secrets = EncryptedSecrets(settings.master_key)
    try:
        if args.operation == "import":
            print(ingest(engine, secrets, read_source(args.sheet_id)))
        else:
            with transaction(engine) as session:
                row = session.get(ImportArchive, args.archive_id)
                if row is None:
                    raise ValueError("Неизвестный архив.")
                content = Fernet(secrets.decrypt(row.encrypted_key)).decrypt(row.encrypted_workbook)
            if args.output.suffix.casefold() != ".xlsx":
                raise ValueError("Укажите новый путь с расширением .xlsx.")
            with args.output.open("xb") as output:
                output.write(content)
            print("Копия сохранена.")
    except Exception as error:
        # Исключения библиотек могут содержать данные ячеек.
        raise SystemExit(f"Операция не выполнена ({type(error).__name__}).") from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
