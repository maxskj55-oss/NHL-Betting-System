"""Shared test helpers: fixtures, a temp-dir config, and a fake HTTP session."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from nhl_betting.config import Config, load_config

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def temp_config(tmp: Path) -> Config:
    """Real config.yaml, but output goes to a temp dir."""
    cfg = load_config()
    return replace(cfg, csv_path=tmp / "odds_snapshots.csv", raw_dir=tmp / "raw")


class FakeResponse:
    def __init__(self, status: int, body, headers: dict):
        self.status_code = status
        self._body = body
        self.headers = headers
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """Returns canned responses by endpoint and records every call."""

    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(f"Unexpected URL {url}")

    def paths(self) -> list[str]:
        return [url.rsplit("/", 1)[-1] for url, _ in self.calls]


def usage_headers(used: int, remaining: int, last: int) -> dict:
    return {
        "x-requests-used": str(used),
        "x-requests-remaining": str(remaining),
        "x-requests-last": str(last),
    }
