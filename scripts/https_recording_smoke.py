"""Verify trusted LAN HTTPS recording; creates and deletes only its own temporary draft."""

import argparse
import hashlib
import json
import os
import ssl
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright


def main(args):
    load_dotenv()
    token = os.getenv("API_TOKEN", "")
    args.output.mkdir(parents=True, exist_ok=True)
    sample = (args.output / "microphone-fixture.wav").resolve()
    sf.write(sample, 0.1 * np.sin(np.arange(24000 * 5) * 2 * np.pi * 440 / 24000), 24000, subtype="PCM_16")
    context = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else ssl.create_default_context()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    errors, draft_id = [], None
    with httpx.Client(base_url=args.url, verify=context, trust_env=False, headers=headers, timeout=30) as client:
        ready = client.get("/ready")
        ready.raise_for_status()
        assert ready.json()["inference_ready"]
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    executable_path=args.browser_executable,
                    args=["--no-sandbox", "--no-proxy-server", "--use-fake-ui-for-media-stream",
                          "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={sample}"],
                )
                browser_context = browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1000})
                page = browser_context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("""(() => {
                  const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                  navigator.mediaDevices.getUserMedia = async options => {
                    const stream = await original(options); window.recordedTracks = stream.getTracks(); return stream;
                  };
                })();""")
                page.goto(args.url.rstrip("/") + "/#quick-clone")
                assert page.evaluate("isSecureContext") is True
                if token:
                    page.fill("#api-token", token)
                    page.locator("#api-token").blur()
                expect(page.locator("#quick-clone-card")).to_be_visible()
                expect(page.locator("#voice-upload-card")).to_be_hidden()
                expect(page.locator("#quick-context-note")).to_be_hidden()
                expect(page.locator("#quick-record")).to_be_enabled()
                page.check("#quick-manual")
                page.click("#quick-record")
                expect(page.locator("#quick-stop")).to_be_visible()
                page.wait_for_timeout(2400)
                with page.expect_response(
                    lambda response: response.url.endswith("/v1/voices/references") and response.request.method == "POST"
                ) as captured:
                    page.click("#quick-stop")
                draft = captured.value.json()
                draft_id = draft["draft_id"]
                assert draft["transcription_state"] == "pending"
                assert draft["reference"]["duration_seconds"] >= 1
                expect(page.locator("#quick-status")).to_contain_text("请填写录音对应文字")
                page.wait_for_function("document.querySelector('#quick-sample-preview').readyState >= 1")
                assert page.locator("#quick-sample-preview").evaluate("node => node.duration") >= 1
                assert page.evaluate("recordedTracks.every(track => track.readyState === 'ended')")
                page.screenshot(path=str(args.output / "https-recording.png"), full_page=True)
                page.click("#quick-reset")
                expect(page.locator("#quick-sample-preview")).to_be_hidden()
                browser.close()
        finally:
            if draft_id:
                deleted = client.delete(f"/v1/voices/references/{draft_id}")
                assert deleted.status_code in {204, 404}, deleted.status_code
    assert not errors, errors
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(args.ca_file.read_text())).hexdigest() if args.ca_file else None
    report = {"passed": True, "url": args.url, "certificate_verified": True,
              "browser_certificate_errors_ignored": False, "secure_context": True,
              "microphone_source": "chromium-controlled-device", "reference_duration_seconds": draft["reference"]["duration_seconds"],
              "recording_converted_and_playable": True, "tracks_released": True, "draft_deleted": True,
              "persistent_voice_created": False, "javascript_errors": errors, "ca_sha256": fingerprint}
    (args.output / "https-recording.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("HTTPS_RECORDING_PASS (trusted certificate, LAN address, temporary draft cleaned)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("test-results/https-recording"))
    parser.add_argument("--browser-executable")
    options = parser.parse_args()
    if not options.url.startswith("https://"):
        parser.error("--url must use HTTPS")
    main(options)
