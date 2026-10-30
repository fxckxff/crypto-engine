"""Общий путь для тестов: backend добавляется в sys.path без установки пакета.

Так тесты запускаются и в чистом окружении (`PYTHONPATH=backend pytest`), и после
`pip install -e .`.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
