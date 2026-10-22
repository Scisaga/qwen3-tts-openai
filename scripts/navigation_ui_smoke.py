"""Navigation and presentation checks against the isolated tests.ui_server only."""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import soundfile as sf
from playwright.sync_api import expect, sync_playwright


def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    base = args.url.rstrip("/")
    headers = {"Authorization": "Bearer ui-test-token"}
    routes = {"studio": "/studio", "voices": "/voices", "jobs": "/jobs", "mcp": "/integrations/mcp", "runtime": "/runtime"}
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.browser_executable)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        health = context.request.get(base + "/health").json()
        assert health["model"]["model_id"] == "controlled-test-tone", "Use an isolated tests.ui_server"

        def authenticate():
            expect(page.locator("#api-token")).to_be_visible()
            page.fill("#api-token", "ui-test-token")
            page.locator("#api-token").blur()

        for view, path in routes.items():
            page.goto(base + path)
            expect(page.locator("#view-" + view)).to_be_visible()
            expect(page.locator(f'[data-view="{view}"]')).to_have_attribute("aria-current", "page")
            assert page.locator(f'[data-view="{view}"]').get_attribute("href") == path
            page.reload()
            expect(page.locator("#view-" + view)).to_be_visible()
            assert urlsplit(page.url).path == path

        page.goto(base + "/studio")
        authenticate()
        draft = "切换页面并前进后退时，保留这段未提交的文稿。"
        page.fill("#text-editor", draft)
        page.click('[data-view="voices"]')
        expect(page.locator("#view-voices")).to_be_visible()
        page.click('[data-view="runtime"]')
        page.go_back()
        expect(page.locator("#view-voices")).to_be_visible()
        page.go_back()
        expect(page.locator("#view-studio")).to_be_visible()
        expect(page.locator("#text-editor")).to_have_value(draft)
        page.go_forward()
        expect(page.locator("#view-voices")).to_be_visible()

        original_url = page.url
        with context.expect_page() as opened:
            page.locator('[data-view="jobs"]').click(modifiers=["Control"])
        new_tab = opened.value
        new_tab.wait_for_load_state()
        expect(new_tab.locator("#view-jobs")).to_be_visible()
        assert urlsplit(new_tab.url).path == "/jobs" and page.url == original_url
        new_tab.close()

        for mode in ("director", "ssml"):
            page.goto(base + "/studio?mode=" + mode)
            expect(page.locator("#tab-" + mode)).to_have_attribute("aria-selected", "true")
            page.reload()
            expect(page.locator("#mode-" + mode)).to_be_visible()

        sample = args.output / "navigation-reference.wav"
        sf.write(sample, .1 * np.sin(np.arange(28800) * 2 * np.pi * 440 / 24000), 24000)
        response = context.request.post(base + "/v1/voices", headers=headers,
                                        multipart={"name": "Navigation Narrator", "ref_text": "参考文字。",
                                                   "file": {"name": "reference.wav", "mimeType": "audio/wav", "buffer": sample.read_bytes()}})
        assert response.status == 202, response.text()
        voice = response.json()
        submitted = context.request.post(base + "/v1/narrations", headers=headers,
                                         data={"text": "独立任务链接测试。", "voice_id": voice["voice_id"], "compatibility": "generic"})
        assert submitted.status == 202, submitted.text()
        job_id = submitted.json()["job_id"]
        for _ in range(100):
            job = context.request.get(base + "/v1/jobs/" + job_id, headers=headers).json()
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.1)
        assert job["status"] == "succeeded", job.get("error")
        job_url = base + "/studio?panel=task&job=" + job_id
        for _ in range(2):
            page.goto(job_url)
            authenticate()
            expect(page.locator("#active-job")).to_contain_text(job_id)
            expect(page.locator("#job-status")).to_have_text("已完成")

        page.click('[data-view="voices"]')
        badge = page.locator(f'[data-copy-voice-id="{voice["voice_id"]}"]')
        expect(badge).to_be_visible()
        assert badge.inner_text().replace("\n", "") == "id" + voice["voice_id"]
        assert badge.locator("svg").count() == 0
        for part in ("span", "code"):
            badge.locator(part).click()
            assert page.evaluate("navigator.clipboard.readText()") == voice["voice_id"]
        badge.focus()
        badge.press("Enter")
        assert page.evaluate("navigator.clipboard.readText()") == voice["voice_id"]
        assert page.locator("body").evaluate("node => parseFloat(getComputedStyle(node).fontSize)") == 14
        assert page.locator(".voice-item h3").first.evaluate("node => parseFloat(getComputedStyle(node).fontSize)") == 14

        for width in (1440, 1280, 1024, 768, 390, 360):
            page.set_viewport_size({"width": width, "height": 1100 if width > 700 else 844})
            if width <= 700:
                assert page.locator("body").evaluate("node => parseFloat(getComputedStyle(node).fontSize)") == 13
                assert page.locator(".nav").first.evaluate("node => parseFloat(getComputedStyle(node).fontSize)") == 10
                assert page.locator(".nav").evaluate_all("nodes => nodes.every(node => node.querySelector('span').getBoundingClientRect().width <= node.clientWidth - 2)")
            for view in routes:
                page.click(f'[data-view="{view}"]')
                expect(page.locator("#view-" + view)).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, view)
                if width in (1440, 390):
                    page.screenshot(path=str(args.output / f"{view}-{width}.png"), full_page=True)

        browser.close()
    assert not errors, errors
    (args.output / "navigation-ui.json").write_text(json.dumps({"passed": True, "javascript_errors": errors,
        "workflows": ["direct-pages", "page-reload", "back-forward", "preserved-draft", "native-new-tab", "mode-links",
                      "job-links-and-reload", "badge-left-and-right-copy", "keyboard-copy", "desktop-type-scale", "six-responsive-widths"]}, indent=2))
    print("NAVIGATION_UI_PASS (isolated controlled backend)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:12308")
    parser.add_argument("--output", type=Path, default=Path("test-results/navigation"))
    parser.add_argument("--browser-executable")
    main(parser.parse_args())
