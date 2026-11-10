from __future__ import annotations

from typing import Any

import httpx


class OpenCodeError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def parse_models(data: Any) -> list[dict[str, str]]:
    """Extract provider/model pairs from /api/config (legacy)."""
    providers: dict[str, Any] = {}
    if isinstance(data, list):
        for doc in data:
            if not isinstance(doc, dict):
                continue
            info = doc.get("info") or {}
            if isinstance(info, dict) and info.get("providers"):
                providers = info["providers"]
                break
    elif isinstance(data, dict):
        info = data.get("info") or {}
        if isinstance(info, dict):
            providers = info.get("providers") or {}

    if not isinstance(providers, dict):
        return []

    models: list[dict[str, str]] = []
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        provider_name = provider.get("name") or provider_id
        provider_models = provider.get("models") or {}
        if not isinstance(provider_models, dict):
            continue
        for model_id, model in provider_models.items():
            model_name = str(model_id)
            if isinstance(model, dict) and model.get("name"):
                model_name = str(model["name"])
            models.append(
                {
                    "provider_id": str(provider_id),
                    "provider_name": str(provider_name),
                    "model_id": str(model_id),
                    "model_name": model_name,
                }
            )
    return models


def parse_provider_names(data: Any) -> dict[str, str]:
    """Extract provider id -> display name from /api/provider."""
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}
    names: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        provider_id = item.get("id")
        if not provider_id:
            continue
        names[str(provider_id)] = str(item.get("name") or provider_id)
    return names


def parse_model_catalog(
    data: Any, provider_names: dict[str, str] | None = None
) -> list[dict[str, str]]:
    """Extract provider/model pairs from /api/model.

    Unlike /api/config, /api/model also includes built-in providers such as
    the free ``opencode`` models.
    """
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    names = provider_names or {}
    models: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        provider_id = item.get("providerID") or item.get("provider_id")
        model_id = (
            item.get("modelID") or item.get("model_id") or item.get("id")
        )
        if not provider_id or not model_id:
            continue
        if item.get("enabled") is False:
            continue
        status = item.get("status")
        if status and status != "active":
            continue
        model_type = item.get("type")
        if model_type and model_type != "chat":
            continue
        key = (str(provider_id), str(model_id))
        if key in seen:
            continue
        seen.add(key)
        models.append(
            {
                "provider_id": str(provider_id),
                "provider_name": names.get(
                    str(provider_id), str(provider_id)
                ),
                "model_id": str(model_id),
                "model_name": str(item.get("name") or model_id),
            }
        )
    return models


def parse_agents(data: Any) -> list[dict[str, Any]]:
    """Extract selectable primary agents from /api/agent.

    Subagents and hidden maintenance agents are not selectable as the main
    agent for a session, so they are filtered out.
    """
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    agents: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("hidden"):
            continue
        mode = item.get("mode")
        if mode == "subagent":
            continue
        agent_id = item.get("id")
        if not agent_id:
            continue
        permissions = item.get("permissions")
        agents.append(
            {
                "id": str(agent_id),
                "name": str(item.get("name") or agent_id),
                "description": str(item.get("description") or ""),
                "mode": str(mode or "primary"),
                "permissions": permissions
                if isinstance(permissions, list)
                else [],
            }
        )
    return agents


