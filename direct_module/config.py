import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database: Path = Path("data/direct-module.sqlite3")
    master_key: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls):
        api_key = os.getenv("DIRECT_MODULE_API_KEY")
        if api_key and (len(api_key) < 32 or not api_key.isascii() or not api_key.isalnum()):
            raise RuntimeError("DIRECT_MODULE_API_KEY должен содержать от 32 букв и цифр ASCII.")
        return cls(
            database=Path(os.getenv("DIRECT_MODULE_DB", "data/direct-module.sqlite3")),
            master_key=os.getenv("DIRECT_MODULE_MASTER_KEY"),
            api_key=api_key,
        )
