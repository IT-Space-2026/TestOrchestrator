"""Real modal submit/failure/recovery behavior via intercepted responses."""
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:39995"
PROJ = "62d2f3e1b4654e5680fd187795c454f7"


def _login(pg):
    pg.goto(f"{BASE}/login")
    pg.fill("input[name=username]", "Admin")
    pg.fill("input[name=password]", "Review-Preview-#2026!")
    pg.click("button[type=submit],input[type=submit]")
    pg.wait_for_timeout(1200)


def _open(pg, modal, subtab):
    pg.goto(f"{BASE}/projects/{PROJ}?tab=test&subtab={subtab}")
    pg.wait_for_timeout(1200)
    pg.click(f'[data-modal-open="{modal}"]')
    pg.wait_for_timeout(300)


def test_held_pending_blocks_second_submit():
    """A genuinely-held pending POST: a second submit fires no second
    request (pending guard drops it)."""
    calls = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        _login(pg)
        # record but NEVER fulfill -> the request stays pending
        pg.route("**/author-new", lambda r: calls.append(r.request.url))
        _open(pg, "suite-modal", "suites")
        pg.fill('#suite-modal input[name=name]', 'S')
        res = pg.evaluate(
            "(function(){var f=document.querySelector('#suite-modal "
            "form[data-modal-form]');var b=document.querySelector("
            "'#suite-modal [data-modal-submit]');"
            "f.dispatchEvent(new Event('submit',{bubbles:true,"
            "cancelable:true}));var d1=b.disabled;"
            "f.dispatchEvent(new Event('submit',{bubbles:true,"
            "cancelable:true}));return {d1:d1,d2:b.disabled};})()")
        pg.wait_for_timeout(500)
        assert res["d1"] is True and res["d2"] is True   # held disabled
        assert len(calls) == 1    # exactly one in-flight request
        b.close()


def test_failure_restores_button_and_values():
    """Server error -> values kept, button re-enabled, error shown."""
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        _login(pg)
        pg.route("**/author-new",
                 lambda r: r.fulfill(
                     status=500, body="<html><div class='flash error'>"
                     "Server failed</div></html>"))
        _open(pg, "suite-modal", "suites")
        pg.fill('#suite-modal input[name=name]', 'KeepMe')
        pg.evaluate("document.querySelector('#suite-modal "
                    "form[data-modal-form]').requestSubmit()")
        pg.wait_for_timeout(800)
        assert pg.locator("#suite-modal.on").count() == 1
        assert "failed" in (pg.locator(
            "#suite-modal .modal-error").text_content() or "").lower()
        assert not pg.locator(
            '#suite-modal [data-modal-submit]').is_disabled()
        assert pg.locator(
            '#suite-modal input[name=name]').input_value() == "KeepMe"
        b.close()
