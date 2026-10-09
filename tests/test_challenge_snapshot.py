"""Снимок непройденной антибот-проверки: что показал Ozon, когда заблокировал."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Page

from price_panel import browser


class StuckChallengePage:
    """Страница, на которой антибот-проверка так и не проходит."""

    url = "https://www.ozon.ru/product/123/"

    def __init__(self):
        self.shots: list = []

    def title(self):
        return "Antibot Challenge Page"

    def inner_text(self, selector, timeout=None):
        return "Доступ ограничен.   Подтвердите, что вы не робот"

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path, full_page=False):
        self.shots.append(path)
        Path(path).write_bytes(b"png")


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(browser.logger, "LOG_DIR", tmp_path)
    monkeypatch.setattr(browser, "_challenge_shots", 0)
    return tmp_path


def test_failed_challenge_leaves_a_snapshot(log_dir):
    page = StuckChallengePage()
    assert browser.pass_challenge(cast(Page, page), timeout=0) is False
    files = list(log_dir.glob("challenge-*.png"))
    assert len(files) == 1 and page.shots == [str(files[0])]


def test_snapshots_are_limited_per_process(log_dir, monkeypatch):
    """При блокировке страницы идут одна за другой - снимков не больше трёх."""
    page = StuckChallengePage()
    for _ in range(5):
        browser.save_challenge_snapshot(cast(Page, page))
    assert len(page.shots) == browser.CHALLENGE_SHOTS_PER_PROCESS


def test_old_snapshots_are_pruned(log_dir, monkeypatch):
    monkeypatch.setattr(browser, "CHALLENGE_SHOTS_KEEP", 2)
    for name in ("challenge-20261001-100000.png", "challenge-20261001-110000.png",
                 "challenge-20261001-120000.png"):
        (log_dir / name).write_bytes(b"old")
    browser.save_challenge_snapshot(cast(Page, StuckChallengePage()))
    remaining = sorted(p.name for p in log_dir.glob("challenge-*.png"))
    assert len(remaining) == 2
    assert "challenge-20261001-100000.png" not in remaining