class OpenCodeClient:
    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url.rstrip("/")
        self._auth = (username, password)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                auth=self._auth,
                timeout=httpx.Timeout(30.0, connect=5.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        client = self._get_client()
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OpenCodeError(f"OpenCode request failed: {exc}") from exc

        if response.status_code >= 400:
            try:
                body: Any = response.json()
            except Exception:
                body = response.text[:1000]
            raise OpenCodeError(
                f"OpenCode HTTP {response.status_code}: {body}",
                response.status_code,
                body,
            )

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except Exception:
            return response.text

    async def create_session(
        self,
        title: str,
        directory: str,
        agent: str,
        provider_id: str,
        model_id: str,
        permissions: list[dict[str, str]],
    ) -> str:
        payload = {
            "title": title,
            "location": {"directory": directory},
            "agent": agent,
            "model": {"providerID": provider_id, "id": model_id},
            "permissions": permissions,
        }
        data = await self._request("POST", "/api/session", json=payload)
        return data["data"]["id"]

    async def prompt(self, session_id: str, text: str) -> Any:
        payload = {"text": text, "delivery": "queue"}
        return await self._request(
            "POST", f"/api/session/{session_id}/prompt", json=payload
        )

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            data = await self._request(
                "GET", f"/api/session/{session_id}"
            )
        except OpenCodeError as exc:
            if exc.status_code == 404:
                return None
            raise
        if isinstance(data, dict):
            payload = data.get("data")
            if isinstance(payload, dict):
                return payload
        return None

    async def delete_session(self, session_id: str) -> None:
        try:
            await self._request(
                "DELETE", f"/api/session/{session_id}"
            )
        except OpenCodeError as exc:
            # Already gone is fine: the bridge may be cleaning up a session
            # that was removed from another client.
            if exc.status_code != 404:
                raise

    async def list_active_sessions(self) -> set[str]:
        data = await self._request("GET", "/api/session/active")
        if isinstance(data, dict):
            payload = data.get("data")
            if isinstance(payload, dict):
                return {str(key) for key in payload}
        return set()

    async def last_message(
        self,
        session_id: str,
        message_type: str,
        after_ts: float | None = None,
        limit: int = 5,
    ) -> dict[str, Any] | None:
        """Return the newest message of ``message_type`` after ``after_ts``."""
        data = await self._request(
            "GET",
            f"/api/session/{session_id}/message",
            params={
                "type": message_type,
                "order": "desc",
                "limit": str(limit),
            },
        )
        messages = data.get("data") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return None
        cutoff = int(after_ts * 1000) - 5000 if after_ts else None
        for message in messages:
            if not isinstance(message, dict):
                continue
            created = (message.get("time") or {}).get("created")
            if (
                cutoff is not None
                and isinstance(created, (int, float))
                and created < cutoff
            ):
                continue
            return message
        return None

    async def last_assistant_message(
        self, session_id: str, after_ts: float | None = None
    ) -> dict[str, Any] | None:
        return await self.last_message(
            session_id, "assistant", after_ts=after_ts
        )

    async def last_idle_message(
        self, session_id: str, after_ts: float | None = None
    ) -> dict[str, Any] | None:
        return await self.last_message(
            session_id, "idle", after_ts=after_ts
        )

    async def reply_permission(
        self, session_id: str, permission_id: str, decision: str
    ) -> None:
        await self._request(
            "POST",
            f"/api/session/{session_id}/permission/{permission_id}/reply",
            json={"decision": decision},
        )

    async def list_session_forms(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        try:
            data = await self._request(
                "GET", f"/api/session/{session_id}/form"
            )
        except OpenCodeError as exc:
            if exc.status_code == 404:
                return []
            raise
        if isinstance(data, dict):
            payload = data.get("data")
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
        return []

    async def get_form(
        self, session_id: str, form_id: str
    ) -> dict[str, Any] | None:
        try:
            data = await self._request(
                "GET", f"/api/session/{session_id}/form/{form_id}"
            )
        except OpenCodeError as exc:
            if exc.status_code == 404:
                return None
            raise
        if isinstance(data, dict):
            payload = data.get("data")
            if isinstance(payload, dict):
                return payload
        return None

    async def reply_form(
        self, session_id: str, form_id: str, answer: dict[str, Any]
    ) -> None:
        await self._request(
            "POST",
            f"/api/session/{session_id}/form/{form_id}/reply",
            json={"answer": answer},
        )

    async def cancel_form(
        self,
        session_id: str,
        form_id: str,
        message: str | None = None,
    ) -> None:
        params = {"message": message} if message else None
        try:
            await self._request(
                "DELETE",
                f"/api/session/{session_id}/form/{form_id}",
                params=params,
            )
        except OpenCodeError as exc:
            # The form may have been answered or cancelled elsewhere.
            if exc.status_code != 404:
                raise

    async def interrupt(self, session_id: str) -> Any:
        return await self._request("POST", f"/api/session/{session_id}/interrupt")

    async def list_models(self) -> list[dict[str, str]]:
        try:
            data = await self._request("GET", "/api/model")
        except OpenCodeError as exc:
            if exc.status_code != 404:
                raise
            data = await self._request("GET", "/api/config")
            return parse_models(data)

        provider_names: dict[str, str] = {}
        try:
            providers = await self._request("GET", "/api/provider")
            provider_names = parse_provider_names(providers)
        except Exception:
            pass
        return parse_model_catalog(data, provider_names)

    async def switch_model(
        self, session_id: str, provider_id: str, model_id: str
    ) -> None:
        await self._request(
            "POST",
            f"/api/session/{session_id}/model",
            json={"model": {"providerID": provider_id, "id": model_id}},
        )

    async def list_agents(
        self, directory: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str] | None = None
        if directory:
            params = {"location[directory]": directory}
        try:
            data = await self._request(
                "GET", "/api/agent", params=params
            )
        except OpenCodeError as exc:
            # A missing workspace directory makes the location lookup fail.
            # Fall back to the server's default location so global agents
            # (build, plan, ...) remain selectable.
            if params and exc.status_code in (400, 404):
                data = await self._request("GET", "/api/agent")
            else:
                raise
        return parse_agents(data)

    async def switch_agent(self, session_id: str, agent: str) -> None:
        await self._request(
            "POST",
            f"/api/session/{session_id}/agent",
            json={"agent": agent},
        )

    async def set_session_permissions(
        self, session_id: str, permissions: list[dict[str, str]]
    ) -> None:
        await self._request(
            "PATCH",
            f"/api/session/{session_id}",
            json={"permissions": permissions},
        )

    async def list_pending_permissions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/permission/request")
        if isinstance(data, dict):
            return data.get("data", [])
        return []


class OpenCodeSyncClient:
    def __init__(self, base_url: str, username: str, password: str):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(username, password),
            timeout=httpx.Timeout(3.0, connect=2.0),
        )

    def close(self) -> None:
        self._client.close()

    def _check(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            try:
                body: Any = response.json()
            except Exception:
                body = response.text[:1000]
            raise OpenCodeError(
                f"OpenCode HTTP {response.status_code}: {body}",
                response.status_code,
                body,
            )

    def reply_permission(
        self, session_id: str, permission_id: str, decision: str
    ) -> None:
        response = self._client.post(
            f"/api/session/{session_id}/permission/{permission_id}/reply",
            json={"decision": decision},
        )
        self._check(response)

    def reply_form(
        self, session_id: str, form_id: str, answer: dict[str, Any]
    ) -> None:
        response = self._client.post(
            f"/api/session/{session_id}/form/{form_id}/reply",
            json={"answer": answer},
        )
        self._check(response)

    def cancel_form(
        self,
        session_id: str,
        form_id: str,
        message: str | None = None,
    ) -> None:
        params = {"message": message} if message else None
        response = self._client.delete(
            f"/api/session/{session_id}/form/{form_id}",
            params=params,
        )
        if response.status_code == 404:
            return
        self._check(response)

    def list_models(self) -> list[dict[str, str]]:
        response = self._client.get("/api/model")
        if response.status_code == 404:
            response = self._client.get("/api/config")
            self._check(response)
            return parse_models(response.json())
        self._check(response)

        provider_names: dict[str, str] = {}
        try:
            provider_response = self._client.get("/api/provider")
            self._check(provider_response)
            provider_names = parse_provider_names(provider_response.json())
        except Exception:
            pass
        return parse_model_catalog(response.json(), provider_names)

    def switch_model(
        self, session_id: str, provider_id: str, model_id: str
    ) -> None:
        response = self._client.post(
            f"/api/session/{session_id}/model",
            json={"model": {"providerID": provider_id, "id": model_id}},
        )
        self._check(response)

    def list_agents(
        self, directory: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str] | None = None
        if directory:
            params = {"location[directory]": directory}
        response = self._client.get("/api/agent", params=params)
        if params and response.status_code in (400, 404):
            response = self._client.get("/api/agent")
        self._check(response)
        return parse_agents(response.json())

    def switch_agent(self, session_id: str, agent: str) -> None:
        response = self._client.post(
            f"/api/session/{session_id}/agent",
            json={"agent": agent},
        )
        self._check(response)

    def set_session_permissions(
        self, session_id: str, permissions: list[dict[str, str]]
    ) -> None:
        response = self._client.patch(
            f"/api/session/{session_id}",
            json={"permissions": permissions},
        )
        self._check(response)
