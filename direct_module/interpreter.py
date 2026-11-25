import re
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Protocol

from .errors import ModuleError
from .schemas import (
    Command,
    GetBalance,
    GetFinancialSummary,
    GetSpending,
    ListAccounts,
    ListOrganizations,
)


def normalize(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


class Interpreter(Protocol):
    def interpret(
        self,
        text: str,
        organizations: Mapping[str, frozenset[int]],
        today: date,
    ) -> Command: ...


class DeterministicInterpreter:
    def interpret(
        self,
        text: str,
        organizations: Mapping[str, frozenset[int]],
        today: date,
    ) -> Command:
        query = normalize(text).strip(" .?!")
        if query in {"список организаций", "покажи организации"}:
            return ListOrganizations()
        if query in {"список аккаунтов", "покажи аккаунты"}:
            return ListAccounts()
        if query == "сколько денег осталось у всех клиентов":
            return GetBalance()

        match = re.fullmatch(
            r"(?P<intent>(?:(?:проверь|покажи) )?баланс|"
            r"(?:покажи )?расходы за (?:неделю|вчера)|"
            r"на сколько дней хватит рекламного бюджета|(?:покажи )?финансовую сводку)"
            r"(?: (?:организации|компании) (?P<name>.+))?",
            query,
        )
        if match is None:
            raise ModuleError(
                "unknown_command", "Команда не распознана. Используйте один из примеров.", 422
            )
        organization_id = None
        name = match.group("name")
        if name is not None:
            matches = organizations.get(name.strip(' «»"'), frozenset())
            if len(matches) != 1:
                code = "ambiguous_organization" if matches else "organization_not_found"
                raise ModuleError(
                    code, "Укажите однозначное название или псевдоним организации.", 422
                )
            organization_id = next(iter(matches))

        intent = match.group("intent")
        if "баланс" in intent:
            return GetBalance(organization_id=organization_id)
        if "расходы" in intent:
            days = 1 if intent.endswith("вчера") else 7
            return GetSpending(
                organization_id=organization_id,
                date_from=today - timedelta(days=days),
                date_to=today - timedelta(days=1),
            )
        return GetFinancialSummary(organization_id=organization_id)
