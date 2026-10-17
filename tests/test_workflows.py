"""Scheduling rules from CLAUDE.md, checked against the workflow files."""

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name):
    data = yaml.safe_load((WF / name).read_text())
    # PyYAML reads the bare key `on:` as True.
    return data, data.get("on", data.get(True))


def test_only_the_morning_job_and_heartbeat_are_scheduled():
    scheduled = {p.name for p in WF.glob("*.yml") if "schedule" in (load(p.name)[1] or {})}
    assert scheduled == {"morning.yml", "heartbeat.yml"}


def test_pull_odds_stays_manual_only():
    _, on = load("pull_odds.yml")
    assert set(on) == {"workflow_dispatch"}


def test_morning_job_cannot_spend_odds_api_credits():
    text = (WF / "morning.yml").read_text()
    assert "ODDS_API_KEY" not in text.replace("ODDS_API_KEY is not passed", "")
    _, on = load("morning.yml")
    assert on["schedule"] == [{"cron": "17 12 * * *"}]   # ~8 am ET (7 am in winter)
    assert "workflow_dispatch" in on


def test_heartbeat_is_weekly_and_only_touches_its_file():
    data, on = load("heartbeat.yml")
    minute, hour, dom, month, dow = on["schedule"][0]["cron"].split()
    assert (dom, month) == ("*", "*") and dow.isdigit()
    steps = data["jobs"]["beat"]["steps"]
    script = next(s["run"] for s in steps if "run" in s)
    assert "git add .github/heartbeat.txt" in script and "git add -A" not in script
