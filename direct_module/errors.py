from dataclasses import dataclass


@dataclass
class ModuleError(Exception):
    code: str
    message: str
    status: int = 400
    yandex_code: int | None = None
    retry_after_seconds: int | None = None

    def public(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.yandex_code is not None:
            result["yandex_code"] = self.yandex_code
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = self.retry_after_seconds
        return result
