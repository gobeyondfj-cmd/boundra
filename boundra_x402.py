# -*- coding: utf-8 -*-
"""Production-friendly async client and fail-closed x402 pre-sign hook.

The hook only returns ``None`` (allow signing) after an explicit ALLOW. Network
errors, malformed responses, DENY and unresolved ASK all abort payment creation.
ALLOW reservations must later be settled or released with the returned token.
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

import httpx

from rail_adapters import X402Adapter

ApprovalHandler = Callable[[dict], str | Awaitable[str | None] | None]


class BoundraError(RuntimeError):
    """The hosted decision service failed or returned an invalid response."""


class BoundraClient:
    def __init__(self, base_url: str = "https://api.boundra.io", *,
                 owner: str | None = None, agent_id: str | None = None,
                 timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.owner = owner
        self.agent_id = agent_id
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.aclose()

    async def aclose(self):
        await self._http.aclose()

    async def _post(self, path: str, body: dict, *, semantic: bool = False) -> dict:
        headers = {"Content-Type": "application/json"}
        if semantic and self.agent_id:
            headers["X-Boundra-Agent"] = self.agent_id
        try:
            response = await self._http.post(
                f"{self.base_url}{path}", json=body, headers=headers)
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BoundraError(f"Boundra request failed: {exc}") from exc
        if not isinstance(result, dict):
            raise BoundraError("Boundra returned a non-object response")
        return result

    async def evaluate(self, intent: dict, *, semantic: bool = False,
                       approval_token: str | None = None,
                       mandate: dict | None = None,
                       purchase_id: str | None = None) -> dict:
        body: dict[str, Any] = {"intent": intent}
        if self.owner:
            body["owner"] = self.owner
        if self.agent_id:
            body["agent_id"] = self.agent_id
        if approval_token:
            body["approval_token"] = approval_token
        if mandate:
            body["mandate"] = mandate
        if purchase_id is not None:
            body["purchase_id"] = purchase_id
        verdict = await self._post(
            "/evaluate/semantic" if semantic else "/evaluate/basic",
            body, semantic=semantic)
        if verdict.get("decision") not in ("ALLOW", "ASK", "DENY", "INDETERMINATE"):
            raise BoundraError("Boundra response has no valid decision")
        return verdict

    async def settle(self, evaluation_id: str, reservation_token: str,
                     settlement_ref: str) -> dict:
        return await self._post(f"/evaluations/{evaluation_id}/settle", {
            "reservation_token": reservation_token,
            "settlement_ref": settlement_ref,
        })

    async def release(self, evaluation_id: str, reservation_token: str) -> dict:
        return await self._post(f"/evaluations/{evaluation_id}/release", {
            "reservation_token": reservation_token,
        })


def create_x402_hook(client: BoundraClient, *, purpose: str,
                     expected_amount: float | None = None,
                     expected_recipient: str | None = None,
                     semantic: bool = False,
                     approval_handler: ApprovalHandler | None = None,
                     purchase_id: str | None = None):
    """Build an x402 ``on_before_payment_creation`` hook.

    ``approval_handler`` receives the ASK verdict and returns a wallet-signed
    approval token (or None to abort). It may be synchronous or asynchronous.
    ``purchase_id`` must identify one logical purchase, not a quote fingerprint.
    """
    adapter = X402Adapter()

    async def before_payment_creation(payment_ctx):
        try:
            rail = adapter.extract(
                payment_ctx, purpose, expected_amount, expected_recipient)
            verdict = await client.evaluate(
                rail.to_intent().__dict__, semantic=semantic, purchase_id=purchase_id)
            if verdict.get("decision") == "ASK" and approval_handler:
                token = approval_handler(verdict)
                if inspect.isawaitable(token):
                    token = await token
                if token:
                    verdict = await client.evaluate(
                        rail.to_intent().__dict__, semantic=semantic,
                        approval_token=token, purchase_id=purchase_id)
            if verdict.get("decision") == "ALLOW":
                return None
            return adapter.abort(verdict)
        except Exception as exc:
            # Fail closed: an unavailable or malformed guardrail must never
            # silently turn into a signed payment.
            return adapter.abort({
                "layer": "availability",
                "reasons": [f"Boundra pre-sign check failed closed: {exc}"],
            })

    return before_payment_creation
