"""Resume tab: the builder zero-state, and the versions panel behind it."""

import json
import sys
import urllib.request
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_resumes import resume  # noqa: E402


def _versions(url):
    return json.load(urllib.request.urlopen(url + "/api/resumes"))


def test_upload_and_switch_without_reload(server, tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(resume("first"))
    b.write_bytes(resume("second"))
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(server)
        pg.get_by_text("Resume", exact=True).first.click()
        # The tab opens on the builder now; the version list is behind the toggle.
        pg.get_by_role("button", name="Versions").click()
        swe = pg.locator(".rs-card").first
        for f, txt in ((a, "a.pdf"), (b, "b.pdf")):
            swe.locator("input[type=file]").set_input_files(str(f))
            swe.get_by_text("Upload & activate").click()
            swe.get_by_text(f"{txt} is now live").wait_for()
            assert swe.locator(".rs-ver", has_text=txt).count() == 1  # list updated in place
        vs = _versions(server)["swe"]
        assert [v["filename"] for v in vs][:2] == ["b.pdf", "a.pdf"] and vs[0]["active"]
        swe.locator(".rs-ver", has_text="a.pdf").click()  # preview the older one
        swe.get_by_text("Make live").click()
        swe.get_by_text("Switched the live resume").wait_for()
        assert _versions(server)["swe"][1]["active"]
        assert not errors
        browser.close()


def test_builder_rail_and_setup_dialog_render_without_a_latex_install(server):
    """CI has no pdflatex and no uploaded source, so the builder must degrade to
    an explanatory zero-state rather than erroring."""
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.goto(server)
        pg.get_by_text("Resume", exact=True).first.click()
        # The rail lists every resume; nothing is open yet.
        pg.locator(".bld-rail").wait_for()
        assert pg.locator(".bld-rail-item").count() == 3
        pg.get_by_text("Pick a resume").wait_for()

        # Opening one with no source offers the setup dialog instead of erroring.
        pg.locator(".bld-rail-item").first.click()
        pg.get_by_role("button", name="Upload source").click()
        pg.locator(".bld-modal").wait_for()
        assert pg.locator('.bld-modal input[type=file]').count() == 1
        pg.get_by_role("button", name="Cancel").click()

        # Toggling to Versions and back must not blow up.
        pg.get_by_role("button", name="Versions").click()
        pg.locator(".rs-card").first.wait_for()
        pg.get_by_role("button", name="Builder").click()
        pg.locator(".bld-rail").wait_for()
        assert not errors
        browser.close()
