# JupyterHub config for tests/hub: the production configuration (hubconfig.configure), with only the
# pieces that need a cluster swapped out — the spawner (no Kubernetes), the proxy (no
# configurable-http-proxy) and the database file location. Everything under test (authenticator,
# refresh, handlers, minting) is exactly what ships.
import os

from jupyterhub.proxy import Proxy

from booth_notebooks.hubconfig import configure

configure(c)  # noqa: F821


class InMemoryProxy(Proxy):
    """No external proxy: the tests talk to the hub directly on its own port."""

    should_start = False

    def __init__(self, **kw):
        super().__init__(**kw)
        self._routes = {}

    async def add_route(self, routespec, target, data):
        self._routes[routespec] = {"routespec": routespec, "target": target, "data": data}

    async def delete_route(self, routespec):
        self._routes.pop(routespec, None)

    async def get_all_routes(self):
        return dict(self._routes)


c.JupyterHub.proxy_class = InMemoryProxy  # noqa: F821
c.JupyterHub.spawner_class = "jupyterhub.spawner.SimpleLocalProcessSpawner"  # noqa: F821
c.JupyterHub.hub_bind_url = f"http://127.0.0.1:{os.environ['TEST_HUB_PORT']}"  # noqa: F821
c.JupyterHub.db_url = f"sqlite:///{os.environ['TEST_HUB_DB']}"  # noqa: F821
# A test-only service that can create API tokens for users (standing in for what JupyterHub does for a
# pod at spawn time: hand it a token owned by that user).
c.JupyterHub.services = [{"name": "test-harness", "api_token": os.environ["TEST_SERVICE_TOKEN"]}]  # noqa: F821
c.JupyterHub.load_roles = [  # noqa: F821
    {"name": "test-harness", "scopes": ["admin:users", "tokens", "read:users"], "services": ["test-harness"]}
]
