import copy

import pytest

from contracts import NarrationRequest
from director_compiler import compile_narration
from tests.conftest import ControlledBackend


def compile(settings, director=None, profile=None, **kwargs):
    request = NarrationRequest(narration_director=director, voice_profile=profile or {}, voice_id="voice_test", **kwargs)
    return compile_narration(request, settings, ControlledBackend().capabilities, lambda ident: {"voice_id": ident, "conditioning_state": "ready"})


def test_actual_protocol_additive_controls_and_source_fidelity(settings, director, profile):
    director["pages"][0]["segments"][0]["text"] = "  TA1、TA15 和 AlSi10Mg。\n"
    profile["pronunciations"].update({"TA1": {"alias": "T A 一"}, "TA15": {"alias": "T A 十五"}})
    source = copy.deepcopy(director)
    result = compile(settings, director, profile, allow_degraded=True)
    assert result["valid"]
    unit = result["units"][0]
    assert unit["postprocessing"]["tempo"] == pytest.approx(0.93)
    assert result["units"][2]["postprocessing"]["tempo"] == pytest.approx(0.91)
    assert unit["text"] == "  TA1、TA15 和 AlSi10Mg。\n"
    assert unit["spoken_text"] == "  T A 一、T A 十五 和 铝硅十镁。\n"
    assert [m["original"] for m in unit["pronunciation_mapping"]] == ["TA1", "TA15", "AlSi10Mg"]
    assert director == source
    assert all("语气" not in u["spoken_text"] for u in result["units"])
    assert result["pages"][0]["role"] == source["pages"][0]["role"]
    assert result["rules"]["target_seconds"] == "reference only"


def test_defaults_match_inspected_skill(settings, director):
    director["pages"] = director["pages"][:1]
    director["pages"][0]["segments"] = [{"text": "保留默认值。"}]
    result = compile(settings, director, {"rate": "-5%"}, allow_degraded=True)
    assert result["units"][0]["postprocessing"]["tempo"] == 1
    assert [e["pause_ms"] for e in result["events"] if e["kind"] == "silence"] == [80, 120]


@pytest.mark.parametrize("global_pitch,local_pitch,valid", [("+0.1st", "+0.1st", False), ("-0.1st", "+0.1st", True), ("+0.2st", "-0.1st", False)])
def test_pitch_components_and_final_range(settings, director, global_pitch, local_pitch, valid):
    director["pages"] = director["pages"][:1]
    director["pages"][0]["segments"] = [{"text": "音高合并。", "pitch": local_pitch}]
    result = compile(settings, director, {"pitch": global_pitch}, allow_degraded=True)
    assert result["valid"] is valid
    assert result["units"][0]["postprocessing"]["pitch_semitones"] == pytest.approx(float(global_pitch[:-2]) + float(local_pitch[:-2]))


def test_generic_pitch_and_pause_are_explicit(settings, director):
    director["pages"][0]["segments"][0].update(pitch="+2st", pause_after_ms=500)
    assert not compile(settings, director, allow_degraded=True)["valid"]
    assert compile(settings, director, allow_degraded=True, compatibility="generic")["valid"]


@pytest.mark.parametrize("field,value", [("rate", "-100%"), ("rate", 1.2), ("pitch", "+1Hz"), ("pause_after_ms", True), ("pause_after_ms", 301)])
def test_bad_numeric_values_never_silently_clamp(settings, director, field, value):
    director["pages"][0]["segments"][0][field] = value
    assert not compile(settings, director, allow_degraded=True)["valid"]


def test_expression_and_unknown_control_preflight(settings, director):
    director["pages"][0]["segments"][0]["emphasis"] = "word"
    result = compile(settings, director)
    assert not result["valid"]
    assert {"instruction_unsupported", "unknown_control"} <= {i["code"] for i in result["issues"]}
    degraded = compile(settings, director, allow_degraded=True)
    assert degraded["valid"]
    assert all(not u["model_instruction"]["effective"] for u in degraded["units"])
    assert all(i["degraded"] for i in degraded["issues"])


