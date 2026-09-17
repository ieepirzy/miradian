"""HTTP/JSON surface for the structured-document service.

Reads are GET with query parameters, mutations are POST with a JSON body. It is
RPC-flavoured rather than REST-shaped on purpose: document ids are opaque and paths
are path-shaped, and a percent-encoded `/` inside a URL path segment is normalised
by some ASGI servers, rejected by others, and mangled by proxies in between. Keeping
identifiers in the query string or the body sidesteps a class of bug that is
miserable to diagnose from the client side.

Auth here is a static bearer token rather than the OAuth the MCP endpoints use.
origo implements only `authorization_code` and `refresh_token`, so a headless
consumer cannot obtain a token without a browser; and its middleware's public-path
list is exact-match, so it cannot express "/api/v1/*" either. A shared secret is the
honest mechanism for service-to-service calls on a private network. Tokens the OAuth
provider issued are accepted too, so a human or agent that already holds one does
not need a second credential.
"""

from __future__ import annotations

import hmac
import logging
from typing import Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from changes import ChangeJournal
from documents import DocumentError, DocumentService, OutOfScope

logger = logging.getLogger("miradian.api")

API_PREFIX = "/api/v1"


def parse_tokens(raw: str) -> dict[str, str]:
    """Parse MIRADIAN_API_TOKENS: `name:secret,name2:secret2`.

    Names exist so a log line can say which consumer called without the secret
    appearing anywhere.
    """
    tokens: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, secret = item.partition(":")
        if not sep or not secret.strip():
            logger.warning("ignoring malformed entry in MIRADIAN_API_TOKENS (expected name:secret)")
            continue
        tokens[name.strip()] = secret.strip()
    return tokens


def _bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw))) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _error(exc: DocumentError) -> JSONResponse:
    return JSONResponse(
        {"error": exc.code, "message": str(exc), **exc.details()},
        status_code=exc.status,
    )


