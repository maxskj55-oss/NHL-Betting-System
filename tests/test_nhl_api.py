"""NHL web API: saved real responses must still parse; any shape change must fail loudly."""

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from nhl_betting.games import parse_games, teams_that_played
from nhl_betting.nhl_api import NhlApiClient, NhlApiError, SchemaError, validate_score
from tests.helpers import FakeResponse, FakeSession

NHL_FIXTURES = Path(__file__).parent / "fixtures" / "nhl"
ALL = sorted(NHL_FIXTURES.glob("score_*.json"))


def load(day: str) -> dict:
    return json.loads((NHL_FIXTURES / f"score_{day}.json").read_text())


def first_final(payload: dict) -> dict:
    return next(g for g in payload["games"] if g["gameState"] in ("OFF", "FINAL"))


def test_fixture_set_covers_what_settlement_needs():
    ends = [g["gameOutcome"]["lastPeriodType"] for f in ALL
            for g in json.loads(f.read_text())["games"] if "gameOutcome" in g]
    assert {"REG", "OT", "SO"} <= set(ends), "fixtures need a regulation, an OT and a shootout game"
    assert any(g["gameState"] == "FUT" for g in load("2026-10-17")["games"])


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_real_fixtures_validate_and_parse(path):
    day = path.stem.split("_", 1)[1]
    payload = json.loads(path.read_text())
    validate_score(payload, day)
    games = parse_games(payload, day)
    assert games, "every saved date has games"
    for g in games:
        assert g.game_date == date.fromisoformat(day)
        assert len(g.home) == 3 and len(g.away) == 3
        if g.is_final:
            assert isinstance(g.home_score, int) and isinstance(g.away_score, int)
            assert g.last_period in ("REG", "OT", "SO")


def _mutations():
    def drop(path):
        def f(p):
            obj = first_final(p)
            for k in path[:-1]:
                obj = obj[k]
            del obj[path[-1]]
        return f

    def set_(path, value):
        def f(p):
            obj = first_final(p)
            for k in path[:-1]:
                obj = obj[k]
            obj[path[-1]] = value
        return f

    return {
        "games missing": lambda p: p.pop("games"),
        "games not a list": lambda p: p.update(games={}),
        "id missing": drop(["id"]),
        "id is text": set_(["id"], "2026020106"),
        "startTimeUTC missing": drop(["startTimeUTC"]),
        "gameState missing": drop(["gameState"]),
        "unknown gameState": set_(["gameState"], "DONE"),
        "homeTeam.abbrev missing": drop(["homeTeam", "abbrev"]),
        "awayTeam renamed": lambda p: first_final(p).update(visitorTeam=first_final(p).pop("awayTeam")),
        "final score missing": drop(["homeTeam", "score"]),
        "final score is text": set_(["awayTeam", "score"], "3"),
        "gameOutcome missing": drop(["gameOutcome"]),
        "lastPeriodType missing": drop(["gameOutcome", "lastPeriodType"]),
        "unknown lastPeriodType": set_(["gameOutcome", "lastPeriodType"], "SHOOTOUT"),
        "wrong date": lambda p: p.update(currentDate="2026-10-13"),
    }


@pytest.mark.parametrize("name", list(_mutations()))
def test_shape_changes_fail_loudly(name):
    payload = copy.deepcopy(load("2026-10-14"))
    _mutations()[name](payload)
    with pytest.raises(SchemaError):
        parse_games(payload, "2026-10-14")


def test_preseason_and_allstar_games_are_dropped():
    payload = copy.deepcopy(load("2026-10-16"))
    payload["games"][0]["gameType"] = 1
    payload["games"][1]["gameType"] = 4
    assert parse_games(payload, "2026-10-16") == []


def test_future_games_have_no_scores():
    games = parse_games(load("2026-10-17"), "2026-10-17")
    assert len(games) == 14
    assert all(not g.is_final and g.home_score is None for g in games)


def test_postponed_games_dont_count_as_played():
    games = parse_games(load("2026-10-16"), "2026-10-16")
    assert teams_that_played(games) == {"PIT", "COL", "BOS", "ANA"}
    payload = copy.deepcopy(load("2026-10-16"))
    payload["games"][0]["gameScheduleState"] = "PPD"
    assert teams_that_played(parse_games(payload, "2026-10-16")) == {"BOS", "ANA"}


def test_client_fetches_score_and_validates():
    session = FakeSession({"/score/2026-10-16": FakeResponse(200, load("2026-10-16"), {})})
    client = NhlApiClient("https://api-web.nhle.com/v1", session=session, backoff_seconds=0)
    assert len(client.get_score(date(2026, 10, 16))["games"]) == 2
    assert session.calls[0][0] == "https://api-web.nhle.com/v1/score/2026-10-16"


def test_client_rejects_a_changed_shape():
    session = FakeSession({"/score/2026-10-16": FakeResponse(200, {"gameWeek": []}, {})})
    client = NhlApiClient("https://x/v1", session=session, backoff_seconds=0)
    with pytest.raises(SchemaError):
        client.get_score(date(2026, 10, 16))


def test_client_gives_up_after_retries_and_stops_on_404():
    class Flaky(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, {}))
            return FakeResponse(503, {}, {})

    s = Flaky({})
    with pytest.raises(NhlApiError, match="3 attempts"):
        NhlApiClient("https://x/v1", session=s, backoff_seconds=0).get_score(date(2026, 10, 16))
    assert len(s.calls) == 3
    s404 = FakeSession({"/score/2026-10-16": FakeResponse(404, {}, {})})
    with pytest.raises(NhlApiError, match="404"):
        NhlApiClient("https://x/v1", session=s404, backoff_seconds=0).get_score(date(2026, 10, 16))
    assert len(s404.calls) == 1
