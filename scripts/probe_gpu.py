"""Read-only GPU/build probe; never loads a model or changes drivers/services."""
import argparse
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Settings
from storage import write_json


def probe():
    settings = Settings.from_env()
    settings.configure_process()
    result = {"gpu_selection": settings.gpu_device, "requested_model": settings.model_id, "requested_revision": settings.model_revision, "dtype": settings.dtype, "attention": settings.attn_implementation, "python": sys.version.split()[0], "gpu_inference_verified": False}
    try:
        p = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used,driver_version", "--format=csv"], capture_output=True, text=True, timeout=10)
        result["nvidia_smi"] = {"exit_code": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        result["nvidia_smi"] = {"error": str(e)}
    path = Path("/proc/driver/nvidia/version")
    result["kernel_driver"] = path.read_text().strip() if path.is_file() else None
    result["proc_devices"] = [p.read_text().strip() for p in Path("/proc/driver/nvidia/gpus").glob("*/information")]
    try:
        import torch
        from qwen_tts import Qwen3TTSModel
        result["dependencies"] = {p: importlib.metadata.version(p) for p in ("torch", "torchaudio", "qwen-tts", "transformers", "accelerate", "gradio")}
        result["cuda_build"] = torch.version.cuda
        result["compiled_architectures"] = torch._C._cuda_getArchFlags()
        result["sm75_in_build"] = "sm_75" in (result["compiled_architectures"] or "")
        result["cuda_available"] = torch.cuda.is_available()
        result["official_import_verified"] = Qwen3TTSModel.__name__ == "Qwen3TTSModel"
        if result["cuda_available"]:
            from backends.qwen import OfficialQwen
            backend = OfficialQwen(settings)
            backend.verify_device()
            result["hardware"] = backend.hardware
    except Exception as e:
        result["error"] = {"type": type(e).__name__, "message": str(e)}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("test-results/gpu-probe.json"))
    args = parser.parse_args()
    result = probe()
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("cuda_available") and result.get("sm75_in_build") else 2)
