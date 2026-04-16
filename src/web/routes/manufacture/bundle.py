from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from src.web.routes._deps import load_session_or_404

router = APIRouter()


@router.get("/sessions/{sid}/manufacture/bundle")
async def download_bundle(sid: str):
    s = load_session_or_404(sid)
    gcode_path = s.artifact_path("enclosure.gcode")
    if not gcode_path.exists():
        raise HTTPException(404, "Missing manufacturing file: enclosure_staged.gcode")

    bitmap_path = s.artifact_path("trace_bitmap.txt")
    if not bitmap_path.exists():
        raise HTTPException(404, "Missing trace_bitmap.txt — run the bitmap step first")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(gcode_path, "enclosure.gcode")
        zf.write(bitmap_path, "trace_bitmap.txt")
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{s.name or s.id}_bundle.zip"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@router.get("/sessions/{sid}/manufacture/print-job")
async def download_print_job(sid: str):
    s = load_session_or_404(sid)
    path = s.artifact_path("print_job.json")
    if not path.exists():
        raise HTTPException(404, "print_job.json not found")
    return FileResponse(path, filename="print_job.json", media_type="application/json")
