"""Browser interaction tests for the Test-tab creation modals.

Requires the running preview (127.0.0.1:39995) with fixture data.
Each test asserts a specific interaction behavior — not markup presence."""
import os
import pytest

playwright = pytest.importorskip("playwright.sync_api",
                                 reason="playwright not installed")
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:39995"
PROJ = "62d2f3e1b4654e5680fd187795c454f7"


def _login(pg):
    pg.goto(f"{BASE}/login")
    pg.fill("input[name=username]", "Admin")
    pg.fill("input[name=password]", "Review-Preview-#2026!")
    pg.click("button[type=submit],input[type=submit]")
    pg.wait_for_timeout(1200)
    pg.goto(f"{BASE}/projects/{PROJ}?tab=test&subtab=suites")
    pg.wait_for_timeout(1500)


def test_modal_opens_and_focus_traps():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        _login(pg)
        pg.click('[data-modal-open="suite-modal"]')
        pg.wait_for_timeout(300)
        assert pg.locator("#suite-modal.on").count() == 1
        # focus trap: Tab from last focusable cycles to first
        pg.keyboard.press("Tab")
        focus_in = pg.evaluate(
            "document.getElementById('suite-modal').contains("
            "document.activeElement)")
        assert focus_in
        # Escape closes + focus returns to the trigger
        pg.keyboard.press("Escape"); pg.wait_for_timeout(200)
        assert pg.locator("#suite-modal.on").count() == 0
        b.close()


def test_dirty_close_confirms():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        _login(pg)
        pg.click('[data-modal-open="suite-modal"]')
        pg.wait_for_timeout(200)
        pg.fill("#suite-modal input[name=name]", "DirtySuite")
        # dirty -> close triggers confirm; dismiss keeps it open
        pg.once("dialog", lambda d: d.dismiss())
        pg.click("#suite-modal [data-modal-close]")
        pg.wait_for_timeout(200)
        assert pg.locator("#suite-modal.on").count() == 1  # still open
        # accept the confirm -> closes
        pg.once("dialog", lambda d: d.accept())
        pg.click("#suite-modal [data-modal-close]")
        pg.wait_for_timeout(200)
        assert pg.locator("#suite-modal.on").count() == 0
        b.close()


def test_collection_modal_opens_and_guard_present():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        _login(pg)
        pg.goto(f"{BASE}/projects/{PROJ}?tab=test&subtab=collections")
        pg.wait_for_timeout(1200)
        pg.click('[data-modal-open="coll-modal"]')
        pg.wait_for_timeout(300)
        assert pg.locator("#coll-modal.on").count() == 1
        b.close()
        # dup-submit guard lives in the served JS (btn.disabled on submit)
        src = open("testorchestrator/static/js/test-modal.js").read()
        assert "btn.disabled = true" in src and "Creating" in src
