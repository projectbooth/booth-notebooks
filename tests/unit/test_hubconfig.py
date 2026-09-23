"""hubconfig.configure: the security-relevant hub settings, pinned without starting a hub."""

from __future__ import annotations

import pytest
from traitlets.config import Config

from booth_notebooks.hubconfig import ConfigError, configure, sqlalchemy_url, trusted_issuers

ENV = {
    "BOOTH_NOTEBOOKS_DATABASE_DSN": "postgresql://u:p@db:5432/booth_mod_notebooks",
    "JUPYTERHUB_CRYPT_KEY": "00" * 32,
    "BOOTH_IDENTITY_ISSUER_URL": "http://booth-core.booth-system.svc:8080/iframe-identity",
    "BOOTH_CORE_URL": "http://booth-core.booth-system.svc:8080",
    "POD_NAMESPACE": "booth-notebooks",
    "BOOTH_WORKLOAD_MINT_DIR": "/nonexistent",
}


def cfg(**env) -> Config:
    c = Config()
    merged = {**ENV, **env}
    configure(c, {k: v for k, v in merged.items() if v is not None})
    return c


def test_logins_are_verified_and_continuously_rechecked():
    c = cfg()
    assert c.JupyterHub.authenticator_class.__name__ == "BoothAuthenticator"
    assert c.Authenticator.auth_refresh_age == 1  # 0 would DISABLE refresh in JupyterHub
    assert c.Authenticator.refresh_pre_spawn is True
    assert c.Authenticator.enable_auth_state is True
    assert c.BoothAuthenticator.trusted_issuers == [{"url": ENV["BOOTH_IDENTITY_ISSUER_URL"], "audience": "notebooks"}]


def test_nobody_is_a_hub_admin():
    c = cfg()
    assert c.Authenticator.admin_users == set()
    assert c.JupyterHub.admin_access is False


def test_open_notebook_sessions_are_bounded():
    assert cfg().JupyterHub.oauth_token_expires_in == 900
    assert cfg(BOOTH_NOTEBOOKS_SESSION_SECONDS="120").JupyterHub.oauth_token_expires_in == 120


def test_servers_survive_a_hub_restart():
    c = cfg()
    assert c.JupyterHub.cleanup_servers is False
    assert c.ConfigurableHTTPProxy.should_start is False


def test_the_core_provisioned_dsn_becomes_a_sqlalchemy_url():
    assert cfg().JupyterHub.db_url == "postgresql+psycopg2://u:p@db:5432/booth_mod_notebooks"
    assert sqlalchemy_url("postgres://a@b/c") == "postgresql+psycopg2://a@b/c"


def test_a_database_and_crypt_key_are_required():
    with pytest.raises(ConfigError, match="DATABASE_DSN"):
        cfg(BOOTH_NOTEBOOKS_DATABASE_DSN=None)
    assert cfg(BOOTH_NOTEBOOKS_DATABASE_DSN=None, BOOTH_NOTEBOOKS_DEV_SQLITE="true").JupyterHub.db_url.startswith("sqlite")
    with pytest.raises(ConfigError, match="CRYPT_KEY"):
        cfg(JUPYTERHUB_CRYPT_KEY=None)


def test_at_least_one_issuer_is_required_and_idp_audience_rules_match_the_fleet():
    with pytest.raises(ConfigError, match="ISSUER_URL"):
        trusted_issuers({})
    with pytest.raises(ConfigError, match="CLIENT_ID"):
        trusted_issuers({"BOOTH_OIDC_ISSUER_URL": "https://idp"})
    assert trusted_issuers({"BOOTH_OIDC_ISSUER_URL": "https://idp/", "BOOTH_OIDC_REQUIRE_AUDIENCE": "false"}) == [{"url": "https://idp", "audience": ""}]


def test_allowed_roles_are_validated():
    assert cfg(BOOTH_NOTEBOOKS_ALLOWED_ROLES="owner,editor").BoothAuthenticator.allowed_roles == ["owner", "editor"]
    with pytest.raises(ConfigError):
        cfg(BOOTH_NOTEBOOKS_ALLOWED_ROLES="admin")


def test_the_platform_token_endpoint_is_registered_and_minting_is_optional(tmp_path):
    c = cfg()
    routes = {h[0]: h for h in c.JupyterHub.extra_handlers}
    assert routes["/api/booth/platform-token"][2]["minter"] is None  # no Secret: notebooks still work
    assert routes["/booth/healthz"][2] == {"platform_access": False}
    (tmp_path / "credential").write_text("bwmc.notebooks.x")
    (tmp_path / "url").write_text("http://core/api/internal/workload-tokens")
    c = cfg(BOOTH_WORKLOAD_MINT_DIR=str(tmp_path))
    routes = {h[0]: h for h in c.JupyterHub.extra_handlers}
    assert routes["/api/booth/platform-token"][2]["minter"] is not None
    assert routes["/api/booth/platform-token"][2]["gateway_url"] == "http://booth-core.booth-system.svc:8080/modules"


def test_idle_servers_are_culled_by_a_least_privilege_service():
    c = cfg()
    (svc,) = c.JupyterHub.services
    assert svc["name"] == "idle-culler" and "--timeout=3600" in " ".join(svc["command"])
    assert "--url=http://127.0.0.1:8081/hub/api" in svc["command"]  # loopback, not its own Service
    (role,) = c.JupyterHub.load_roles
    assert set(role["scopes"]) == {"list:users", "read:users:activity", "read:servers", "delete:servers"}
    assert cfg(BOOTH_NOTEBOOKS_CULL_IDLE_SECONDS="0").JupyterHub.services == []


def test_framing_follows_the_configured_shell_origin():
    assert cfg().JupyterHub.tornado_settings["headers"]["Content-Security-Policy"] == "frame-ancestors 'self'"
    c = cfg(BOOTH_NOTEBOOKS_FRAME_ANCESTORS="https://booth.example.com")
    assert c.JupyterHub.tornado_settings["headers"]["Content-Security-Policy"] == "frame-ancestors https://booth.example.com"
    assert c.BoothSpawner.environment["BOOTH_FRAME_ANCESTORS"] == "https://booth.example.com"


def test_bad_profiles_json_is_a_config_error():
    with pytest.raises(ConfigError, match="PROFILES"):
        cfg(BOOTH_NOTEBOOKS_PROFILES="{not json")
