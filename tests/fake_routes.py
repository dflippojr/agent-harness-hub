"""The fake daemon's route table, built with the decorator names its `register_*` modules have always used (#254).

The fakes register handlers that take `request` and path parameters by name and return dicts, as they did on FastAPI;
`HubRoute` (harness_hub/routes/common.py) calls them that way on Starlette, and answers exactly the methods given, so
the contract coverage test sees no added HEAD."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from starlette.applications import Starlette
from starlette.routing import Route

from harness_hub.routes.common import HubRoute

Handler = Callable[..., Awaitable[Any]]


class FakeRoutes:
    """Collects routes in registration order; `app()` serves them, the first match winning, as on FastAPI."""

    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add_api_route(self, path: str, handler: Handler, methods: Iterable[str]) -> None:
        self.routes.append(HubRoute(path, handler, methods))

    def api_route(self, path: str, methods: Iterable[str]) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self.add_api_route(path, handler, methods)
            return handler

        return register

    def get(self, path: str) -> Callable[[Handler], Handler]:
        return self.api_route(path, ["GET"])

    def post(self, path: str) -> Callable[[Handler], Handler]:
        return self.api_route(path, ["POST"])

    def put(self, path: str) -> Callable[[Handler], Handler]:
        return self.api_route(path, ["PUT"])

    def delete(self, path: str) -> Callable[[Handler], Handler]:
        return self.api_route(path, ["DELETE"])

    def app(self) -> Starlette:
        return Starlette(routes=self.routes)
