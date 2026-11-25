from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import account_view, permitted
from .db import timestamp, utc_now
from .errors import ModuleError
from .models import Account, Connection, FinancialRecord, Organization


def direct_today() -> date:
    return utc_now().astimezone(ZoneInfo("Europe/Moscow")).date()


def reporting_period(today: date) -> tuple[date, date]:
    return today - timedelta(days=7), today - timedelta(days=1)


def parse_period(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    if start is None and end is None:
        return reporting_period(today)
    if start is None or end is None or not 0 <= (end - start).days < 366 or end >= today:
        raise ModuleError(
            "invalid_period", "Укажите завершённый период от одного до 366 дней.", 422
        )
    return start, end


def report_vat(account: Account) -> str:
    return "included" if account.balance_vat == "included" else "excluded"


def record(session: Session, account_id: int, metric: str, data: dict) -> dict:
    updated = timestamp()
    session.add(
        FinancialRecord(
            account_id=account_id,
            metric=metric,
            details=data,
            observed_at=updated,
        )
    )
    return {**data, "updated_at": updated}


def spending_data(
    daily: dict[date, Decimal | None],
    start: date,
    end: date,
    currency: str,
    vat: str,
) -> dict:
    values = list(daily.values())
    total = sum(values, Decimal(0)) if all(value is not None for value in values) else None
    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "currency": currency,
        "vat": vat,
        "total": str(total) if total is not None else None,
        "daily": {
            day.isoformat(): str(amount) if amount is not None else None
            for day, amount in daily.items()
        },
    }


def summary(
    account: Account,
    connection: Connection,
    organization: Organization | None,
    balance: dict | None,
    spending: dict | None,
    start: date,
    end: date,
) -> dict:
    yesterday = spending["daily"].get(end.isoformat()) if spending else None
    total = spending["total"] if spending else None
    average = Decimal(total) / 7 if total is not None else None
    forecast = None
    forecast_reason = None

    if balance is None or average is None:
        forecast_reason = "insufficient_data"
    elif balance["currency"] != spending["currency"]:
        forecast_reason = "currency_mismatch"
    elif balance["vat"] == "unknown" or balance["vat"] != spending["vat"]:
        forecast_reason = "vat_unconfirmed"
    elif average <= 0:
        forecast_reason = "nonpositive_spending"
    else:
        forecast = Decimal(balance["amount"]) / average

    dates = [item["updated_at"] for item in (balance, spending) if item is not None]
    return {
        "account": account_view(account),
        "organization_name": organization.name if organization else None,
        "connection_status": connection.status,
        "connection_diagnostic": connection.diagnostic,
        "currency": (balance or spending or {}).get("currency") or account.currency,
        "balance": balance,
        "spending": spending,
        "spending_yesterday": yesterday,
        "spending_7_days": total,
        "average_daily_spending": str(average.quantize(Decimal("0.000001")))
        if average is not None
        else None,
        "forecast_days": str(forecast.quantize(Decimal("0.01"))) if forecast is not None else None,
        "forecast_reason": forecast_reason,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "updated_at": max(dates, default=None),
    }


def cached_summary(
    session: Session,
    account: Account,
    connection: Connection,
    organization: Organization | None,
    today: date,
) -> dict:
    start, end = reporting_period(today)
    metrics = {}
    diagnostics = {}
    has_summary = permitted(session, "local", "get_financial_summary", account.id)
    for metric, operation in (("balance", "get_balance"), ("spending", "get_spending")):
        if not has_summary or not permitted(session, "local", operation, account.id):
            diagnostics[metric] = {
                "code": "permission_denied",
                "message": "Выдайте разрешения на сводку и нужный финансовый показатель.",
            }
            continue

        records = session.scalars(
            select(FinancialRecord)
            .where(
                FinancialRecord.account_id == account.id,
                FinancialRecord.metric.in_((metric, f"{metric}_error")),
            )
            .order_by(FinancialRecord.id.desc())
        )
        for entry in records:
            data = entry.details
            if metric == "balance":
                if data["vat"] != account.balance_vat or (
                    account.shared_account_id is not None
                    and data["shared_account_id"] != account.shared_account_id
                ):
                    continue
            elif (data["date_from"], data["date_to"], data["vat"]) != (
                start.isoformat(),
                end.isoformat(),
                report_vat(account),
            ):
                continue
            if entry.metric == metric:
                metrics[metric] = {**data, "updated_at": entry.observed_at}
            else:
                diagnostics[metric] = data["diagnostic"]
            break

    result = summary(
        account,
        connection,
        organization,
        metrics.get("balance"),
        metrics.get("spending"),
        start,
        end,
    )
    return {**result, "diagnostics": diagnostics}
