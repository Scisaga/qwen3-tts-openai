"""Verify voice file workflows against an isolated controlled browser-test server."""
import argparse
import hashlib
import json
import time
import uuid
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf
from playwright.sync_api import expect, sync_playwright


def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    base, headers = args.url.rstrip("/"), {"Authorization": "Bearer ui-test-token"}
    errors = []
    sample = args.output / "voice-files-reference.wav"
    sf.write(sample, .1 * np.sin(np.arange(48000) * 2 * np.pi * 440 / 24000), 24000, subtype="PCM_16")
    audio_hash = hashlib.sha256(sample.read_bytes()).hexdigest()
    suffix = uuid.uuid4().hex[:6]
    originals = [f"files-{letter}-{suffix}" for letter in "abc"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.browser_executable)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
        health = context.request.get(base + "/health").json()
        assert health["model"]["model_id"] == "controlled-test-tone", "Use an isolated tests.ui_server"

        def api(path):
            response = context.request.get(base + path, headers=headers)
            assert response.ok, response.text()
            return response.json()

        def wait_preparations():
            for _ in range(200):
                if all(job["status"] not in {"queued", "running"} for job in api("/v1/jobs")["jobs"]):
                    return
                time.sleep(.1)
            raise AssertionError("Preparation jobs did not finish")

        before = api("/v1/voices")["voices"]
        for index, ident in enumerate(originals):
            response = context.request.post(base + "/v1/voices", headers=headers, multipart={
                "voice_id": ident, "name": f"文件管理演示音色 {index + 1}", "ref_text": "参考录音对应文字。",
                "file": {"name": "reference.wav", "mimeType": "audio/wav", "buffer": sample.read_bytes()}})
            assert response.status == 202, response.text()
        wait_preparations()
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/voices")
        page.fill("#api-token", "ui-test-token")
        page.locator("#api-token").blur()

        def checkbox(ident):
            return page.locator(f'[data-check-voice="{ident}"]')

        expect(checkbox(originals[0])).to_be_visible()
        expect(page.locator("#export-voices")).to_be_disabled()
        checkbox(originals[0]).focus()
        checkbox(originals[0]).press("Space")
        expect(page.locator("#voice-selection-count")).to_have_text("已选 1 个")
        assert page.locator("#select-all-voices").evaluate("node => node.indeterminate")
        checkbox(originals[1]).check()
        page.click("#refresh-voices")
        expect(checkbox(originals[0])).to_be_checked()
        expect(checkbox(originals[1])).to_be_checked()
        expect(page.locator("#voice-selection-count")).to_have_text("已选 2 个")

        with page.expect_download() as downloaded:
            page.click("#export-voices")
        batch = args.output / "two-voices.zip"
        downloaded.value.save_as(batch)

        def verify_package(path, expected_ids):
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                assert {v["voice_id"] for v in manifest["voices"]} == set(expected_ids)
                for entry in manifest["voices"]:
                    assert entry["ref_text"] == "参考录音对应文字。" and entry["mode"] == "icl"
                    assert hashlib.sha256(archive.read(entry["audio"])).hexdigest() == audio_hash == entry["sha256"]

        verify_package(batch, originals[:2])
        row = checkbox(originals[2]).locator("xpath=../..")
        row.locator(".voice-menu summary").click()
        with page.expect_download() as downloaded:
            page.locator(f'[data-export-voice="{originals[2]}"]').click()
        single = args.output / "one-voice.zip"
        downloaded.value.save_as(single)
        verify_package(single, originals[2:])

        page.click("#clear-voice-selection")
        expect(page.locator("#delete-voices")).to_be_disabled()
        page.locator("#select-all-voices").check()
        expect(page.locator("#voice-selection-count")).to_have_text(f"已选 {len(before) + 3} 个")
        assert not page.locator("#select-all-voices").evaluate("node => node.indeterminate")
        page.locator("#select-all-voices").uncheck()
        expect(page.locator("#voice-selection-count")).to_have_text("已选 0 个")

        invalid = args.output / "invalid.zip"
        invalid.write_bytes(b"invalid package")
        with page.expect_file_chooser() as chooser:
            page.click("#import-voices")
        chooser.value.set_files([str(batch), str(single), str(invalid)])
        expect(page.locator("#voice-import-details")).to_contain_text("invalid.zip：导入失败", timeout=30000)
        expect(page.locator("#message")).to_contain_text("已导入 3 个音色")
        expect(page.locator("#message")).to_contain_text("3 个重复 ID 已自动更名")
        expect(page.locator("#voice-selection-count")).to_have_text("已选 3 个")
        imported = [ident + "-2" for ident in originals]
        for ident in imported:
            expect(checkbox(ident)).to_be_checked()
        wait_preparations()
        page.click("#refresh-voices")
        for width in (1440, 1024, 768, 390, 360):
            page.set_viewport_size({"width": width, "height": 1100 if width > 700 else 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            expect(page.locator("#export-voices")).to_be_visible()
            if width in (1440, 390):
                page.screenshot(path=str(args.output / f"voice-files-{width}.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1100})
        page.click("#delete-voices")
        expect(page.locator("#delete-voices-dialog")).to_be_visible()
        expect(page.locator("#delete-voices-list li")).to_have_count(3)
        page.click("#cancel-delete-voices")
        assert len(api("/v1/voices")["voices"]) == len(before) + 6
        # The failure stays in the confirmation dialog and preserves all selected voices.
        page.route("**/v1/voices/delete", lambda route: route.fulfill(status=409, content_type="application/json",
            body=json.dumps({"error": {"type": "voices_in_use", "message": "所选音色有任务正在使用。"}})))
        page.click("#delete-voices")
        page.click("#confirm-delete-voices")
        expect(page.locator("#delete-voices-error")).to_contain_text("任务正在使用")
        expect(page.locator("#delete-voices-dialog")).to_be_visible()
        assert len(api("/v1/voices")["voices"]) == len(before) + 6
        page.unroute("**/v1/voices/delete")
        page.click("#confirm-delete-voices")
        expect(page.locator("#delete-voices-dialog")).not_to_be_visible()
        expect(page.locator("#message")).to_have_text("已删除 3 个音色。")
        expect(page.locator("#voice-selection-count")).to_have_text("已选 0 个")
        expect(page.locator("#delete-voices")).to_be_disabled()
        for ident in originals:
            expect(checkbox(ident)).to_be_visible()
        row = checkbox(originals[2]).locator("xpath=../..")
        row.locator(".voice-menu summary").click()
        page.locator(f'[data-delete-voice="{originals[2]}"]').click()
        expect(page.locator("#delete-voices-list li")).to_have_count(1)
        page.click("#confirm-delete-voices")
        expect(checkbox(originals[2])).to_have_count(0)
        for ident in originals[:2]:
            checkbox(ident).check()
        page.click("#delete-voices")
        page.click("#confirm-delete-voices")
        expect(checkbox(originals[0])).to_have_count(0)
        assert {v["voice_id"] for v in api("/v1/voices")["voices"]} == {v["voice_id"] for v in before}
        if not before:
            expect(page.locator("#voice-list")).to_contain_text("还没有参考音色")
            expect(page.locator("#select-all-voices")).to_be_disabled()
        browser.close()
    assert not errors, errors
    (args.output / "voice-files-ui.json").write_text(json.dumps({"passed": True, "javascript_errors": errors,
        "workflows": ["keyboard-single-selection", "multiple-selection", "refresh-preserves-selection", "select-all-and-clear",
                      "batch-and-single-export", "wav-and-metadata-roundtrip", "multiple-package-import", "collision-renaming",
                      "mixed-import-results", "cancel-delete", "delete-failure-retains-selection", "batch-and-single-delete",
                      "five-responsive-widths", "empty-library"]}, indent=2))
    print("VOICE_FILES_UI_PASS (isolated controlled backend)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:12308")
    parser.add_argument("--output", type=Path, default=Path("test-results/voice-files-ui"))
    parser.add_argument("--browser-executable", default=None)
    main(parser.parse_args())
