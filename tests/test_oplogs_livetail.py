"""Live-tail lifecycle — deterministic stale-response regression.

Uses Playwright request interception to hold/release tail responses so a
deliberately-stale poll cannot be conflated with the current lifecycle.
Requires a running preview on 127.0.0.1:39995 (skipped otherwise)."""
import pytest
playwright = pytest.importorskip(
    "playwright.sync_api", reason="live-tail browser check")

URL = "http://127.0.0.1:39995"
USER, PW = "Admin", "Review-Preview-#2026!"


def _login(pg):
    pg.goto(f"{URL}/login")
    pg.fill("input[name=username]", USER)
    pg.fill("input[name=password]", PW)
    pg.click("button[type=submit],input[type=submit]")
    pg.wait_for_timeout(1200)


def test_stale_response_dropped_on_pause():
    from playwright.sync_api import sync_playwright
    held = []
    with sync_playwright() as p:
        pg = p.chromium.launch(headless=True).new_page()
        pg.route("**/system/logs/tail*", lambda r: held.append(r))
        _login(pg)
        pg.goto(f"{URL}/system/logs"); pg.wait_for_timeout(1500)
        pg.locator("#live-tail").check(); pg.wait_for_timeout(1200)
        pg.locator("#live-tail").uncheck(); pg.wait_for_timeout(300)
        stale = ('{"generated_at":"2026-11-09T10:30:00Z","rows":[{"ts":'
                 '"2026-11-09T10:30:00Z","severity":"error","component":"stale"'
                 ',"category":"t","message":"STALE-NO-APPEAR","project":"",'
                 '"execution":"","worker":"","corr":""}]}')
        for r in held:
            r.fulfill(status=200, content_type="application/json", body=stale)
        pg.wait_for_timeout(600)
        body = pg.locator("#log-rows").inner_text()
        assert "STALE-NO-APPEAR" not in body
        assert "paused" in pg.locator("#tail-status").text_content()
        pg.close()