def test_numeric_instruction_conflict_is_explicit(settings, director):
    caps = ControlledBackend().capabilities.copy()
    caps["instruction_control"] = "supported"
    director["pages"][0]["direction"] = "放慢语速，降低音高。"
    req = NarrationRequest(narration_director=director, voice_id="voice_test")
    result = compile_narration(req, settings, caps, lambda v: {"voice_id": v})
    assert not result["valid"]
    assert any(i["code"] == "instruction_numeric_conflict" for i in result["issues"])
    assert result["units"][0]["model_instruction"]["effective"] == ""


@pytest.mark.parametrize("rule,code", [({"phoneme":"x","alphabet":"ipa"},"phoneme_unsupported"), ({"say_as":"cardinal"},"say_as_unsupported")])
def test_unimplemented_pronunciations_reported(settings, director, rule, code):
    director["pages"][0]["segments"][0]["text"] = "测试 TA1。"
    result = compile(settings, director, {"pronunciations":{"TA1":rule}})
    assert any(i["code"] == code for i in result["issues"])
    degraded = compile(settings, director, {"pronunciations":{"TA1":rule}}, allow_degraded=True)
    assert degraded["units"][0]["spoken_text"] == "测试 TA1。"


def test_reference_voice_required_and_explicit_mapping(settings, director):
    req = NarrationRequest(narration_director=director, voice_profile={"provider":"azure-speech","voice":"zh-CN-XiaochenNeural"}, voice_mapping={"zh-CN-XiaochenNeural":"voice_local"}, allow_degraded=True)
    result = compile_narration(req, settings, ControlledBackend().capabilities, lambda v: {"voice_id":v})
    assert result["valid"] and result["units"][0]["voice_id"] == "voice_local"
    assert not compile_narration(req.model_copy(update={"voice_mapping":{}}), settings, ControlledBackend().capabilities, lambda v: (_ for _ in ()).throw(__import__("errors").ServiceError("voice_not_found","Unknown voice")))["valid"]


def test_long_text_split_keeps_all_characters(settings):
    text = "这是第一句。" * 50
    result = compile(settings.model_copy(update={"max_unit_chars":32}), text=text)
    assert result["valid"]
    assert "".join(u["text"] for u in result["units"]) == text
    assert all(len(u["text"]) <= 32 for u in result["units"])
    assert not compile(settings.model_copy(update={"max_unit_chars":32}), text="字"*33)["valid"]


def test_ssml_namespaces_inheritance_bookmarks_and_alias(settings):
    xml = '<speak xmlns="http://www.w3.org/2001/10/synthesis"><voice name="reference"><bookmark mark="page-01"/><p><prosody rate="-10%" pitch="+0.1st" volume="-1dB"><prosody rate="+10%" pitch="+0.2st" volume="-2dB">测试<sub alias="铝硅十镁">AlSi10Mg</sub>。</prosody></prosody></p><break time="0.18s"/><bookmark mark="page-02"/><s>第二页。</s></voice></speak>'
    result = compile(settings, ssml=xml, voice_mapping={"reference":"voice_test"})
    assert result["valid"]
    unit = result["units"][0]
    assert unit["postprocessing"] == {"tempo":pytest.approx(.99),"pitch_semitones":pytest.approx(.3),"gain_db":-3}
    assert unit["text"] == "测试AlSi10Mg。"
    assert unit["spoken_text"] == "测试铝硅十镁。"
    assert [p["page"] for p in result["pages"]] == [1,2]
    assert [e["pause_ms"] for e in result["events"] if e["kind"] == "silence"] == [180]


