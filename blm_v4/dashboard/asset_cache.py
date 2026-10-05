"""Static-asset cache policy for the operator dashboard.

A deploy must never be masked by a browser's heuristic cache.  The
2026-10-05 incident: the server served a fixed ``dashboard.js``, but a
dashboard tab kept executing the PRE-FIX bundle because the asset was sent
with only ``ETag``/``Last-Modified`` (no ``Cache-Control``) and referenced
without a version query — so the browser reused a stale copy and the unit
price field stayed disabled even on a healthy 200.

``Cache-Control: no-cache`` means "store, but revalidate before every use":
the ETag stays authoritative, so a changed bundle is picked up on the very
next load, while an unchanged one costs a cheap 304.  That is exactly the
deterministic invalidation a deploy needs; ``max-age`` + a content hash would
only trade this for fewer requests, which this dashboard does not need.
"""
from __future__ import annotations

from fastapi.staticfiles import StaticFiles

NO_CACHE = "no-cache"


class NoCacheStaticFiles(StaticFiles):
    """``StaticFiles`` that forbids stale reuse by forcing revalidation."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = NO_CACHE
        return response
