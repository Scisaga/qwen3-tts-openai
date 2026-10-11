import copy
import json
import threading
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import create_app
from contracts import NarrationRequest
from errors import ServiceError
from job_store import JobStore
from storage import Database, file_hash
from synthesis_service import SynthesisService
from tests.conftest import ControlledBackend, wait_done


def register(service, sample, name="声音 A"):
    voice=service.register_voice(sample,name,"参考测试文字。","icl")
    job=wait_done(service,voice["preparation_job_id"])
    assert job["state"] == "succeeded",job["error"]
    return voice


def produce(service, director, profile, voice, **kwargs):
    request=NarrationRequest(narration_director=director,voice_profile=profile,voice_id=voice["voice_id"],allow_degraded=True,**kwargs)
    job=service.submit(request)
    done=wait_done(service,job["job_id"])
    assert done["state"] == "succeeded",done["error"]
    manifest=json.loads(service.artifact(job["job_id"],"manifest")[0].read_text())
    return done,manifest


def test_cache_layers_incremental_edits_and_pristine_audio(settings,sample,director,profile):
    backend=ControlledBackend()
    service=SynthesisService(settings,backend)
    service.start()
    try:
        voice=register(service,sample)
        first,m1=produce(service,director,profile,voice)
        assert len(backend.calls) == 4
        second,m2=produce(service,director,profile,voice)
        assert len(backend.calls) == 4
        assert m2["assembly_cache_hit"] and all(u["raw_cache_hit"] and u["processed_cache_hit"] for u in m2["units"])
        changed=copy.deepcopy(profile)
        changed["rate"]="-10%"
        changed["page_break_ms"]=250
        _,m3=produce(service,director,changed,voice)
        assert len(backend.calls) == 4
        assert all(u["raw_cache_hit"] and not u["processed_cache_hit"] for u in m3["units"])
        for idx in range(4):
            assert m1["units"][idx]["raw_key"] == m3["units"][idx]["raw_key"]
        edited=copy.deepcopy(director)
        edited["pages"][1]["segments"][0]["text"] += "只修改这一段。"
        _,m4=produce(service,edited,changed,voice)
        assert len(backend.calls) == 5
        assert sum(not u["raw_cache_hit"] for u in m4["units"]) == 1
        changed["pronunciations"]["unused"]={"alias":"未使用规则"}
        _,m5=produce(service,edited,changed,voice)
        assert len(backend.calls) == 5
        assert all(u["raw_cache_hit"] for u in m5["units"])
        raw=settings.data_dir/"cache/raw"/(m5["units"][0]["raw_key"]+".wav")
        raw.write_bytes(b"corrupted")
        _,m6=produce(service,edited,changed,voice)
        assert len(backend.calls) == 6 and not m6["units"][0]["raw_cache_hit"]
        assert service.artifact(first["id"],"full-mp3")[0].is_file()
        assert second["result"]["artifacts"]["full-mp3"]["sha256"] == file_hash(service.artifact(second["id"],"full-mp3")[0])
    finally:
        service.close()


def test_voice_switch_snapshot_and_seed_invalidation(settings,sample,director,profile):
    backend=ControlledBackend()
    service=SynthesisService(settings,backend)
    service.start()
    try:
        sample_b=sample.with_name("sample-b.wav")
        sf.write(sample_b,.1*np.sin(np.arange(48000)*2*np.pi*660/24000),24000)
        a,b=register(service,sample,"声音 A"),register(service,sample_b,"声音 B")
        service.edit_voice(b["voice_id"],{"ref_text":"另一段对应参考文字。"})
        b=service.voices.get(b["voice_id"])
        director["pages"][0]["segments"][1]["voice_id"]=b["voice_id"]
        _,manifest=produce(service,director,profile,a)
        assert backend.calls[1]["unit"]["voice_snapshot"]["ref_text"] == b["ref_text"]
        assert backend.calls[0]["unit"]["voice_snapshot"]["voice_id"] == a["voice_id"]
        assert backend.calls[0]["unit"]["voice_snapshot"]["reference"]["original_sha256"] != backend.calls[1]["unit"]["voice_snapshot"]["reference"]["original_sha256"]
        assert manifest["units"][0]["raw_key"] != manifest["units"][1]["raw_key"]
        before=len(backend.calls)
        produce(service,director,profile,a,generation={"seed":3})
        assert len(backend.calls) == before+4
    finally:
        service.close()


