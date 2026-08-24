from __future__ import annotations

from pathlib import Path
import copy
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeagueDetails(StrictModel):
    external_id: str | None = None
    name: str
    platform: str | None = None
    team_count: int = Field(ge=2, le=32)
    scoring_type: str
    start_scoring_week: int = Field(default=1, ge=1, le=18)
    public: bool = False
    auto_renew: bool = False


class DraftSettings(StrictModel):
    type: Literal["snake"] = "snake"
    entry_mode: Literal["manual_offline"] = "manual_offline"
    rounds: int = Field(ge=1, le=40)
    our_team_id: str
    allow_pick_trades: bool = False


class TeamConfig(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    draft_slot: int = Field(ge=1)


class RosterSlot(StrictModel):
    code: str = Field(min_length=1)
    label: str = Field(min_length=1)
    count: int = Field(ge=1)
    eligible_positions: list[str] = Field(min_length=1)
    draftable: bool = True
    starter: bool = True


class RosterSettings(StrictModel):
    slots: list[RosterSlot] = Field(min_length=1)

    @property
    def draftable_size(self) -> int:
        return sum(slot.count for slot in self.slots if slot.draftable)


class LeagueConfig(StrictModel):
    schema_version: int = 1
    league: LeagueDetails
    draft: DraftSettings
    teams: list[TeamConfig]
    roster: RosterSettings
    scoring: dict[str, Any]
    transactions: dict[str, Any] = Field(default_factory=dict)
    playoffs: dict[str, Any] = Field(default_factory=dict)
    rules: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_consistency(self) -> "LeagueConfig":
        if len(self.teams) != self.league.team_count:
            raise ValueError("teams must contain exactly league.team_count entries")
        team_ids = [team.id for team in self.teams]
        if len(team_ids) != len(set(team_ids)):
            raise ValueError("team ids must be unique")
        slots = sorted(team.draft_slot for team in self.teams)
        if slots != list(range(1, self.league.team_count + 1)):
            raise ValueError("team draft_slot values must be contiguous from 1")
        if self.draft.our_team_id not in team_ids:
            raise ValueError("draft.our_team_id must reference a configured team")
        if self.draft.rounds > self.roster.draftable_size:
            raise ValueError("draft rounds cannot exceed draftable roster capacity")
        codes = [slot.code for slot in self.roster.slots]
        if len(codes) != len(set(codes)):
            raise ValueError("roster slot codes must be unique")
        if any(slot.starter and not slot.draftable for slot in self.roster.slots):
            raise ValueError("non-draftable roster slots cannot be starting slots")
        return self

    def team_by_id(self, team_id: str) -> TeamConfig:
        return next(team for team in self.teams if team.id == team_id)

    def team_by_slot(self, slot: int) -> TeamConfig:
        return next(team for team in self.teams if team.draft_slot == slot)


def load_league_config(path: str | Path) -> LeagueConfig:
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return LeagueConfig.model_validate(raw)


def dump_league_config(config: LeagueConfig) -> str:
    """Serialize a validated, immutable-per-draft configuration snapshot."""
    return yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)


def load_league_config_text(text: str) -> LeagueConfig:
    return LeagueConfig.model_validate(yaml.safe_load(text))


def configure_draft_session(
    master: LeagueConfig, *, team_count: int, our_draft_slot: int
) -> LeagueConfig:
    """Create a validated per-session config without mutating the master config."""
    if not 1 <= our_draft_slot <= team_count:
        raise ValueError("our draft slot must be within the configured team count")
    raw = copy.deepcopy(master.model_dump())
    raw["league"]["team_count"] = team_count
    if team_count != len(master.teams):
        raw["teams"] = [
            {"id": f"team-{slot}", "name": f"Team {slot}", "draft_slot": slot}
            for slot in range(1, team_count + 1)
        ]
    raw["draft"]["our_team_id"] = next(
        team["id"] for team in raw["teams"] if team["draft_slot"] == our_draft_slot
    )
    return LeagueConfig.model_validate(raw)
