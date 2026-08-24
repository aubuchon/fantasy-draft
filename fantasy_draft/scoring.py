from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fantasy_draft.config import LeagueConfig


@dataclass(frozen=True)
class ScoringResult:
    points: float
    approximations: tuple[str, ...]


def _n(stats: dict[str, Any], key: str) -> float:
    try:
        return float(stats.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def score_projection(stats: dict[str, Any], position: str, config: LeagueConfig) -> ScoringResult:
    scoring = config.scoring
    notes: list[str] = []
    points = 0.0
    passing = scoring.get("passing", {})
    rushing = scoring.get("rushing", {})
    receiving = scoring.get("receiving", {})
    misc = scoring.get("misc", {})
    if passing.get("yards_per_point"):
        points += _n(stats, "pass_yds") / float(passing["yards_per_point"])
    points += _n(stats, "pass_tds") * float(passing.get("touchdowns", 0))
    points += _n(stats, "pass_ints") * float(passing.get("interceptions", 0))
    for bonus in passing.get("bonuses", []):
        points += _n(stats, f"pass_yds_{bonus['threshold_yards']}") * float(bonus["points"])
    if rushing.get("yards_per_point"):
        points += _n(stats, "rush_yds") / float(rushing["yards_per_point"])
    points += _n(stats, "rush_tds") * float(rushing.get("touchdowns", 0))
    for bonus in rushing.get("bonuses", []):
        points += _n(stats, f"rush_yds_{bonus['threshold_yards']}") * float(bonus["points"])
    points += _n(stats, "rec_rec") * float(receiving.get("receptions", 0))
    if receiving.get("yards_per_point"):
        points += _n(stats, "rec_yds") / float(receiving["yards_per_point"])
    points += _n(stats, "rec_tds") * float(receiving.get("touchdowns", 0))
    for bonus in receiving.get("bonuses", []):
        points += _n(stats, f"rec_yds_{bonus['threshold_yards']}") * float(bonus["points"])
    points += _n(stats, "ret_tds") * float(misc.get("return_touchdowns", 0))
    points += _n(stats, "2pt_tds") * float(misc.get("two_point_conversions", 0))
    if _n(stats, "fumbles"):
        notes.append("Provider fumbles are not explicitly fumbles lost; excluded from league score.")

    if position == "K":
        kicking = scoring.get("kicking", {})
        buckets = kicking.get("field_goals", {})
        if buckets and _n(stats, "fg"):
            points += _n(stats, "fg") * min(float(value) for value in buckets.values())
            notes.append("Kicker projections lack distance buckets; field goals use the lowest configured value.")
        points += _n(stats, "xpt") * float(kicking.get("extra_point_made", 0))
    if position in {"DEF", "DST"}:
        defense = scoring.get("defense", {})
        points += _n(stats, "def_sack") * float(defense.get("sack", 0))
        points += _n(stats, "def_int") * float(defense.get("interception", 0))
        points += _n(stats, "def_fr") * float(defense.get("fumble_recovery", 0))
        points += _n(stats, "def_td") * float(defense.get("touchdown", 0))
        points += _n(stats, "def_safety") * float(defense.get("safety", 0))
        points += _n(stats, "def_retd") * float(defense.get("return_touchdown", 0))
        allowed = defense.get("points_allowed", {})
        for field, bucket in zip(
            ("def_pa_a", "def_pa_b", "def_pa_c", "def_pa_d", "def_pa_e", "def_pa_f", "def_pa_g"),
            ("0", "1-6", "7-13", "14-20", "21-27", "28-34", "35+"),
        ):
            points += _n(stats, field) * float(allowed.get(bucket, 0))
    return ScoringResult(round(points, 2), tuple(notes))
