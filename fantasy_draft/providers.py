from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class ProviderError(RuntimeError):
    pass


class ProviderNotConfigured(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderPayload:
    provider: str
    dataset: str
    records: list[dict[str, Any]]
    raw: dict[str, Any]
    metadata: dict[str, Any]


class FantasyDataProvider(Protocol):
    def get_players(self) -> ProviderPayload: ...
    def get_rankings(self, season: int, scoring: str, ranking_type: str) -> ProviderPayload: ...
    def get_projections(self, season: int, positions: list[str]) -> ProviderPayload: ...


class FantasyProsProvider:
    """Official FantasyPros Public API v2 adapter; no draft rules live here."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str,
        timeout_seconds: float = 10,
        client: httpx.Client | None = None,
    ):
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._api_key:
            raise ProviderNotConfigured("FANTASYPROS_API_KEY is not configured")
        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        close = self._client is None
        try:
            response = client.get(
                f"{self.base_url}/{path.lstrip('/')}",
                params=params,
                headers={"x-api-key": self._api_key, "accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ProviderError("FantasyPros returned an unexpected response shape")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"FantasyPros request failed ({type(exc).__name__})") from exc
        finally:
            if close:
                client.close()

    def get_players(self) -> ProviderPayload:
        raw = self._get(
            "nfl/players",
            {"external_ids": "yahoo:espn:cbs:nfl", "ecr": "included", "show": "pos_rank"},
        )
        return ProviderPayload("fantasypros", "players", list(raw.get("players") or []), raw, {})

    def get_rankings(self, season: int, scoring: str, ranking_type: str) -> ProviderPayload:
        raw = self._get(
            f"nfl/{season}/consensus-rankings",
            {
                "position": "ALL", "type": ranking_type.upper(), "scoring": scoring,
                "week": 0, "range": "true", "rankstats": "true",
            },
        )
        return ProviderPayload(
            "fantasypros",
            "adp" if ranking_type.upper() == "ADP" else "rankings",
            list(raw.get("players") or []),
            raw,
            {"season": season, "scoring": scoring, "ranking_type": ranking_type.upper()},
        )

    def get_projections(self, season: int, positions: list[str]) -> ProviderPayload:
        records: list[dict[str, Any]] = []
        responses: dict[str, Any] = {}
        for position in positions:
            api_position = "DST" if position == "DEF" else position
            raw = self._get(
                f"nfl/{season}/projections", {"position": api_position, "week": 0}
            )
            responses[api_position] = raw
            records.extend(raw.get("players") or [])
        return ProviderPayload(
            "fantasypros", "projections", records,
            {"season": season, "responses": responses},
            {
                "season": season, "week": 0, "positions": positions,
                "projection_type": "preseason",
            },
        )


class DynastyProcessProvider:
    def __init__(self, url: str, timeout_seconds: float = 15, client: httpx.Client | None = None):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._client = client

    def fetch_csv(self) -> str:
        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        close = self._client is None
        try:
            response = client.get(self.url, headers={"accept": "text/csv"})
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            raise ProviderError(f"DynastyProcess request failed ({type(exc).__name__})") from exc
        finally:
            if close:
                client.close()
