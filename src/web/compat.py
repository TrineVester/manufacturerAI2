"""Compatibility middleware that rewrites old-style frontend routes.

Old frontend:  /api/session/X?session=ID
New backend:   /api/sessions/{id}/X

This middleware intercepts old-style requests and rewrites the ASGI
scope so they reach the correct new-style route handlers.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode

from starlette.types import ASGIApp, Receive, Scope, Send

# Old path suffix → new path suffix
# Old: /api/session/{suffix}?session={sid}
# New: /api/sessions/{sid}/{new_suffix}
_COMPAT: dict[str, str] = {
    "":                             "",
    "conversation":                 "design/conversation",
    "tokens":                       "design/tokens",
    "design":                       "design",
    "design/result":                "design",
    "design/enclosure":             "design/enclosure",
    "design/stream":                "design/stream",
    "design/status":                "design/status",
    "design/stop":                  "design/stop",
    "circuit":                      "circuit",
    "circuit/conversation":         "circuit/conversation",
    "circuit/result":               "circuit",
    "circuit/stream":               "circuit/stream",
    "circuit/status":               "circuit/status",
    "circuit/stop":                 "circuit/stop",
    "catalog":                      "catalog",
    "placement":                    "manufacture/placement",
    "placement/result":             "manufacture/placement",
    "placement/status":               "manufacture/placement/status",
    "routing":                      "manufacture/routing",
    "routing/result":               "manufacture/routing",
    "routing/status":                "manufacture/routing/status",
    "scad":                         "manufacture/scad",
    "scad/result":                  "manufacture/scad",
    "scad/status":                  "manufacture/scad/status",
    "scad/stl":                     "manufacture/stl",
    "scad/extras-stl":              "manufacture/extras-stl",
    "scad/compile":                 "manufacture/compile",
    "manufacturing/gcode":          "manufacture/gcode",
    "manufacturing/bitmap":         "manufacture/bitmap",
    "manufacturing/bitmap/status":  "manufacture/bitmap/status",
    "manufacturing/manifest":       "manufacture/bundle",
    "manufacturing/gcode/download": "manufacture/gcode/download",
    "manufacturing/bitmap/download":"manufacture/bitmap/download",
    # firmware / assembly / simulator are frontend features without
    # dedicated backend routes yet.  Entries are omitted so these
    # fall through as 404s, which the JS error-handles gracefully.
    "firmware/download":            "manufacture/bundle",     # best-effort: download all
}

_PREFIX = "/api/session"


class LegacyRouteRewriter:
    """ASGI middleware: rewrite /api/session/…?session=X → /api/sessions/X/…"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path: str = scope["path"]
            if path == _PREFIX or path.startswith(_PREFIX + "/"):
                qs = scope.get("query_string", b"").decode()
                params = parse_qs(qs)
                sid = params.get("session", [None])[0]

                if sid:
                    suffix = path[len(_PREFIX) + 1:] if len(path) > len(_PREFIX) else ""
                    new_suffix = _COMPAT.get(suffix)

                    if new_suffix is not None:
                        new_path = f"/api/sessions/{sid}"
                        if new_suffix:
                            new_path += f"/{new_suffix}"

                        remaining = {k: v for k, v in params.items() if k != "session"}
                        new_qs = urlencode(remaining, doseq=True)

                        scope = dict(scope)
                        scope["path"] = new_path
                        scope["query_string"] = new_qs.encode()
                        scope["raw_path"] = new_path.encode()

        await self.app(scope, receive, send)
