"""Browser creation, persistence, overlap errors and expiry of EV presence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, expect

from e2e.conftest import _configuration, running_service
from test_ev_planning import EV_SETTINGS

pytestmark = pytest.mark.e2e


def test_presence_overrides_can_be_added_removed_and_expire(
    page: Page, tmp_path: Path
) -> None:
    configuration = _configuration(tmp_path / "presence")
    configuration["home_assistant"]["ev"] = EV_SETTINGS
    first = (datetime.now(UTC) + timedelta(days=2)).replace(
        minute=0, second=0, microsecond=0
    )
    zone = ZoneInfo("Europe/Berlin")
    with running_service(configuration, tmp_path) as url:
        page.goto(url + "/dashboard/")
        page.locator("#plan-tab").click()
        add = page.get_by_role("button", name="Add EV presence override")
        expect(add).to_be_enabled()
        for index, present in enumerate(("true", "false")):
            add.click()
            start = first + timedelta(hours=2 * index)
            end = start + timedelta(hours=2)
            page.locator("#ev-presence-start").fill(
                start.astimezone(zone).strftime("%Y-%m-%dT%H:%M")
            )
            page.locator("#ev-presence-end").fill(
                end.astimezone(zone).strftime("%Y-%m-%dT%H:%M")
            )
            page.locator("#ev-presence-value").select_option(present)
            page.get_by_role("button", name="Save presence override").click()
            expect(page.locator("#ev-presence-status")).to_contain_text("Saved")
            expect(page.locator("#ev-presence-list li")).to_have_count(index + 1)
        saved = page.request.get(url + "/api/v1/ev/presence").json()
        assert datetime.fromisoformat(saved["overrides"][0]["start_time"]) == first
        page.reload()
        page.locator("#plan-tab").click()
        rows = page.locator("#ev-presence-list li")
        expect(rows).to_have_count(2)
        expect(rows.nth(0)).to_contain_text("Present")
        expect(rows.nth(1)).to_contain_text("Absent")
        add.click()
        page.locator("#ev-presence-start").fill(
            first.astimezone(zone).strftime("%Y-%m-%dT%H:%M")
        )
        page.locator("#ev-presence-end").fill(
            (first + timedelta(hours=1)).astimezone(zone).strftime("%Y-%m-%dT%H:%M")
        )
        page.get_by_role("button", name="Save presence override").click()
        expect(page.locator("#ev-presence-status")).to_contain_text("overlapping")
        expect(rows).to_have_count(2)
        for start_text, end_text in (
            ("2027-03-28T02:00", "2027-03-28T04:00"),
            ("2027-10-31T02:00", "2027-10-31T04:00"),
        ):
            page.locator("#ev-presence-start").fill(start_text)
            page.locator("#ev-presence-end").fill(end_text)
            page.get_by_role("button", name="Save presence override").click()
            expect(page.locator("#ev-presence-status")).to_contain_text("unambiguous")
        page.get_by_role("button", name="Cancel", exact=True).click()
        rows.nth(1).get_by_role(
            "button", name="Remove presence override", exact=False
        ).click()
        expect(rows).to_have_count(1)
        page.clock.install(time=first + timedelta(hours=2, minutes=1))
        page.clock.run_for(1100)
        expect(rows).to_have_count(0)
