"""Quick-clone browser acceptance with isolated tone backend and controlled ASR responses."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import soundfile as sf
from playwright.sync_api import Error, expect, sync_playwright


def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    sample = (args.output / "microphone-fixture.wav").resolve()
    sf.write(sample, 0.1 * np.sin(np.arange(24000 * 5) * 2 * np.pi * 440 / 24000), 24000, subtype="PCM_16")
    errors, workflows = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=args.browser_executable,
            args=[
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={sample}",
                "--no-proxy-server",
                "--host-resolver-rules=MAP quick-clone.test 127.0.0.1",
            ],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, permissions=["microphone"])
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""(() => {
          const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          window.originalGetUserMedia = original;
          navigator.mediaDevices.getUserMedia = async options => {
            const stream = await original(options); window.recordedTracks = stream.getTracks(); return stream;
          };
        })();""")
        page.goto(args.url)
        expect(page.locator("#connection")).to_have_text("服务在线")
        page.fill("#api-token", "ui-test-token")
        page.locator("#api-token").blur()
        expect(page.locator("#go-quick-clone")).to_be_visible()
        page.click("#go-quick-clone")
        expect(page.locator("#quick-clone-card")).to_be_visible()
        expect(page.locator("#voice-tab-quick")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#voice-upload-card")).to_be_hidden()
        expect(page.locator("#quick-create")).to_be_disabled()
        page.click("#quick-record")
        expect(page.locator("#quick-stop")).to_be_visible()
        page.wait_for_timeout(2400)
        with page.expect_response(
            lambda response: response.url.endswith("/v1/voices/references") and response.request.method == "POST"
        ) as captured:
            page.click("#quick-stop")
        draft = captured.value.json()
        assert draft["transcription_state"] == "succeeded", draft
        expect(page.locator("#quick-ref-text")).to_have_value("这是一段浏览器测试录音。")
        page.wait_for_function("document.querySelector('#quick-sample-preview').readyState >= 1")
        assert page.locator("#quick-sample-preview").evaluate("node => node.duration") >= 1
        assert page.evaluate("recordedTracks.every(track => track.readyState === 'ended')")
        workflows.extend(
            [
                "microphone-recording",
                "webm-conversion",
                "automatic-transcription",
                "tracks-released",
                "reference-playback",
            ]
        )
        page.fill("#quick-name", "网页快速复刻")
        page.fill("#quick-ref-text", "确认后的录音对应文字。")
        page.click("#voice-tab-upload")
        expect(page.locator("#quick-clone-card")).to_be_hidden()
        expect(page.locator("#voice-upload-card")).to_be_visible()
        page.fill("#voice-name", "尚未提交的上传音色")
        page.fill("#ref-text", "上传页保留的文字。")
        page.evaluate("health()")
        expect(page.locator("#quick-clone-card")).to_be_hidden()
        page.locator("#voice-tab-upload").press("ArrowLeft")
        expect(page.locator("#quick-clone-card")).to_be_visible()
        expect(page.locator("#voice-tab-quick")).to_be_focused()
        expect(page.locator("#quick-name")).to_have_value("网页快速复刻")
        expect(page.locator("#quick-ref-text")).to_have_value("确认后的录音对应文字。")
        expect(page.locator("#quick-sample-preview")).to_be_visible()
        page.locator("#voice-tab-quick").press("End")
        expect(page.locator("#voice-name")).to_have_value("尚未提交的上传音色")
        expect(page.locator("#ref-text")).to_have_value("上传页保留的文字。")
        page.locator("#voice-tab-upload").press("Home")
        workflows.extend(["parallel-voice-tabs", "tab-draft-preservation", "health-poll-keeps-tab", "keyboard-voice-tabs"])
        with page.expect_response(
            lambda response: response.url.endswith("/v1/voices") and response.request.method == "POST"
        ) as registered:
            page.click("#quick-create")
        voice = registered.value.json()
        assert voice["mode"] == "icl" and voice["ref_text"] == "确认后的录音对应文字。"
        expect(page.locator("#quick-result-status")).to_contain_text("音色已就绪", timeout=20000)
        expect(page.locator("#voice-select")).to_have_value(voice["voice_id"])
        expect(page.locator("#quick-create")).to_be_disabled()
        page.click("#quick-audition")
        expect(page.locator("#quick-result-status")).to_contain_text("试听已生成", timeout=20000)
        page.wait_for_function("document.querySelector('#quick-result-audio').readyState >= 1")
        assert page.locator("#quick-result-audio").evaluate("node => node.duration") > 0
        page.click("#quick-use")
        expect(page.locator("#view-studio")).to_be_visible()
        expect(page.locator("#voice-select")).to_have_value(voice["voice_id"])
        workflows.extend(
            [
                "reviewed-text",
                "icl-registration",
                "voice-preparation",
                "automatic-selection",
                "new-sentence-audition",
                "use-in-studio",
            ]
        )

        # Controlled ASR fails on its second call: keep audio and enable manual text.
        page.click("#go-quick-clone")
        page.click("#quick-reset")
        page.set_input_files("#quick-file", str(sample))
        expect(page.locator("#quick-status")).to_contain_text("转写超时", timeout=20000)
        expect(page.locator("#quick-sample-preview")).to_be_visible()
        page.fill("#quick-ref-text", "识别失败时也能手动填写。")
        expect(page.locator("#quick-create")).to_be_enabled()
        page.click("#quick-retry")
        expect(page.locator("#quick-ref-text")).to_have_value("这是一段浏览器测试录音。", timeout=20000)
        expect(page.locator("#quick-status")).to_contain_text("文字已识别")
        workflows.extend(
            ["short-recording-upload", "asr-timeout-retains-draft", "manual-text-fallback", "transcription-retry"]
        )
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(args.output / "quick-clone-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        workflows.append("mobile-width")
        page.click('[data-view="studio"]')
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.click("#go-quick-clone")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.screenshot(path=str(args.output / "quick-clone-desktop.png"), full_page=True)

        # Denied permission stays recoverable and does not leave a microphone active.
        page.click("#quick-reset")
        page.evaluate(
            "() => { navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Denied', 'NotAllowedError')); }"
        )
        page.click("#quick-record")
        expect(page.locator("#quick-status")).to_contain_text("麦克风权限未获允许")
        expect(page.locator("#quick-record")).to_be_enabled()
        workflows.append("microphone-denial")
        page.evaluate(
            "() => { navigator.mediaDevices.getUserMedia = async options => {const stream = await originalGetUserMedia(options); window.recordedTracks = stream.getTracks(); return stream;}; }"
        )
        page.click("#quick-record")
        expect(page.locator("#quick-stop")).to_be_visible()
        page.click("#voice-tab-upload")
        assert page.evaluate("recordedTracks.every(track => track.readyState === 'ended')")
        expect(page.locator("#voice-upload-card")).to_be_visible()
        page.click("#voice-tab-quick")
        expect(page.locator("#quick-record")).to_be_enabled()
        workflows.append("tab-switch-stops-microphone")
        page.click("#quick-record")
        expect(page.locator("#quick-stop")).to_be_visible()
        page.click('[data-view="studio"]')
        assert page.evaluate("recordedTracks.every(track => track.readyState === 'ended')")
        workflows.append("navigation-stops-microphone")

        # Switch a pending recognition to manual text without losing the local recording.
        page.click("#go-quick-clone")
        held = []

        def hold_recognition(route):
            if b'name="transcribe"\r\n\r\nfalse' in route.request.post_data_buffer:
                route.continue_()
            else:
                held.append(route)

        page.route("**/v1/voices/references", hold_recognition)
        page.set_input_files("#quick-file", str(sample))
        expect(page.locator("#quick-status")).to_contain_text("正在处理录音")
        page.wait_for_timeout(150)
        assert held
        page.check("#quick-manual")
        for route in held:
            try:
                route.abort()
            except Error:
                pass  # The browser may already have cancelled the previous recognition.
        expect(page.locator("#quick-status")).to_contain_text("请填写录音对应文字", timeout=10000)
        expect(page.locator("#quick-ref-text")).to_be_enabled()
        page.fill("#quick-ref-text", "手动确认的录音文字。")
        expect(page.locator("#quick-create")).to_be_enabled()
        with page.expect_response(
            lambda response: response.url.endswith("/v1/voices") and response.request.method == "POST"
        ) as manual:
            page.click("#quick-create")
        assert manual.value.json()["ref_text"] == "手动确认的录音文字。"
        expect(page.locator("#quick-result-status")).to_contain_text("音色已就绪", timeout=20000)
        page.unroute("**/v1/voices/references", hold_recognition)
        workflows.extend(["pending-asr-manual-bypass", "manual-icl-preparation"])

        # Plain HTTP away from localhost must explain the recording requirement.
        insecure = context.new_page()
        insecure.on("pageerror", lambda error: errors.append(str(error)))
        port = urlsplit(args.url).port
        insecure.goto(f"http://quick-clone.test:{port}/")
        insecure.fill("#api-token", "ui-test-token")
        insecure.locator("#api-token").blur()
        insecure.click("#go-quick-clone")
        assert insecure.evaluate("window.isSecureContext") is False
        expect(insecure.locator("#quick-context-note")).to_contain_text("HTTPS 或 localhost")
        expect(insecure.locator("#quick-file")).to_be_enabled()
        https_settings = insecure.evaluate("state.health.features.quick_voice_clone")
        if https_settings["https_url"]:
            expect(insecure.locator("#quick-record")).to_be_enabled()
            expect(insecure.locator("#quick-record-label")).to_have_text("打开录音页面")
            expect(insecure.locator("#quick-secure-link")).to_have_attribute("href", https_settings["https_url"] + "/#quick-clone")
            if https_settings["https_local_ca"]:
                expect(insecure.locator("#quick-ca-link")).to_be_visible()
            insecure.route(https_settings["https_url"] + "/**", lambda route: route.fulfill(status=200, content_type="text/html", body="<p>HTTPS recording entrance</p>"))
            insecure.click("#quick-record")
            insecure.wait_for_url(https_settings["https_url"] + "/#quick-clone")
            workflows.append("http-button-opens-https")
        else:
            expect(insecure.locator("#quick-record")).to_be_disabled()
        workflows.append("http-microphone-guidance")
        browser.close()
    assert not errors, errors
    report = {
        "passed": True,
        "model_source": "controlled-test-tone",
        "asr_source": "controlled-http-transport",
        "qwen_inference_verified": False,
        "javascript_errors": errors,
        "workflows": workflows,
    }
    (args.output / "quick-clone-ui.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"QUICK_CLONE_UI_PASS ({len(workflows)} workflows; controlled backend, not acoustic acceptance)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:12308")
    parser.add_argument("--output", type=Path, default=Path("test-results/quick-clone-ui"))
    parser.add_argument("--browser-executable")
    main(parser.parse_args())
