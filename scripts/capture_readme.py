"""Refresh README images using an isolated tests.ui_server, never a live model service."""

import argparse
import json
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf
from playwright.sync_api import expect, sync_playwright


def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    args.evidence.mkdir(parents=True, exist_ok=True)
    sample = args.evidence / "demo-reference.wav"
    sf.write(sample, .1 * np.sin(np.arange(72000) * 2 * np.pi * 440 / 24000), 24000, subtype="PCM_16")
    headers = {"Authorization": "Bearer ui-test-token"}
    with httpx.Client(base_url=args.url, headers=headers, trust_env=False, timeout=15) as client:
        health = client.get("/health")
        health.raise_for_status()
        health = health.json()
        if health.get("model", {}).get("model_id") != "controlled-test-tone":
            raise ValueError("Use an isolated tests.ui_server; refusing to add demonstration voices to a model service")
        if not health.get("features", {}).get("quick_voice_clone", {}).get("enabled"):
            raise ValueError("Start tests.ui_server with UI_TEST_QUICK_CLONE=1 and UI_TEST_PROGRESS=1")
        existing = {v["voice_id"]: v for v in client.get("/v1/voices").json()["voices"]}
        for voice_id, name in (("narrator", "叙事讲解 · 主讲示例"),
                               ("documentary", "纪录旁白 · 沉稳示例"),
                               ("gentle", "人文故事 · 温柔示例")):
            if voice_id in existing:
                assert existing[voice_id]["name"] == name, "Use a separate demonstration data directory"
                continue
            with sample.open("rb") as audio:
                response = client.post("/v1/voices", files={"file": (sample.name, audio, "audio/wav")},
                                       data={"voice_id": voice_id, "name": name, "mode": "icl",
                                             "ref_text": "这是一段用于展示界面的参考录音。"})
            response.raise_for_status()
            job_id = response.json()["preparation_job_id"]
            for _ in range(100):
                job = client.get("/v1/jobs/" + job_id).json()
                if job["status"] in {"succeeded", "failed", "cancelled"}:
                    assert job["status"] == "succeeded", job.get("error")
                    break
                time.sleep(.1)
            else:
                raise TimeoutError("Demonstration voice preparation did not finish")

    director = {
        "schema_version": 2,
        "policy": {"visual_sync": "independent", "performance_contract": "rhetorical-v1"},
        "pages": [
            {"page": 1, "chapter": "library-story", "role": "故事开场", "intent": "opening",
             "direction": "自然叙述。", "rationale": "先让听众看见故事发生的地方。", "target_seconds": 15,
             "segments": [
                 {"text": "十年前，这座小城的图书馆只有一间阅览室。", "rate": "-3%", "pitch": "+0st", "pause_after_ms": 180},
                 {"text": "放学后的孩子们常常挤在窗边，等着一本刚刚归还的故事书。", "rate": "+0%", "pitch": "+0st", "pause_after_ms": 120},
             ]},
            {"page": 2, "chapter": "library-story", "role": "转变与结尾", "intent": "closing",
             "direction": "自然叙述。", "rationale": "从阅读活动讲到人与城市的变化。", "target_seconds": 15,
             "segments": [
                 {"text": "后来，志愿者把阅读活动带进社区，越来越多的家庭加入进来。", "rate": "+2%", "pitch": "+0st", "pause_after_ms": 180},
                 {"text": "改变的，不只是书本的数量，更是每个人愿意为这座城市多做一点的心意。", "rate": "-2%", "pitch": "+0st", "pause_after_ms": 120},
             ]},
        ],
    }
    errors, images = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.browser_executable)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, timezone_id="Asia/Shanghai")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url.rstrip("/") + "/studio?mode=director")
        page.fill("#api-token", "ui-test-token")
        page.locator("#api-token").blur()
        expect(page.locator('#voice-select option[value="narrator"]')).to_be_attached()
        page.select_option("#voice-select", "narrator")
        page.add_style_tag(content="""
            .status-strip, .job-identity, #token-field, #message { display:none !important; }
            .job-table-id { font-size:0 !important; }
            .job-table-id::after { content:'演示任务'; font-size:13px; }
        """)

        def capture(name):
            page.locator("#director-editor").evaluate("node => { node.scrollTop=0; }")
            page.evaluate("scrollTo(0,0)")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(args.output / name), full_page=True, animations="disabled")
            images.append(name)

        page.fill("#director-editor", json.dumps(director, ensure_ascii=False, indent=2))
        page.click("#profile-disclosure > summary")
        page.fill("#profile-editor", json.dumps({"schema_version": 1, "rate": "+0%", "pitch": "+0st", "page_break_ms": 180}))
        page.click("#profile-disclosure > summary")
        page.click("#compile-button")
        expect(page.locator("#plan-review")).to_be_visible()
        page.check("#allow-degraded")
        page.click("#compile-button")
        expect(page.locator("#plan-summary")).to_contain_text("预检通过")
        capture("workbench.png")

        page.click('[data-view="voices"]')
        page.click("#voice-tab-upload")
        expect(page.locator(".voice-item")).to_have_count(3)
        page.set_input_files("#sample-file", str(sample.resolve()))
        page.fill("#voice-name", "我的视频旁白")
        page.fill("#voice-id", "my-narrator")
        page.fill("#ref-text", "这里填写参考录音中实际说出的文字。")
        capture("voices.png")
        page.click("#voice-tab-quick")
        page.set_input_files("#quick-file", str(sample.resolve()))
        expect(page.locator("#quick-status")).to_contain_text("文字已识别")
        page.fill("#quick-name", "我的讲解音色")
        page.fill("#quick-voice-id", "my-speaker")
        capture("quick-clone.png")
        page.click("#quick-reset")
        page.click('[data-view="studio"]')
        page.click("#submit-button")
        deadline = time.monotonic() + 60
        progress_captured = False
        while time.monotonic() < deadline:
            detail = page.locator("#job-progress-detail").inner_text()
            if "已生成约" in detail and not progress_captured:
                capture("progress.png")
                progress_captured = True
            if page.locator("#job-status").inner_text() == "已完成":
                break
            page.wait_for_timeout(150)
        else:
            raise TimeoutError("Demonstration narration did not finish")
        assert progress_captured, "Use UI_TEST_PROGRESS=1 and fresh demo data; cached jobs have no decoding progress"
        page.wait_for_function("Number.isFinite(document.querySelector('#full-audio')?.duration)")
        expect(page.locator("#playback-select option")).to_have_count(3)
        capture("audio-result.png")
        page.click('[data-view="jobs"]')
        expect(page.locator("#job-list tr").first).to_be_visible()
        capture("jobs.png")
        page.click('[data-view="mcp"]')
        expect(page.locator("#mcp-state")).to_have_text("已连接")
        expect(page.locator(".mcp-tool")).to_have_count(3)
        capture("mcp.png")
        page.click('[data-view="runtime"]')
        expect(page.locator('[data-capability="instruction_control"]')).to_contain_text("自然语言要求")
        capture("runtime.png")
        page.click('[data-view="voices"]')
        page.set_viewport_size({"width": 390, "height": 844})
        capture("mobile.png")
        browser.close()
    assert not errors, errors
    (args.evidence / "screenshots.json").write_text(json.dumps({
        "source": "isolated controlled-tone backend with demonstration voices and a director script",
        "production_data_used": False, "images": images, "javascript_errors": errors,
        "desktop_width": 1440, "mobile_width": 390,
    }, ensure_ascii=False, indent=2) + "\n")
    print(f"README_SCREENSHOTS_PASS ({len(images)} current interface images; no production data)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:12308")
    parser.add_argument("--output", type=Path, default=Path("docs/images"))
    parser.add_argument("--evidence", type=Path, default=Path("test-results/readme-ui"))
    parser.add_argument("--browser-executable")
    main(parser.parse_args())
