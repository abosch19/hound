"""Servidor MCP (Streamable HTTP) con autenticación por token personal."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .auth import TokenStore
from .search import AccessDenied, SearchIndex, User

log = logging.getLogger("hound.server")

INSTRUCTIONS = """\
Hound busca en los ficheros de la carpeta compartida de la empresa por significado y por palabras clave.
Solo devuelve ficheros que el usuario puede abrir en el disco con sus permisos de Windows.

- search_files: primero. Describe lo que buscas en lenguaje natural ("el contrato con el proveedor de
  limpieza", "factura de marzo de Pérez"). Filtra por carpeta, tipo (pdf, word, excel, powerpoint,
  email, text, image, cad, other) o fechas de modificación (AAAA-MM-DD).
- read_document: lee el texto extraído de un fichero por tramos (start/end son índices de fragmento);
  usa next_start para seguir leyendo.
- list_folder y recent_files: para explorar.
Las rutas se devuelven como rutas UNC que el usuario puede abrir en el Explorador de Windows.
"""


def _bearer(headers) -> str | None:
    if not headers:
        return None
    value = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


class BearerAuthMiddleware:
    """Rechaza con 401 cualquier petición al MCP sin un token válido."""

    def __init__(self, app, store: TokenStore, protected_prefix: str = "/mcp"):
        self.app = app
        self.store = store
        self.prefix = protected_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            user = await anyio.to_thread.run_sync(self.store.resolve, _bearer(headers))
            if user is None:
                resp = JSONResponse({"error": "Token no válido o revocado"}, status_code=401)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_mcp(index: SearchIndex, store: TokenStore) -> MCPServer:
    mcp = MCPServer(name="hound", title="Hound: buscador de la carpeta compartida", instructions=INSTRUCTIONS)

    def current_user(ctx: Context) -> User:
        user = store.resolve(_bearer(ctx.headers))
        if user is None:
            raise ToolError("Token no válido o revocado")
        return user

    def run(ctx: Context, tool: str, args: dict[str, Any], fn):
        user = current_user(ctx)
        t0 = time.monotonic()
        n = None
        try:
            out = fn(user)
            n = len(out) if isinstance(out, list) else None
            return out
        except AccessDenied:
            raise ToolError("El fichero no existe o no tienes permiso para verlo") from None
        except ValueError as exc:  # parámetros no válidos: fechas, tipo, ruta fuera de la carpeta
            raise ToolError(str(exc)) from None
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            store.log_query(user, tool, json.dumps({k: v for k, v in args.items() if v is not None},
                                                   ensure_ascii=False), n, ms)
            log.info("%s %s %s (%d ms)", user.name, tool, args.get("query") or args.get("path") or "", ms)

    @mcp.tool()
    def search_files(query: str, ctx: Context, folder: str | None = None, file_type: str | None = None,
                     date_from: str | None = None, date_to: str | None = None, limit: int = 10) -> dict:
        """Busca ficheros por significado y palabras clave.

        Args:
            query: lo que se busca, en lenguaje natural.
            folder: limita la búsqueda a esta carpeta (ruta UNC o relativa a la carpeta compartida).
            file_type: pdf, word, excel, powerpoint, email, text, image, cad u other (o una extensión).
            date_from: fecha de modificación mínima, AAAA-MM-DD.
            date_to: fecha de modificación máxima, AAAA-MM-DD.
            limit: número máximo de ficheros (1-50).

        Devuelve ruta, nombre, ubicación de la coincidencia (página, hoja…), fragmento, fecha y puntuación.
        """
        args = dict(query=query, folder=folder, file_type=file_type, date_from=date_from, date_to=date_to,
                    limit=limit)
        results = run(ctx, "search_files", args,
                      lambda u: index.search(u, query, folder, file_type, date_from, date_to, limit))
        return {"results": results}

    @mcp.tool()
    def read_document(path: str, ctx: Context, start: int = 0, end: int | None = None) -> dict:
        """Lee el texto extraído de un fichero, por tramos (fragmentos start..end-1).

        Args:
            path: ruta del fichero tal como la devuelve search_files.
            start: primer fragmento (0 por defecto).
            end: fragmento final, excluido (por defecto, hasta llenar ~30.000 caracteres).
        """
        return run(ctx, "read_document", dict(path=path, start=start, end=end),
                   lambda u: index.read_document(u, path, start, end))

    @mcp.tool()
    def list_folder(ctx: Context, path: str = "") -> dict:
        """Lista las subcarpetas y ficheros indexados de una carpeta (vacío = raíz de la carpeta compartida)."""
        return run(ctx, "list_folder", dict(path=path), lambda u: index.list_folder(u, path))

    @mcp.tool()
    def recent_files(ctx: Context, limit: int = 20, folder: str | None = None) -> dict:
        """Ficheros modificados más recientemente.

        Args:
            limit: cuántos (1-200).
            folder: limita a esta carpeta (opcional).
        """
        return {"files": run(ctx, "recent_files", dict(limit=limit, folder=folder),
                             lambda u: index.recent_files(u, limit, folder))}

    return mcp


def build_app(index: SearchIndex, store: TokenStore) -> BearerAuthMiddleware:
    mcp = build_mcp(index, store)
    mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[Route("/health", health), Mount("/", app=mcp_app)],
        lifespan=lambda _app: mcp_app.router.lifespan_context(mcp_app),
    )
    return BearerAuthMiddleware(app, store)
