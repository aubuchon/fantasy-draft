from datetime import date

from fantasy_draft.players import age_on, experience_label


def test_age_uses_birthday_not_only_calendar_year():
    as_of = date(2026, 8, 25)
    assert age_on(date(2000, 8, 25), as_of) == 26
    assert age_on(date(2000, 8, 26), as_of) == 25
    assert age_on(None, as_of) is None


def test_experience_is_rookie_then_completed_league_years():
    assert experience_label(2026, 2026) == "R"
    assert experience_label(2025, 2026) == "1"
    assert experience_label(2018, 2026) == "8"
    assert experience_label(None, 2026) == "—"
    assert experience_label(2027, 2026) == "—"
