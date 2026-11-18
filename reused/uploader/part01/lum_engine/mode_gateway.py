"""Same-origin proxy for isolated Auto Uploader engine applications.

Scene Builder authenticates the parent LUM request. Engine apps listen only
on loopback, receive server-derived identity headers, and keep independent
data/session roots. This does NOT migrate any legacy channel or upload records.
"""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import Request, HTTPException
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

MODE_PORTS = {"engine2": 8016, "shorts": 8017, "song": 8018}
ALLOWED_METHODS = {"GET", "HEAD", "POST"}


def proxy_target(mode: str, path: str, query: str = "") -> str:
    if mode not in MODE_PORTS:
        raise ValueError("Unknown independent engine")
    prefix = f"/local-upload-manager/modes/{mode}/"
    if not path.startswith(prefix) or "\x00" in path or "\\" in path:
        raise ValueError("Invalid independent engine route")
    url = f"http://127.0.0.1:{MODE_PORTS[mode]}{path}"
    return url + (f"?{query}" if query else "")


def trusted_headers(user: dict[str, Any], request: Request) -> dict[str, str]:
    # No user-supplied X-Auth-User, role, host or Authorization is forwarded.
    headers = {
        "x-auth-user": str(user["username"]),
        "x-app-access": "all" if bool(user.get("is_admin")) else "lum",
        "x-is-admin": "1" if bool(user.get("is_admin")) else "0",
    }
    for key in ("accept", "accept-language", "content-type", "cookie", "user-agent"):
        value = request.headers.get(key)
        if value:
            headers[key] = value
    return headers


async def forward(mode: str, request: Request, user: dict[str, Any]) -> Response:
    if request.method not in ALLOWED_METHODS:
        raise HTTPException(405, "Unsupported engine method")
    try:
        url = proxy_target(mode, request.url.path, request.url.query)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=5.0), follow_redirects=False,
        trust_env=False,
    )
    try:
        upstream_request = client.build_request(
            request.method, url, headers=trusted_headers(user, request),
            content=request.stream() if request.method == "POST" else None,
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.RequestError:
        await client.aclose()
        return Response(
            "Independent engine app is not responding", status_code=503,
            media_type="text/plain",
        )
    except BaseException:
        await client.aclose()
        raise

    async def close_stream() -> None:
        await upstream.aclose()
        await client.aclose()

    response_headers = {}
    for key in ("content-type", "content-disposition", "cache-control",
                "location", "set-cookie", "content-encoding"):
        if key in upstream.headers:
            response_headers[key] = upstream.headers[key]
    return StreamingResponse(
        upstream.aiter_raw(), status_code=upstream.status_code,
        headers=response_headers, background=BackgroundTask(close_stream),
    )
