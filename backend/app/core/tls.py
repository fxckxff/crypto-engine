"""TLS-контекст для HTTP/WebSocket-клиентов.

На macOS Python из python.org не использует системное хранилище сертификатов, из-за чего
любой публичный HTTPS падает с CERTIFICATE_VERIFY_FAILED. `truststore` подключает
системный trust store ОС. Проверка сертификатов при этом остаётся включённой:
это не отключение TLS-верификации.
"""

from __future__ import annotations

import ssl


def build_ssl_context() -> ssl.SSLContext:
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:  # pragma: no cover - зависит от окружения
        return ssl.create_default_context()
