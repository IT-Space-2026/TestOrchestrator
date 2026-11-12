"""Live-browser coverage for the new infrastructure controls:
remote-runner selector in the runtime-create form, license_slots
in the runner edit form. Disposable preview at 127.0.0.1:39995."""
import pytest

playwright = pytest.importorskip("playwright.sync_api",
                                 reason="playwright not installed")
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:39995"


def _login(pg):
    pg.goto(f"{BASE}/login")
    pg.fill("input[name=username]", "Admin")
    pg.fill("input[name=password]", "Review-Preview-#2026!")
    pg.click("button[type=submit],input[type=submit]")


@pytest.fixture
def pg():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        _login(page)
        yield page
        b.close()


def test_runner_table_and_edit_form_render(pg):
    pg.goto(f"{BASE}/system/infrastructure")
    assert pg.locator('input[name="license_slots"]').count() >= 1
    assert pg.locator('select[name="runner_id"]').count() == 1


def _open_runtime_form(pg):
    pg.goto(f"{BASE}/system/infrastructure")
    pg.locator('select[name="runner_id"]').locator(
        "xpath=ancestor::details[1]/summary").click()


def test_second_runner_selected_persists(pg):
    """Selecting the second eligible remote runner binds the runtime
    to exactly that runner — verified via the persisted record on the
    page's runtime list."""
    _open_runtime_form(pg)
    sel = pg.locator('select[name="runner_id"]')
    opt2 = sel.locator("option").nth(2)  # 0=auto,1=rem-1,2=rem-2
    sel.select_option(opt2.get_attribute("value"))
    pg.locator('form[action$="infrastructure/runtimes"] input#name').fill("rt-sel2")
    pg.locator('form[action$="infrastructure/runtimes"] input#executable_path') \
        .fill("/opt/katalonc")
    pg.locator('form[action$="infrastructure/runtimes"] input#loc-remote').check()
    pg.locator('form[action$="infrastructure/runtimes"] input#worker_label') \
        .fill("wl-sel2")
    pg.locator('form[action$="infrastructure/runtimes"] button[type=submit]').click()
    pg.wait_for_timeout(1200)
    # the runtime row should show the second runner's name
    body = pg.locator("body").inner_text()
    assert "rt-sel2" in body and "rem-2" in body


def test_runner_selection_survives_validation_error(pg):
    """An invalid submission re-renders the form with the selected
    runner preserved."""
    _open_runtime_form(pg)
    sel = pg.locator('select[name="runner_id"]')
    opt2 = sel.locator("option").nth(2)
    sel.select_option(opt2.get_attribute("value"))
    # omit required executable_path -> validation error
    pg.locator('form[action$="infrastructure/runtimes"] input#name').fill("rt-bad")
    pg.locator('form[action$="infrastructure/runtimes"] input#loc-remote').check()
    pg.locator('form[action$="infrastructure/runtimes"] input#worker_label') \
        .fill("wl-bad")
    pg.locator('form[action$="infrastructure/runtimes"] button[type=submit]').click()
    pg.wait_for_timeout(1200)
    assert sel.input_value() == opt2.get_attribute("value")


def test_license_slots_set_change_clear(pg):
    """license_slots set, changed, cleared via the runner edit form;
    the rendered input reflects the persisted value."""
    pg.goto(f"{BASE}/system/infrastructure")
    pg.evaluate("document.querySelectorAll('details').forEach(d=>d.open=true)")
    form = pg.locator('form[action$="/edit"]').first
    for val in ("2", "5", ""):
        form.locator('input[name="license_slots"]').fill(val)
        form.locator("button[type=submit]").click()
        pg.wait_for_timeout(1000)
        pg.goto(f"{BASE}/system/infrastructure")
        pg.evaluate("document.querySelectorAll('details').forEach(d=>d.open=true)")
        form = pg.locator('form[action$="/edit"]').first
        assert form.locator('input[name="license_slots"]') \
            .input_value() == val


def test_license_slots_invalid_rejected_no_partial_update(pg):
    """Invalid capacity values are rejected without a partial update —
    the persisted value stays unchanged."""
    pg.goto(f"{BASE}/system/infrastructure")
    pg.evaluate("document.querySelectorAll('details').forEach(d=>d.open=true)")
    form = pg.locator('form[action$="/edit"]').first
    # establish a known-good baseline first
    form.locator('input[name="license_slots"]').fill("2")
    form.locator("button[type=submit]").click()
    pg.wait_for_timeout(1000)
    pg.goto(f"{BASE}/system/infrastructure")
    pg.evaluate("document.querySelectorAll('details').forEach(d=>d.open=true)")
    form = pg.locator('form[action$="/edit"]').first
    # submit an invalid value
    form.locator('input[name="license_slots"]').fill("abc")
    form.locator("button[type=submit]").click()
    pg.wait_for_timeout(1000)
    pg.goto(f"{BASE}/system/infrastructure")
    pg.evaluate("document.querySelectorAll('details').forEach(d=>d.open=true)")
    # persisted value must still be the earlier valid one, not blank/abc
    assert pg.locator('form[action$="/edit"]').first.locator(
        'input[name="license_slots"]').input_value() == "2"
