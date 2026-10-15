"""The Hub server's calls to its daemon. Only this module and `claim.py` talk to the daemon.

The Hub calls the public `/health`, the Hub-claim pairing routes for its *own* claim, and the owner API with its
`hub` key. It never claims or redeems an App's pairing request: App tokens go from the daemon straight to the App
(agent-harness #519), so the only token response the Hub ever reads is its own Hub key's. The owner API's
`/pairing-requests` routes carry request metadata only: the owner approves, confirms or denies, and the App redeems.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

ADMIN = "/api/admin/v1"
CLAIM_REQUESTS = "/api/v1/pair/requests"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# The redeem mints the Hub key and the daemon answers it only once: wait long enough that a slow reply is not lost.
REDEEM_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
# Failures raised before the request could reach the daemon; any other failure may follow a request it processed.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)

# Closed operation table: browser input never supplies a daemon URL or arbitrary route.
SETTINGS_ROUTES = {
    "schema": ("GET", "/config/schema"),
    "config": ("GET", "/config"),
    "validate": ("POST", "/config/validate"),
    "save": ("PATCH", "/config"),
    "rollback": ("POST", "/config/rollback"),
    "restart": ("POST", "/config/restart"),
    "smart": ("GET", "/smart-approvals"),
    "smart_save": ("PUT", "/smart-approvals"),
    "backends": ("GET", "/backends"),
    "backend_save": ("PUT", "/backends/{name}"),
    "memory": ("GET", "/memory"),
    "memory_save": ("PUT", "/memory/profile"),
    "github": ("GET", "/github-member-auth"),
    "github_save": ("PUT", "/github-member-auth"),
    "google": ("GET", "/google-signin"),
    "notify": ("POST", "/notify/test"),
}

APPS_ROUTES = {
    "revoke": ("DELETE", "/keys/{app_id}"),
    "erasures": ("GET", "/apps/erasures"),
    "keys": ("GET", "/keys"),
    "limits": ("GET", "/apps/{app_id}/limits"),
    "limits_save": ("PUT", "/apps/{app_id}/limits"),
    "retention": ("PUT", "/apps/{app_id}/retention"),
}

# App pairing requests (#519): owner decisions only, plus arming a pre-approved slot for a Hub entry (no secret exists
# until the App claims it). The App-side `claim` and `token` routes are never called for them.
PAIRING_ROUTES = {
    "list": ("GET", "/pairing-requests"),
    "arm": ("POST", "/pairing-requests"),
    "approve": ("POST", "/pairing-requests/{rid}/approve"),
    "confirm": ("POST", "/pairing-requests/{rid}/confirm"),
    "deny": ("POST", "/pairing-requests/{rid}/deny"),
}

ADMIN_ROUTES = {
    "accounts": ("GET", "/accounts"),
    "account": ("GET", "/accounts/{user_id}"),
    "account_add": ("POST", "/accounts"),
    "account_save": ("PATCH", "/accounts/{user_id}"),
    "github_reset": ("POST", "/accounts/{user_id}/github-connection/reset"),
    "google_invite": ("POST", "/accounts/{user_id}/google/invitation"),
    "google_cancel": ("DELETE", "/accounts/{user_id}/google/invitation"),
    "google_revoke": ("POST", "/accounts/{user_id}/google/revoke-sessions"),
    "google_unlink": ("DELETE", "/accounts/{user_id}/google"),
    "account_audit": ("GET", "/accounts/audit"),
    "credentials": ("GET", "/provider-credentials"),
    "credential_set": ("POST", "/provider-credentials"),
    "credential_revoke": ("DELETE", "/provider-credentials/{credential_id}"),
    "skills": ("GET", "/skills"),
    "skills_enabled": ("GET", "/skills/enabled"),
    "proposal": ("GET", "/skills/proposals/{pid}"),
    "skill_install": ("POST", "/skills/proposals/{pid}/install"),
    "skill_reject": ("POST", "/skills/proposals/{pid}/reject"),
    "skill_reopen": ("POST", "/skills/proposals/{pid}/reopen"),
    "skill_review": ("POST", "/skills/proposals/{pid}/review"),
    "proposal_delete": ("DELETE", "/skills/proposals/{pid}"),
    "skill_enable": ("POST", "/skills/{slug}/enable"),
    "skill_disable": ("POST", "/skills/{slug}/disable"),
    "skill_projects": ("PUT", "/skills/{slug}/projects"),
    "skill_rollback": ("POST", "/skills/{slug}/rollback"),
    "skill_uninstall": ("POST", "/skills/{slug}/uninstall"),
    "skill_export": ("GET", "/skills/{slug}/export"),
    "maintenance": ("GET", "/maintenance"),
    "cleanup": ("POST", "/maintenance/cleanup"),
    "backup": ("POST", "/maintenance/backup"),
    "retention_preview": ("POST", "/maintenance/image-archive/retention/preview"),
    "retention_apply": ("POST", "/maintenance/image-archive/retention/apply"),
    "runners": ("GET", "/runners"),
    "runner_update": ("POST", "/runners/{name}/update"),
    "projects": ("GET", "/projects"),
    "project_add": ("POST", "/projects"),
    "remote": ("GET", "/remote-control"),
    "folder_trust": ("POST", "/remote-control/{project}/trust"),
    "folder_forget": ("DELETE", "/remote-control/folders/{slug}"),
    "scan_start": ("POST", "/remote-control/discovery/scans"),
    "scan": ("GET", "/remote-control/discovery/scans/{scan_id}"),
    "scan_cancel": ("DELETE", "/remote-control/discovery/scans/{scan_id}"),
    "scan_promote": ("POST", "/remote-control/discovery/scans/{scan_id}/candidates/{candidate_id}/promote"),
    "audit": ("GET", "/audit"),
}


class DaemonUnreachable(Exception):
    """The daemon did not answer (stopped, restarting, or the URL is wrong).

    `sent` is False only when the request certainly never reached the daemon (it could not connect).
    """

    def __init__(self, message: str, *, sent: bool = True):
        super().__init__(message)
        self.sent = sent


@dataclass
class Reply:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def code(self) -> str:
        """The daemon's machine-readable error code (`error.code`), or ''."""
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), dict):
            return str(self.body["error"].get("code") or "")
        return ""

    @property
    def detail(self) -> str:
        if isinstance(self.body, dict) and isinstance(self.body.get("detail"), str):
            return self.body["detail"]
        return ""


class DaemonClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url
        # No Origin header is ever sent: the Hub claims as a native client, so its key is not bound to a browser.
        # trust_env=False: proxy settings (HTTP_PROXY and friends, or the Windows system proxy) must never
        # see the Hub key or the claim verifier, and a proxy cannot reach the loopback daemon anyway.
        # The SSL context still honours SSL_CERT_FILE / SSL_CERT_DIR, so a private-CA HTTPS daemon keeps working.
        self._client = httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT, transport=transport,
                                         follow_redirects=False, trust_env=False,
                                         verify=httpx.create_ssl_context(trust_env=True),
                                         headers={"Accept": "application/json"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, *, token: str = "", json: Any = None,
                    params: dict | None = None, timeout: float | httpx.Timeout = TIMEOUT) -> Reply:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self._client.request(
                method, path, json=json, headers=headers, params=params, timeout=timeout)
        except httpx.HTTPError as exc:
            raise DaemonUnreachable(type(exc).__name__, sent=not isinstance(exc, NOT_SENT)) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        return Reply(response.status_code, body)

    async def health(self) -> Reply:
        return await self._call("GET", "/health")

    async def inventory(self, token: str) -> Reply:
        return await self._call("GET", ADMIN + "/hub", token=token)

    async def apps(self, token: str, operation: str, app_id: str = "", body: dict | None = None) -> Reply:
        method, path = APPS_ROUTES[operation]
        return await self._call(method, ADMIN + path.format(app_id=app_id), token=token, json=body)

    async def pairing(self, token: str, operation: str, rid: str = "", body: dict | None = None) -> Reply:
        method, path = PAIRING_ROUTES[operation]
        # The server validates `rid` as one path segment before dispatch.
        return await self._call(method, ADMIN + path.format(rid=rid), token=token, json=body)

    async def settings(self, token: str, operation: str, body: dict | None = None, name: str = "") -> Reply:
        method, path = SETTINGS_ROUTES[operation]
        if operation == "backend_save":
            # The server validates this single path segment before dispatch.
            path = path.format(name=name)
        return await self._call(method, ADMIN + path, token=token, json=body)

    async def administration(self, token: str, operation: str, targets: dict, body: dict | None = None,
                             params: dict | None = None) -> Reply:
        method, path = ADMIN_ROUTES[operation]
        return await self._call(method, ADMIN + path.format(**targets), token=token, json=body, params=params,
                                timeout=310.0 if operation in ("runner_update", "backup", "cleanup") else TIMEOUT)

    async def start_hub_claim(self, name: str, challenge: str) -> Reply:
        return await self._call("POST", CLAIM_REQUESTS, json={
            "name": name, "kind": "hub", "code_challenge": challenge, "code_challenge_method": "S256"})

    async def redeem_hub_claim(self, request_id: str, verifier: str) -> Reply:
        """Redeem this Hub's own claim. The caller (`claim.py`) checks that the key it gets has role `hub`."""
        return await self._call("POST", f"{CLAIM_REQUESTS}/{request_id}/token", json={"code_verifier": verifier},
                                timeout=REDEEM_TIMEOUT)

    async def admin(self, token: str, path: str = "") -> Reply:
        """A read on the owner API with the Hub key."""
        return await self._call("GET", ADMIN + path, token=token)