def test_multi_page_timeline_uses_final_samples_and_mp3(settings,sample,director,profile):
    service=SynthesisService(settings,ControlledBackend())
    service.start()
    try:
        voice=register(service,sample)
        job,_=produce(service,director,profile,voice)
        timeline=json.loads(service.artifact(job["id"],"timeline")[0].read_text())
        assert timeline["mp3"]["decoded_frames"] == timeline["frames"]
        assert len(timeline["pages"]) == 2
        assert sum(p["mp3"]["decoded_frames"] for p in timeline["pages"]) == timeline["frames"]
        events=timeline["events"]
        assert all(a["end_frame"]==b["start_frame"] for a,b in zip(events,events[1:]))
        assert timeline["bookmarks"][1]["seconds"] == timeline["pages"][1]["start_seconds"]
        assert timeline["pages"][0]["target_seconds"] == 15
        assert timeline["pages"][0]["duration_seconds"] != 15
    finally:
        service.close()


def test_original_source_file_bytes_and_hash_survive(settings,sample,director,profile):
    service=SynthesisService(settings,ControlledBackend())
    service.start()
    try:
        voice=register(service,sample)
        raw="\ufeff"+json.dumps(director,ensure_ascii=False,indent=2).replace("\n","\r\n")
        job,manifest=produce(service,director,profile,voice,source_documents={"narration_director":raw})
        path,_=service.artifact(job["id"],"source-narration_director")
        assert path.read_bytes()==raw.encode("utf-8")
        assert manifest["source_documents"]["narration_director"]["sha256"] == file_hash(path)
    finally:
        service.close()


def test_running_and_queued_cancellation_does_not_kill_next_job(settings,sample):
    backend=ControlledBackend(delay=.4)
    service=SynthesisService(settings,backend)
    service.start()
    try:
        voice=register(service,sample)
        request=NarrationRequest(text="第一句。",voice_id=voice["voice_id"])
        first=service.submit(request)["job_id"]
        assert backend.generating.wait(5)
        queued=service.submit(request.model_copy(update={"text":"取消排队句。"}))["job_id"]
        last=service.submit(request.model_copy(update={"text":"最后一句。"}))["job_id"]
        assert service.cancel(queued)["status"] == "cancelled"
        service.cancel(first)
        assert wait_done(service,first)["state"] == "cancelled"
        assert wait_done(service,last)["state"] == "succeeded"
        assert len(backend.calls)==2
        assert service.jobs.get(first)["result"] is None
    finally:
        service.close()


def test_durable_restart_and_queue_bound(settings):
    store=JobStore(Database(settings.data_dir/"metadata.sqlite3"),2)
    running=store.submit("narration",{})["id"]
    store.claim()
    queued=store.submit("narration",{})["id"]
    store.submit("narration",{})
    with pytest.raises(ServiceError) as caught:
        store.submit("narration",{})
    assert caught.value.code == "queue_full"
    store.recover()
    assert store.get(running)["state"] == "failed"
    assert store.get(running)["error"]["type"] == "service_restarted"
    assert store.get(queued)["state"] == "queued"


def test_single_holder_lock_and_preload_does_not_block_http(settings):
    gate=threading.Event()
    backend=ControlledBackend(preload_gate=gate)
    app=create_app(settings.model_copy(update={"preload_model":True}),backend)
    with TestClient(app) as client:
        start=time.monotonic()
        assert client.get("/").status_code==200
        assert client.get("/health").json()["model"]["state"] in {"unloaded","loading"}
        assert client.get("/ready").status_code==503
        assert time.monotonic()-start < 1
        another=SynthesisService(settings,ControlledBackend())
        with pytest.raises(RuntimeError,match="already owned"):
            another.start()
        gate.set()


