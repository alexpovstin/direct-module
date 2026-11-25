import ctypes
import sys
from ctypes import wintypes
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from .errors import ModuleError


class SecretStore(Protocol):
    def encrypt(self, token: str) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> str: ...


def storage_error() -> ModuleError:
    return ModuleError(
        "secret_storage_unavailable",
        "Не удалось открыть хранилище токенов. Проверьте учётную запись Windows или ключ модуля.",
        503,
    )


class EncryptedSecrets:
    """Шифротекст хранится транзакционно вместе с подключением."""

    def __init__(self, key: str | None):
        try:
            self.fernet = Fernet(key) if key else None
        except (ValueError, TypeError):
            raise RuntimeError("DIRECT_MODULE_MASTER_KEY должен быть ключом Fernet.") from None

    def encrypt(self, token: str) -> bytes:
        data = token.encode("ascii")
        if self.fernet is not None:
            return b"fernet:" + self.fernet.encrypt(data)
        return b"dpapi:" + self._dpapi(data, protect=True)

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            if ciphertext.startswith(b"fernet:") and self.fernet is not None:
                return self.fernet.decrypt(ciphertext[7:]).decode("ascii")
            if ciphertext.startswith(b"dpapi:"):
                return self._dpapi(ciphertext[6:], protect=False).decode("ascii")
        except (InvalidToken, UnicodeError):
            pass
        raise storage_error()

    @staticmethod
    def _dpapi(data: bytes, *, protect: bool) -> bytes:
        if sys.platform != "win32":
            raise storage_error()

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

        memory = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(memory, ctypes.POINTER(ctypes.c_byte)))
        output = Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        flag_ui_forbidden = 1
        try:
            if not function(
                ctypes.byref(source),
                None,
                None,
                None,
                None,
                flag_ui_forbidden,
                ctypes.byref(output),
            ):
                raise storage_error()
            return ctypes.string_at(output.data, output.size)
        finally:
            ctypes.memset(memory, 0, len(data))
            if output.data:
                ctypes.memset(output.data, 0, output.size)
                kernel32.LocalFree(output.data)
