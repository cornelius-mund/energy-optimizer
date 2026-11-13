"""Browser CRUD, timezone interpretation and automatic expiry of EV targets."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, expect

from e2e.conftest import _configuration, running_service
from test_ev_planning import EV_SETTINGS

pytestmark = pytest.mark.e2e


def test_add_multiple_requirements_reload_remove_and_hide_expired(
    page: Page, tmp_path: Path
) -> None:
    configuration = _configuration(tmp_path / "targets")
    configuration["home_assistant"]["ev"] = EV_SETTINGS
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    first = now + timedelta(days=2)
    with running_service(configuration, tmp_path) as url:
        page.goto(url + "/dashboard/")
        page.locator("#plan-tab").click()
        add = page.get_by_role("button", name="Add EV charge requirement")
        expect(add).to_be_enabled()
        for days, percent in ((2, 70), (3, 80)):
            add.click()
            deadline = (now + timedelta(days=days)).astimezone(
                ZoneInfo("Europe/Berlin")
            )
            page.locator("#ev-target-deadline").fill(
                deadline.strftime("%Y-%m-%dT%H:%M")
            )
            page.locator("#ev-target-percent").fill(str(percent))
            page.get_by_role("button", name="Save requirement").click()
            expect(page.locator("#ev-target-status")).to_contain_text("Saved")
            expect(page.locator("#ev-target-list li")).to_have_count(days - 1)
        page.reload()
        page.locator("#plan-tab").click()
        rows = page.locator("#ev-target-list li")
        expect(rows).to_have_count(2)
        expect(rows.first).to_contain_text("70%")
        rows.nth(1).get_by_role(
            "button", name="Remove requirement", exact=False
        ).click()
        expect(rows).to_have_count(1)
        add.click()
        for ambiguous_time in ("2027-03-28T02:30", "2027-10-31T02:30"):
            page.locator("#ev-target-deadline").fill(ambiguous_time)
            page.get_by_role("button", name="Save requirement").click()
            expect(page.locator("#ev-target-status")).to_contain_text("unambiguous")
            expect(rows).to_have_count(1)
        page.get_by_role("button", name="Cancel", exact=True).click()
        # Advance only the browser clock; the open page must hide elapsed goals
        # without needing a reload or a response from the service.
        page.clock.install(time=first + timedelta(minutes=1))
        page.clock.run_for(1100)
        expect(rows).to_have_count(0)