def create_api(
    service: DocumentService,
    journal: ChangeJournal,
    *,
    tokens: dict[str, str] | None = None,
    verify_oauth: Callable[[str], bool] | None = None,
    require_auth: bool = True,
    capabilities: dict | None = None,
) -> Starlette:
    """Build the JSON API app.

    `service` and `journal` are the same objects the MCP tools use. There is one
    implementation of everything below this line.
    """
    tokens = tokens or {}
    capabilities = capabilities or {}

    def authorise(request: Request) -> str | None:
        """The calling consumer's name, or None if the request is not authorised."""
        if not require_auth:
            return "anonymous"
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        presented = header[len("Bearer ") :].strip()
        if not presented:
            return None
        for name, secret in tokens.items():
            # compare_digest, not ==: a plain comparison returns early on the first
            # differing byte, which leaks the secret's prefix to a patient caller.
            if hmac.compare_digest(presented, secret):
                return name
        if verify_oauth and verify_oauth(presented):
            return "oauth"
        return None

    def guard_scope(*paths: str | None) -> None:
        """Keep the API inside the configured project roots.

        The service layer can address the whole vault because the MCP tools need
        that. A project-state consumer does not, and should not be one typo away
        from writing into the personal knowledge base.
        """
        for path in paths:
            if path and not service.path_in_scope(path.strip().lstrip("/")):
                raise OutOfScope(
                    f"{path!r} is outside the configured project roots "
                    f"({', '.join(service.roots) or 'none configured'}). "
                    "The document API only addresses project documents."
                )

    # --- reads -------------------------------------------------------------

    async def health(request: Request) -> JSONResponse:
        # Deliberately unauthenticated: container healthchecks need it and it
        # discloses nothing about the vault.
        return JSONResponse({"status": "ok"})

    async def root(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "service": "miradian",
                "api_version": "v1",
                "capabilities": capabilities,
                "project_roots": service.roots,
                "doc_type_field": service.doc_type_field,
                "uid_field": service.uid_field,
            }
        )

    async def list_projects(request: Request) -> JSONResponse:
        return JSONResponse({"projects": [p.as_dict() for p in service.discover_projects()]})

    async def list_folders(request: Request) -> JSONResponse:
        project = request.query_params.get("project")
        guard_scope(project)
        return JSONResponse({"folders": service.list_folders(project)})

    async def list_documents(request: Request) -> JSONResponse:
        q = request.query_params
        project = q.get("project")
        guard_scope(project, q.get("path_prefix"))
        refs = service.list_documents(
            project,
            doc_type=q.get("doc_type"),
            include_unstructured=_bool(q.get("include_unstructured")),
            path_prefix=q.get("path_prefix"),
        )
        limit = _int(q.get("limit"), 100, 1, 1000)
        offset = max(0, _int(q.get("offset"), 0, 0, 10**9))
        window = refs[offset : offset + limit]
        return JSONResponse(
            {
                "documents": [r.as_dict() for r in window],
                "total": len(refs),
                "count": len(window),
                "offset": offset,
                "has_more": offset + len(window) < len(refs),
                "next_offset": offset + len(window) if offset + len(window) < len(refs) else None,
            }
        )

    async def get_document(request: Request) -> JSONResponse:
        q = request.query_params
        guard_scope(q.get("path"))
        doc = service.read_document(id=q.get("id"), path=q.get("path"))
        guard_scope(doc.path)
        return JSONResponse({"document": doc.as_dict()})

    async def get_changes(request: Request) -> JSONResponse:
        q = request.query_params
        return JSONResponse(journal.poll(q.get("cursor"), limit=_int(q.get("limit"), 500, 1, 2000)))

    # --- mutations ---------------------------------------------------------

    async def _body(request: Request) -> dict:
        try:
            payload = await request.json()
        except Exception as e:
            raise DocumentError(f"Request body must be JSON: {e}") from e
        if not isinstance(payload, dict):
            raise DocumentError("Request body must be a JSON object.")
        return payload

    async def create_document(request: Request) -> JSONResponse:
        p = await _body(request)
        path = p.get("path")
        if not path:
            raise DocumentError("'path' is required: a new document needs a location.")
        guard_scope(path)
        doc = service.create_document(path, body=p.get("body", ""), metadata=p.get("metadata"))
        return JSONResponse({"document": doc.as_dict()}, status_code=201)

    async def write_document(request: Request) -> JSONResponse:
        p = await _body(request)
        guard_scope(p.get("path"))
        if "body" not in p:
            raise DocumentError("'body' is required. To change metadata only, use patch-metadata.")
        doc = service.write_document(
            id=p.get("id"),
            path=p.get("path"),
            body=p["body"],
            metadata=p.get("metadata"),
            expected_version=p.get("expected_version"),
        )
        guard_scope(doc.path)
        return JSONResponse({"document": doc.as_dict()})

    async def patch_metadata(request: Request) -> JSONResponse:
        p = await _body(request)
        guard_scope(p.get("path"))
        doc, missing = service.patch_metadata(
            id=p.get("id"),
            path=p.get("path"),
            set_fields=p.get("set"),
            unset_fields=p.get("unset"),
            expected_version=p.get("expected_version"),
        )
        guard_scope(doc.path)
        return JSONResponse({"document": doc.as_dict(), "not_present": missing})

    async def move_document(request: Request) -> JSONResponse:
        p = await _body(request)
        if not p.get("to"):
            raise DocumentError("'to' is required: the new path for the document.")
        guard_scope(p.get("path"), p["to"])
        doc = service.move_document(
            id=p.get("id"),
            path=p.get("path"),
            to=p["to"],
            expected_version=p.get("expected_version"),
        )
        guard_scope(doc.path)
        return JSONResponse({"document": doc.as_dict()})

    async def delete_document(request: Request) -> JSONResponse:
        p = await _body(request)
        guard_scope(p.get("path"))
        result = service.delete_document(
            id=p.get("id"), path=p.get("path"), expected_version=p.get("expected_version")
        )
        return JSONResponse(result)

    routes = [
        Route(f"{API_PREFIX}/health", health, methods=["GET"]),
        Route(f"{API_PREFIX}/", root, methods=["GET"]),
        Route(API_PREFIX, root, methods=["GET"]),
        Route(f"{API_PREFIX}/projects", list_projects, methods=["GET"]),
        Route(f"{API_PREFIX}/folders", list_folders, methods=["GET"]),
        Route(f"{API_PREFIX}/documents", list_documents, methods=["GET"]),
        Route(f"{API_PREFIX}/documents/get", get_document, methods=["GET"]),
        Route(f"{API_PREFIX}/documents/create", create_document, methods=["POST"]),
        Route(f"{API_PREFIX}/documents/write", write_document, methods=["POST"]),
        Route(f"{API_PREFIX}/documents/patch-metadata", patch_metadata, methods=["POST"]),
        Route(f"{API_PREFIX}/documents/move", move_document, methods=["POST"]),
        Route(f"{API_PREFIX}/documents/delete", delete_document, methods=["POST"]),
        Route(f"{API_PREFIX}/changes", get_changes, methods=["GET"]),
    ]

    async def on_document_error(request: Request, exc: DocumentError) -> JSONResponse:
        return _error(exc)

    async def authenticate(request: Request, call_next):
        if request.url.path != f"{API_PREFIX}/health":
            if require_auth and not tokens and not verify_oauth:
                # Fail closed. An upgrade that adds this API to an existing
                # deployment must not silently expose the vault because nobody
                # knew there was a new variable to set.
                return JSONResponse(
                    {
                        "error": "not_configured",
                        "message": "No API credentials are configured. Set MIRADIAN_API_TOKENS.",
                    },
                    status_code=503,
                )
            if authorise(request) is None:
                return JSONResponse(
                    {"error": "unauthorized", "message": "A valid bearer token is required."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    # Exception handlers are registered by class, so every DocumentError subclass is
    # covered -- including ones added later -- rather than each endpoint repeating a
    # try/except. The auth middleware wraps the lot.
    return Starlette(
        routes=routes,
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=authenticate)],
        exception_handlers={DocumentError: on_document_error},
    )


class ApiDispatcher:
    """Route `/api/*` to the JSON API and everything else to the OAuth-protected app.

    Starlette's Mount would work but rewrites `path` and sets `root_path`, which the
    MCP transports and origo's exact-path matching both read. A dispatcher that
    forwards the scope untouched is twenty lines and changes nothing about how the
    existing app sees a request.

    `lifespan` scopes always go to the MCP app: FastMCP starts its session managers
    there, and a lifespan delivered to the wrong app fails at request time rather
    than at startup, which is a bad way to find out.
    """

    def __init__(self, mcp_app, api_app, prefix: str = "/api/"):
        self.mcp_app = mcp_app
        self.api_app = api_app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith(self.prefix):
            await self.api_app(scope, receive, send)
            return
        await self.mcp_app(scope, receive, send)