def test_api_upload_compile_speech_and_authenticated_download(settings,sample,director,profile):
    settings=settings.model_copy(update={"api_token":"local-test-token"})
    app=create_app(settings,ControlledBackend())
    headers={"Authorization":"Bearer local-test-token"}
    with TestClient(app) as client:
        assert client.get("/health").status_code==200
        assert client.get("/v1/voices").status_code==401
        with sample.open("rb") as f:
            response=client.post("/v1/voices",files={"file":("sample.wav",f,"audio/wav")},data={"name":"本地声音","ref_text":"测试。"},headers=headers)
        assert response.status_code==202,response.text
        voice=response.json()
        wait_done(app.state.service,voice["preparation_job_id"])
        body={"narration_director":director,"voice_profile":profile,"voice_id":voice["voice_id"],"allow_degraded":True}
        assert client.post("/v1/narrations/compile",json=body,headers=headers).json()["valid"]
        body["allow_degraded"]=False
        assert client.post("/v1/narrations",json=body,headers=headers).status_code==422
        for fmt,magic in (("wav",b"RIFF"),("mp3",b"ID3")):
            response=client.post("/v1/audio/speech",json={"model":"qwen3-tts","input":"你好，本地配音。","voice":voice["voice_id"],"response_format":fmt,"speed":.9},headers=headers)
            assert response.status_code==200,response.text
            assert response.content.startswith(magic)
        response=client.post("/v1/audio/speech",json={"model":"qwen3-tts","input":"你好。","voice":voice["voice_id"],"instructions":"用愉快的语气说话"},headers=headers)
        assert response.status_code==422
        last=client.get("/v1/jobs",headers=headers).json()["jobs"][0]
        url=next(a["url"] for a in last["artifacts"] if a["artifact_id"]=="full-mp3")
        assert client.get(url).status_code==401
        assert client.get(url,headers=headers).status_code==200
        assert client.get(f"/v1/jobs/{last['job_id']}/artifacts/not-registered",headers=headers).status_code==404
        assert client.post("/admin/model/preload").status_code==403
        assert client.post("/v1/audio/speech",json={"model":"qwen3-tts","input":json.dumps({"pages":[]}),"voice":voice["voice_id"]},headers=headers).status_code==422


@pytest.mark.parametrize("name,payload",[("evil.pkl",b"pickle"),("sample.wav",b"RIFFxxxxWAVEbad"),("sample.wav",b"bad")])
def test_corrupt_uploads_and_non_wav_rejected(settings,name,payload):
    with TestClient(create_app(settings,ControlledBackend())) as client:
        response=client.post("/v1/voices",files={"file":(name,payload,"audio/wav")},data={"name":"test"})
        assert response.status_code in {400,422}
        assert client.get("/v1/voices").json()["voices"]==[]


def test_body_and_file_limits_include_chunked_requests(settings,sample):
    small=settings.model_copy(update={"max_upload_bytes":1024,"max_request_bytes":2048})
    with TestClient(create_app(small,ControlledBackend())) as client:
        response=client.post("/v1/voices",files={"file":("sample.wav",sample.read_bytes(),"audio/wav")},data={"name":"test"})
        assert response.status_code==413
        response=client.post("/v1/narrations",content=iter([b"x"*1024,b"x"*1025]),headers={"Content-Type":"application/json"})
        assert response.status_code==413
        assert client.post("/v1/narrations",json={"text":"hi","unknown":True}).status_code==422


def test_mcp_shared_service_tools_resources_and_no_audio_base64(settings):
    with TestClient(create_app(settings,ControlledBackend())) as client:
        headers={"Accept":"application/json, text/event-stream"}
        response=client.post("/mcp/",json={"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}},headers=headers)
        assert response.status_code==200,response.text
        assert {t["name"] for t in response.json()["result"]["tools"]}=={"submit_narration","get_job","list_voices"}
        response=client.post("/mcp/",json={"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_voices","arguments":{}}},headers=headers)
        assert response.status_code==200
        assert "base64" not in response.text
        response=client.post("/mcp/",json={"jsonrpc":"2.0","id":3,"method":"resources/read","params":{"uri":"qwen3tts://health"}},headers=headers)
        content=response.json()["result"]["contents"][0]["text"]
        assert json.loads(content)["service"]==client.get("/health").json()["service"]
