"""End-to-end real HTTP smoke test. Supply two authorized recordings, never test tones."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qwen3_tts_openai.storage import file_hash, write_json


def main(args):
    args.output.mkdir(parents=True,exist_ok=True)
    token=os.getenv("API_TOKEN","")
    result={"scope":"real GPU smoke; qualitative acceptance requires listening","references":[],"cases":[],"listening_review":"pending"}
    with httpx.Client(base_url=args.base_url,headers={"Authorization":"Bearer "+token} if token else {},timeout=60,trust_env=False) as client:
        def call(method,path,**kwargs):
            response=client.request(method,path,**kwargs)
            response.raise_for_status()
            return response.json()
        def wait(ident):
            started=time.monotonic()
            while time.monotonic()-started<args.timeout:
                job=call("GET",f"/v1/jobs/{ident}")
                if job["status"] in {"succeeded","failed","cancelled"}:
                    if job["status"]!="succeeded":
                        raise RuntimeError(json.dumps(job,ensure_ascii=False))
                    return job
                time.sleep(1)
            raise TimeoutError(f"Job {ident} still running; cancel explicitly through its API")
        try:
            result["initial_health"]=call("GET","/health")
            combined = result["initial_health"]["model"]["capabilities"]["combined_clone_instruction"]=="experimental"
            if combined and not args.allow_experimental:
                raise RuntimeError("Combined backend requires --allow-experimental for the smoke test")
            voice_ids=[]
            for sample,text,name in ((args.sample_a,args.ref_text_a,args.name_a),(args.sample_b,args.ref_text_b,args.name_b)):
                with sample.open("rb") as f:
                    voice=call("POST","/v1/voices",files={"file":(sample.name,f,"audio/wav")},data={"name":name,"ref_text":text,"mode":"x_vector_only" if combined or not text else "icl"})
                wait(voice["preparation_job_id"])
                voice_ids.append(voice["voice_id"])
                result["references"].append({"name":name,"voice_id":voice["voice_id"],"sha256":file_hash(sample),"ref_text":text})
                print(f"Reference prepared: {name} ({voice['voice_id']})",flush=True)
            original=json.loads((Path(__file__).resolve().parents[1]/"examples/narration_director.json").read_text())
            profile=json.loads((Path(__file__).resolve().parents[1]/"examples/voice_profile.json").read_text())
            original["pages"][1]["voice_id"]=voice_ids[1]
            cases=[("clone-A",{"text":"你好，这是一段中文克隆测试。","voice_id":voice_ids[0]}),("clone-B",{"text":"你好，这是一段中文克隆测试。","voice_id":voice_ids[1]}),("multi-page",{"narration_director":original,"voice_profile":profile,"voice_id":voice_ids[0],"allow_degraded":True}),("multi-page-cache-repeat",{"narration_director":original,"voice_profile":profile,"voice_id":voice_ids[0],"allow_degraded":True})]
            caps=result["initial_health"]["model"]["capabilities"]
            if caps["combined_clone_instruction"]=="experimental":
                if not args.allow_experimental:
                    raise RuntimeError("Combined backend requires --allow-experimental for the smoke test")
                cases.append(("expressive-A",{"text":"今天终于见到你了，我有很多事情想和你分享。","voice_profile":{"instructions":"用非常愉快、兴奋的语气说话。"},"voice_id":voice_ids[0]}))
            else:
                result["combined_expression_acceptance"]="not-run: Base capability is unsupported; run the independent experiment"
            for name,body in cases:
                body["allow_experimental"]=args.allow_experimental
                started=time.monotonic()
                print(f"Starting {name}",flush=True)
                job=wait(call("POST","/v1/narrations",json=body)["job_id"])
                folder=args.output/name
                folder.mkdir(exist_ok=True)
                for artifact in job["artifacts"]:
                    response=client.get(artifact["url"])
                    response.raise_for_status()
                    extension=".json" if artifact["media_type"]=="application/json" else ".mp3" if artifact["media_type"]=="audio/mpeg" else ".wav"
                    (folder/(artifact["artifact_id"]+extension)).write_bytes(response.content)
                manifest=json.loads((folder/"manifest.json").read_text())
                result["cases"].append({"name":name,"job":job,"elapsed_seconds":time.monotonic()-started,"manifest":manifest})
                print(f"Completed {name} in {time.monotonic()-started:.2f}s",flush=True)
            result["final_health"]=call("GET","/health")
            result["execution"]="completed-awaiting-listening"
        except Exception as e:
            result["execution"]="failed"
            result["error"]={"type":type(e).__name__,"message":str(e)}
            raise
        finally:
            write_json(args.output/"smoke.json",result)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url",default="http://127.0.0.1:12302")
    parser.add_argument("--sample-a",type=Path,required=True)
    parser.add_argument("--sample-b",type=Path,required=True)
    parser.add_argument("--ref-text-a",default="")
    parser.add_argument("--ref-text-b",default="")
    parser.add_argument("--name-a",default="smoke-A")
    parser.add_argument("--name-b",default="smoke-B")
    parser.add_argument("--allow-experimental",action="store_true")
    parser.add_argument("--timeout",type=int,default=3600)
    parser.add_argument("--output",type=Path,default=Path("test-results/gpu-smoke"))
    main(parser.parse_args())
