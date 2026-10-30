from __future__ import annotations

from app.api.routes import router as api_router
from app.api.ws import router as ws_router

__all__ = ["api_router", "ws_router"]
