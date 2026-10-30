"""Ошибки домена."""

from __future__ import annotations


class CLEError(Exception):
    """Базовая ошибка проекта."""


class BookNotReady(CLEError):
    """Попытка использовать стакан, который не подтвердил целостность."""


class SequenceGap(CLEError):
    """Разрыв последовательности обновлений стакана."""

    def __init__(self, expected: int, received: int) -> None:
        super().__init__(f"sequence gap: expected U={expected}, received U={received}")
        self.expected = expected
        self.received = received


class CrossedBook(CLEError):
    """Лучший bid >= лучшего ask: стакан нельзя использовать."""


class SnapshotRequired(CLEError):
    """Требуется новый snapshot."""


class CredentialsMissing(CLEError):
    """Приватный метод вызван без ключей API."""


class MalformedPayload(CLEError):
    """Сообщение биржи не соответствует ожидаемой схеме."""


class ExchangeError(CLEError):
    """Биржа вернула ошибку."""

    def __init__(self, code: int | None, message: str, status: int | None = None) -> None:
        super().__init__(f"exchange error code={code} status={status}: {message}")
        self.code = code
        self.message = message
        self.status = status


class RateLimitExceeded(CLEError):
    """Исчерпан локальный лимит запросов."""
