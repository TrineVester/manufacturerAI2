from fastapi import APIRouter

from src.web.routes.sessions import router as _sessions
from src.web.routes.catalog import router as _catalog
from src.web.routes.design import router as _design
from src.web.routes.circuit import router as _circuit
from src.web.routes.manufacture import router as _manufacture

api_router = APIRouter(prefix="/api")
api_router.include_router(_sessions)
api_router.include_router(_catalog)
api_router.include_router(_design)
api_router.include_router(_circuit)
api_router.include_router(_manufacture)

__all__ = ["api_router"]
