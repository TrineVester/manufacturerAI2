from fastapi import APIRouter

from src.web.routes.manufacture.cancel import router as _cancel
from src.web.routes.manufacture.placement import router as _placement
from src.web.routes.manufacture.routing import router as _routing
from src.web.routes.manufacture.bitmap import router as _bitmap
from src.web.routes.manufacture.scad import router as _scad
from src.web.routes.manufacture.compile import router as _compile
from src.web.routes.manufacture.gcode import router as _gcode
from src.web.routes.manufacture.bundle import router as _bundle
from src.web.routes.manufacture.sse import router as _sse

router = APIRouter(tags=["manufacture"])
router.include_router(_cancel)
router.include_router(_placement)
router.include_router(_routing)
router.include_router(_bitmap)
router.include_router(_scad)
router.include_router(_compile)
router.include_router(_gcode)
router.include_router(_bundle)
router.include_router(_sse)