@pytest.mark.parametrize("xml,code", [('<!DOCTYPE speak [<!ENTITY e SYSTEM "file:///etc/passwd">]><speak>&e;</speak>',"unsafe_or_invalid_xml"), ('<speak><audio src="https://example.com/a.wav"/></speak>',"ssml_element_unsupported"), ('<speak><prosody contour="(0%,+1st)">测试。</prosody></speak>',"unknown_control"), ('<speak><phoneme alphabet="ipa" ph="x">测试。</phoneme></speak>',"ssml_element_unsupported"), ('<speak xmlns="https://evil.example"><p>测试。</p></speak>',"ssml_namespace")])
def test_ssml_unsupported_and_security(settings, xml, code):
    result = compile(settings, ssml=xml)
    assert not result["valid"] and any(i["code"] == code for i in result["issues"])


def test_ssml_sentence_local_controls_need_alignment(settings):
    result = compile(settings, ssml='<speak><p>一句中的<prosody rate="-10%">局部</prosody>控制。</p></speak>', allow_degraded=True)
    assert not result["valid"] and any(i["code"] == "inline_alignment_unsupported" for i in result["issues"])


def test_ssml_long_paragraph_preserves_alias_projection(settings):
    text = '第一句。' * 20 + '<sub alias="读法">TERM</sub>。' + '最后一句。' * 20
    result = compile(settings.model_copy(update={"max_unit_chars":32}), ssml='<speak><p>'+text+'</p></speak>')
    assert result["valid"]
    assert ''.join(u["text"] for u in result["units"]) == '第一句。'*20+'TERM。'+'最后一句。'*20
    assert ''.join(u["spoken_text"] for u in result["units"]) == '第一句。'*20+'读法。'+'最后一句。'*20


def test_inline_bookmark_requires_alignment(settings):
    result = compile(settings, ssml='<speak>一句中的<bookmark mark="inside"/>书签。</speak>')
    assert not result["valid"] and any(i["code"] == "inline_bookmark_unsupported" for i in result["issues"])


def test_ssml_locale_and_explicit_language_conflict(settings):
    xml='<speak xml:lang="en-US"><p>Hello.</p></speak>'
    result=compile(settings,ssml=xml)
    assert result["valid"] and result["language"]=="English"
    result=compile(settings,ssml=xml,language="Chinese")
    assert not result["valid"] and any(i["code"]=="language_conflict" for i in result["issues"])


def test_selection_preserves_source_positions(settings, director):
    result = compile(settings, director, allow_degraded=True, selection={"pages":[2],"segments":[2]})
    assert result["valid"] and len(result["units"]) == 1
    assert result["units"][0]["page"] == 2 and result["units"][0]["segment"] == 2
    assert not compile(settings, director, allow_degraded=True, selection={"pages":[2],"segments":[9]})["valid"]


def test_source_document_cannot_disagree_with_compiled_object(director):
    import json
    req = NarrationRequest(narration_director=director, source_documents={"narration_director":"\ufeff"+json.dumps(director,ensure_ascii=False,indent=2)})
    assert req.source_documents["narration_director"].startswith("\ufeff")
    with pytest.raises(ValueError,match="same supplied object"):
        NarrationRequest(narration_director=director,source_documents={"narration_director":"{}"})


def test_combined_requires_opt_in_and_explicit_icl_degradation(settings):
    from backends.qwen import capabilities
    settings=settings.model_copy(update={"backend":"qwen-combined-experimental"})
    request=NarrationRequest(text="组合实验。",voice_id="voice_test",allow_experimental=True)
    def resolve(v):
        return {"voice_id":v,"mode":"icl"}
    plan=compile_narration(request,settings,capabilities(settings),resolve)
    assert not plan["valid"]
    assert any(i["code"]=="reference_mode_unsupported" for i in plan["issues"])
    request=request.model_copy(update={"allow_degraded":True})
    assert compile_narration(request,settings,capabilities(settings),resolve)["valid"]
    request=request.model_copy(update={"allow_experimental":False})
    assert not compile_narration(request,settings,capabilities(settings),resolve)["valid"]
