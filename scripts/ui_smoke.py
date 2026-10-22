"""Browser-only service test against tests.ui_server. Does not verify Qwen acoustics."""
import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from playwright.sync_api import expect, sync_playwright


def main(args):
    args.output.mkdir(parents=True,exist_ok=True)
    sample=args.output/"controlled-tone.wav"
    sf.write(sample,.1*np.sin(np.arange(48000)*2*np.pi*440/24000),24000)
    errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=args.browser_executable)
        page=browser.new_page(viewport={"width":1440,"height":1100},accept_downloads=True)
        page.on("pageerror",lambda error:errors.append(str(error)))
        page.goto(args.url)
        expect(page.locator("#connection")).to_have_text("服务在线")
        expect(page.locator("#tab-text")).to_have_attribute("aria-selected","true")
        expect(page.locator("#tab-text")).to_have_text("文本配音")
        expect(page.locator("#plan-empty")).to_be_visible()
        assert page.locator(".brand strong").evaluate("node => node.getBoundingClientRect().height < parseFloat(getComputedStyle(node).fontSize) * 1.5")
        mcp_requests=[]
        page.on("request",lambda request:mcp_requests.append(request.post_data_json) if request.url.endswith("/mcp/") and request.method=="POST" else None)
        page.click('[data-view="mcp"]')
        expect(page.locator("#mcp-state")).to_have_text("未连接")
        expect(page.locator("#mcp-check-result")).to_contain_text("API Token")
        page.fill("#api-token","ui-test-token")
        page.locator("#api-token").blur()
        expect(page.locator("#mcp-state")).to_have_text("已连接")
        expect(page.locator("#mcp-tool-count")).to_have_text("3 个工具")
        assert page.input_value("#mcp-endpoint")==args.url.rstrip("/")+"/mcp/"
        names=page.locator(".mcp-tool-heading code").all_text_contents()
        assert set(names)=={"list_voices","submit_narration","get_job"}
        submit_tool=page.locator(".mcp-tool").filter(has=page.locator("code",has_text="submit_narration"))
        submit_tool.locator("summary").click()
        schema=json.loads(submit_tool.locator("pre").inner_text())
        assert schema["properties"]["request"]["type"]=="object"
        submit_tool.locator("summary").click()
        config=json.loads(page.locator("#mcp-config").inner_text())
        server=config["mcpServers"]["qwen3-tts-openai"]
        assert server["url"]==args.url.rstrip("/")+"/mcp/"
        assert server["headers"]["Authorization"]=="Bearer <API_TOKEN>"
        assert "ui-test-token" not in page.locator("#view-mcp").inner_text()
        page.context.grant_permissions(["clipboard-read","clipboard-write"])
        page.click("#copy-mcp-config")
        expect(page.locator("#message")).to_contain_text("已复制")
        assert json.loads(page.evaluate("navigator.clipboard.readText()"))==config
        page.click("#copy-mcp-endpoint")
        expect(page.locator("#message")).to_contain_text("MCP 地址已复制")
        assert page.evaluate("navigator.clipboard.readText()") == server["url"]
        # A missing/disabled MCP entry must clear the old tool catalog and explain recovery.
        def missing_mcp(route):
            route.fulfill(status=404,json={"detail":"Not Found"})
        page.route("**/mcp/",missing_mcp)
        page.click("#check-mcp")
        expect(page.locator("#mcp-state")).to_have_text("未连接")
        expect(page.locator("#mcp-check-result")).to_contain_text("ENABLE_MCP=1")
        expect(page.locator(".mcp-tool")).to_have_count(0)
        page.unroute("**/mcp/",missing_mcp)
        # Config follows the reported auth mode, without copying the user's credential.
        def no_api_auth(route):
            response=route.fetch()
            health=response.json()
            health["api_auth_enabled"]=False
            route.fulfill(response=response,json=health)
        page.route("**/health",no_api_auth)
        expect(page.locator("#mcp-auth-state")).to_have_text("未启用",timeout=10000)
        assert "headers" not in json.loads(page.locator("#mcp-config").inner_text())["mcpServers"]["qwen3-tts-openai"]
        page.unroute("**/health",no_api_auth)
        expect(page.locator("#mcp-auth-state")).to_have_text("Bearer Token",timeout=10000)
        page.click("#check-mcp")
        expect(page.locator("#mcp-state")).to_have_text("已连接")
        assert {r["method"] for r in mcp_requests}=={"initialize","notifications/initialized","tools/list"}
        assert page.request.get(args.url+"/v1/jobs",headers={"Authorization":"Bearer ui-test-token"}).json()["jobs"]==[]
        page.screenshot(path=str(args.output/"mcp.png"),full_page=True)
        page.click('[data-view="voices"]')
        page.click("#voice-tab-upload")
        expect(page.locator("#voice-tab-quick")).to_be_hidden()
        expect(page.locator("#voice-upload-card")).to_be_visible()
        page.set_input_files("#sample-file",str(sample.resolve()))
        expect(page.locator("#voice-name")).to_have_value("controlled-tone")
        page.fill("#voice-name","浏览器测试音色")
        page.fill("#voice-id","studio-narrator")
        page.fill("#ref-text","这是一段服务测试对应文字。")
        page.wait_for_function("document.querySelector('#sample-preview').readyState >= 1")
        assert page.locator("#sample-preview").evaluate("node => node.duration")>0
        page.locator('#voice-form button[type="submit"]').click()
        expect(page.locator("#message")).to_contain_text("音色已保存",timeout=20000)
        page.click("#refresh-voices")
        expect(page.locator(".voice-item h3")).to_contain_text("浏览器测试音色")
        expect(page.locator("#voice-list .voice-id-badge code")).to_have_text("studio-narrator")
        badge = page.locator('[data-copy-voice-id="studio-narrator"]')
        expect(badge).to_have_text("idstudio-narrator")
        assert badge.locator("svg").count() == 0
        page.click('[data-copy-voice-id="studio-narrator"]')
        expect(page.locator("#message")).to_contain_text("音色 ID 已复制")
        assert page.evaluate("navigator.clipboard.readText()") == "studio-narrator"
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value:undefined, configurable:true})")
        page.click('[data-copy-voice-id="studio-narrator"]')
        expect(page.locator("#message")).to_contain_text("音色 ID 已复制")
        page.evaluate("delete navigator.clipboard")
        assert page.evaluate("navigator.clipboard.readText()") == "studio-narrator"
        page.screenshot(path=str(args.output/"voice-ids.png"),full_page=True)
        page.locator(".voice-menu > summary").first.click()
        page.locator("[data-edit]").first.click()
        page.fill("#edit-ref-text","修改后的对应参考文字。")
        page.click("#save-ref-text")
        expect(page.locator("#message")).to_contain_text("参考文字已保存")
        page.locator("[data-select-voice]").first.click()
        page.click("#preview-voice")
        page.wait_for_function("document.querySelector('#selected-voice-preview').readyState >= 1")
        assert page.locator("#selected-voice-preview").evaluate("node => node.duration")>0
        page.locator("#selected-voice-preview").evaluate("node => node.pause()")
        # Ordinary articles must reach the plain-text contract intact, including when
        # pasted into the director editor. Malformed structured inputs remain errors.
        compiled_requests=[]
        page.on("request",lambda request:compiled_requests.append(request.post_data_json) if request.url.endswith("/v1/narrations/compile") and request.method=="POST" else None)
        page.click("#compile-button")
        expect(page.locator("#message")).to_contain_text("请先输入要配音的正文")
        assert not compiled_requests
        # A slow automatic preflight remains locked across health polls, and an edit
        # during that preflight must never submit the stale document.
        held=[]
        submissions=[]
        page.on("request",lambda request:submissions.append(request.post_data_json) if request.url.endswith("/v1/narrations") and request.method=="POST" else None)
        page.fill("#text-editor","预检期间的原始正文。")
        page.route("**/v1/narrations/compile",lambda route:held.append(route))
        page.click("#submit-button")
        expect(page.locator("#submit-button")).to_be_disabled()
        page.wait_for_timeout(3200)
        expect(page.locator("#submit-button")).to_be_disabled()
        assert len(held)==1
        page.fill("#text-editor","不需要先手动预检，也能直接生成。")
        held[0].continue_()
        expect(page.locator("#message")).to_contain_text("预检期间文稿或设置发生变化")
        assert not submissions
        page.unroute("**/v1/narrations/compile")
        page.locator('[data-speed-step="-0.05"]').click()
        page.locator('[data-speed-step="-0.05"]').click()
        expect(page.locator("#text-speed")).to_have_value("0.90")
        page.click("#submit-button")
        expect(page.locator("#active-job")).to_contain_text("已完成",timeout=60000)
        assert submissions[-1]["text"]=="不需要先手动预检，也能直接生成。"
        assert submissions[-1]["speed"]==.9
        expect(page.locator("#plan-review")).to_be_hidden()
        assert page.locator("#playback-select").count()==0
        expect(page.locator("#compare-disclosure")).not_to_have_attribute("open","")
        page.fill("#text-editor","")
        page.fill("#text-speed","1")
        article=("Intel Core i5-9400T 在规格和功耗特性上，介于 i3-9100T 和 i5-9600T 之间。\n"
                 "标称设计功耗（TDP）为 35W。这是一份用于验证文本入口的文稿。\n"
                 "* 本地配音：保留  ALL IN ONE、NAS 和 Linux 这些文字，以及原有换行。\n")*4
        page.click('[data-mode="director"]')
        page.fill("#director-editor",article)
        with page.expect_response(lambda response:response.url.endswith("/v1/narrations/compile") and response.request.method=="POST") as compiled:
            page.click("#compile-button")
        plan=compiled.value.json()
        assert plan["valid"] and len(plan["units"])>1
        assert "".join(u["text"] for u in plan["units"])==article
        assert compiled_requests[-1]["text"]==article and "narration_director" not in compiled_requests[-1]
        expect(page.locator("#tab-text")).to_have_attribute("aria-selected","true")
        expect(page.locator("#text-editor")).to_have_value(article)
        expect(page.locator("#director-editor")).to_have_value(article)
        expect(page.locator("#message")).to_contain_text("已识别为普通文本")
        page.click("#submit-button")
        expect(page.locator("#active-job")).to_contain_text("已完成",timeout=60000)
        page.click("#audit-disclosure > summary")
        with page.expect_download() as downloaded:
            page.click('[data-download="source-input"]')
        source_path=args.output/"plain-source-input.json"
        downloaded.value.save_as(source_path)
        assert json.loads(source_path.read_text())["text"]==article
        page.click('[data-mode="director"]')
        for invalid in ['{\n "pages": [\n}', '[]', 'null', '"一段文字"']:
            previous=len(compiled_requests)
            page.fill("#director-editor",invalid)
            page.click("#compile-button")
            expect(page.locator("#message")).to_contain_text("JSON")
            assert "Unexpected token" not in page.locator("#message").inner_text()
            expect(page.locator("#tab-director")).to_have_attribute("aria-selected","true")
            assert len(compiled_requests)==previous
        # Do not silently drop director controls or overwrite another text draft.
        page.fill("#director-editor",article+"另一个文稿。")
        page.click("#profile-disclosure > summary")
        page.fill("#profile-editor",'{"rate":"-5%"}')
        page.click("#compile-button")
        expect(page.locator("#message")).to_contain_text("已有参数")
        expect(page.locator("#profile-editor")).to_have_value('{"rate":"-5%"}')
        page.fill("#profile-editor","{}")
        page.click("#compile-button")
        expect(page.locator("#message")).to_contain_text("已有另一份正文")
        expect(page.locator("#text-editor")).to_have_value(article)
        page.click("#profile-disclosure > summary")
        page.click("#load-example")
        expect(page.locator("#director-editor")).to_have_value(__import__("re").compile(r'.*"pages".*',__import__("re").S))
        before=len(submissions)
        page.click("#submit-button")
        expect(page.locator("#plan-summary")).to_contain_text("预检未通过")
        expect(page.locator("#plan-review")).to_be_visible()
        assert len(submissions)==before
        page.check("#allow-degraded")
        page.click("#compile-button")
        expect(page.locator("#plan-summary")).to_contain_text("预检通过")
        expect(page.locator(".unit")).to_have_count(4)
        expect(page.locator(".plan-page")).to_have_count(2)
        # Editing invalidates the displayed plan and prevents stale auditions.
        page.fill("#director-editor",page.input_value("#director-editor")+"\n")
        expect(page.locator("#plan-summary")).to_contain_text("请重新预检")
        expect(page.locator("[data-audition]").first).to_be_disabled()
        page.click("#compile-button")
        expect(page.locator("#plan-summary")).to_contain_text("预检通过")
        page.locator("#director-editor").evaluate("node => node.scrollTop = 0")
        page.evaluate("window.scrollTo(0,0)")
        page.screenshot(path=str(args.output/"studio-preview.png"),full_page=True)
        page.click("#submit-button")
        expect(page.locator("#active-job")).to_contain_text("已完成",timeout=60000)
        expect(page.locator("#full-audio")).to_have_attribute("src",__import__("re").compile(r"blob:.*"),timeout=20000)
        page.wait_for_function("document.querySelector('#full-audio').readyState >= 1")
        page.click("#full-audio")
        assert page.locator("#full-audio").evaluate("node => node.duration")>0
        expect(page.locator("#raw-audio")).to_have_attribute("src",__import__("re").compile(r"blob:.*"))
        expect(page.locator("#voice-select option:checked")).to_contain_text("已就绪")
        page.click("#audit-disclosure > summary")
        with page.expect_download() as downloaded:
            page.click('[data-download="timeline"]')
        timeline_path=args.output/"downloaded-timeline.json"
        downloaded.value.save_as(timeline_path)
        timeline=json.loads(timeline_path.read_text())
        assert len(timeline["pages"])==2
        page.locator("#director-editor").evaluate("node => node.scrollTop = 0")
        page.evaluate("window.scrollTo(0,0)")
        page.screenshot(path=str(args.output/"studio-completed.png"),full_page=True)
        # Verify the independent SSML path and single-segment audition controls.
        page.click('[data-mode="ssml"]')
        page.click("#load-ssml")
        expect(page.locator("#ssml-editor")).to_have_value(__import__("re").compile(r".*<speak.*",__import__("re").S))
        assert json.loads(page.input_value("#voice-mapping"))["reference-speaker"] == page.input_value("#voice-select")
        mapping=page.input_value("#voice-mapping")
        page.click("#mode-ssml .disclosure > summary")
        page.fill("#voice-mapping",'{"reference-speaker":')
        page.click("#compile-button")
        expect(page.locator("#message")).to_contain_text("音色名称映射 JSON 格式有误")
        page.fill("#voice-mapping",mapping)
        page.click("#mode-ssml .disclosure > summary")
        page.click("#compile-button")
        expect(page.locator("#plan-summary")).to_contain_text("预检通过")
        with page.expect_response(lambda response: response.url.endswith("/v1/narrations") and response.request.method=="POST" and response.status==202) as submitted:
            page.locator('[data-audition="1:1"]').first.click()
        audition_job_id=submitted.value.json()["job_id"]
        expect(page.locator("#active-job")).to_contain_text(audition_job_id,timeout=20000)
        expect(page.locator("#active-job")).to_contain_text("已完成",timeout=60000)
        expect(page.locator("#full-audio")).to_have_attribute("src",__import__("re").compile(r"blob:.*"),timeout=20000)
        page.click('[data-view="jobs"]')
        expect(page.locator("#job-list tr")).not_to_have_count(0)
        page.click('[data-view="runtime"]')
        expect(page.locator('[data-capability="instruction_control"]')).to_contain_text("开心地读")
        expect(page.locator('[data-capability="instruction_control"]')).to_contain_text("停顿仍可单独设置")
        expect(page.locator('[data-capability="word_alignment"]')).to_contain_text("每个词在音频中的精确起止时间")
        expect(page.locator('[data-capability="word_alignment"]')).to_contain_text("按生成片段记录")
        expect(page.locator('[data-capability="instruction_control"] .badge')).not_to_have_class(__import__("re").compile(r".*failed.*"))
        page.screenshot(path=str(args.output/"capabilities.png"),full_page=True)
        page.click("#diagnostic-disclosure > summary")
        expect(page.locator("#runtime-detail")).to_contain_text("controlled-test-tone")
        # Adding a WAV from the studio opens the picker immediately, derives the
        # name, chooses the no-transcript mode, and returns with that voice selected.
        page.click('[data-view="studio"]')
        with page.expect_file_chooser() as chooser:
            page.click("#go-upload")
        chooser.value.set_files(str(sample.resolve()))
        expect(page.locator("#voice-name")).to_have_value("controlled-tone")
        page.locator('#voice-form button[type="submit"]').click()
        expect(page.locator("#view-studio")).to_be_visible()
        expect(page.locator("#voice-select option:checked")).to_contain_text("controlled-tone")
        selected=page.input_value("#voice-select")
        voices=page.request.get(args.url+"/v1/voices",headers={"Authorization":"Bearer ui-test-token"}).json()["voices"]
        assert next(v for v in voices if v["voice_id"]==selected)["mode"]=="x_vector_only"
        page.set_viewport_size({"width":390,"height":844})
        page.click('[data-view="voices"]')
        expect(page.locator('[data-copy-voice-id="controlled-tone"]')).to_be_visible()
        page.screenshot(path=str(args.output/"voice-ids-mobile.png"),full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        nav_tops=page.locator(".nav").evaluate_all("nodes => nodes.map(n => n.getBoundingClientRect().top)")
        assert len(nav_tops)==5 and max(nav_tops)-min(nav_tops)<1
        page.click('[data-view="mcp"]')
        expect(page.locator("#view-mcp")).to_be_visible()
        page.screenshot(path=str(args.output/"mcp-mobile.png"),full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.click('[data-view="studio"]')
        page.screenshot(path=str(args.output/"mobile.png"),full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        browser.close()
    assert not errors,errors
    (args.output/"ui-smoke.json").write_text(json.dumps({"passed":True,"model_source":"controlled-test-tone","qwen_inference_verified":False,"javascript_errors":errors,"workflows":["mcp-authentication","mcp-discovery-and-schema","mcp-config-copy-without-secrets","mcp-missing-entry","mcp-auth-mode-config","mcp-without-inference","upload","semantic-voice-id","voice-id-visible-and-copyable","voice-id-copy-without-clipboard-api","voice-id-mobile-width","auto-voice-name-and-mode","direct-upload-and-return","inline-reference-preview","ref-text-dialog","default-plain-text","automatic-preflight","slow-preflight-lock","edited-input-preservation","explicit-degradation-review","speed-stepper","article-auto-detection","plain-source-preservation","malformed-json-localization","draft-and-profile-preservation","director-compile","whole-narration","blob-audio-metadata","authenticated-download","ssml-compile","segment-audition","jobs","capability-explanations","mobile-five-item-navigation","mcp-mobile-width","mobile-width"]},ensure_ascii=False,indent=2))
    print("UI_SMOKE_PASS (controlled service backend; no GPU/audio-quality acceptance)")


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--url",default="http://127.0.0.1:12308")
    parser.add_argument("--output",type=Path,default=Path("test-results/ui"))
    parser.add_argument("--browser-executable",help="Optional installed Chromium executable")
    main(parser.parse_args())
