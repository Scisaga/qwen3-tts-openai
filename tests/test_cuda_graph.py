import pytest

from backends.cuda_graph import PredictorCudaGraph, PredictorSampling
from backends.qwen import OfficialQwen
from config import Settings
from errors import ServiceError
from storage import digest


@pytest.mark.parametrize("values", [
    {"attn_implementation": "eager"},
    {"backend": "qwen-combined-experimental"},
])
def test_graph_configuration_rejects_unsupported_paths(values):
    with pytest.raises(ValueError, match="CUDA_GRAPH_ENABLED requires"):
        Settings(cuda_graph_enabled=True, **values)


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("overridden", [False, True])
def test_predictor_override_is_local_and_restored_after_generation(fails, overridden):
    class Predictor:
        def generate(self):
            return "official"

    owned, other = Predictor(), Predictor()
    if overridden:
        owned.generate = lambda: "existing override"
    original = owned.generate
    graph = PredictorCudaGraph.__new__(PredictorCudaGraph)
    graph.predictor = owned
    graph.generate = lambda: "captured"
    try:
        with graph.activate():
            assert owned.generate() == "captured" and other.generate() == "official"
            if fails:
                raise RuntimeError("controlled failure")
    except RuntimeError:
        assert fails
    assert owned.generate == original
    assert ("generate" in owned.__dict__) == overridden


@pytest.mark.parametrize("changes", [
    {"max_new_tokens": 14}, {"do_sample": False}, {"top_p": .8},
    {"output_hidden_states": False}, {"return_dict_in_generate": False}, {"unexpected": True},
])
def test_predictor_rejects_changed_sdk_contract_instead_of_silent_fallback(changes):
    options = {"do_sample": True, "top_k": 50, "top_p": 1.0, "temperature": .9}
    graph = PredictorCudaGraph.__new__(PredictorCudaGraph)
    graph.codebooks = 15
    graph.sampling = PredictorSampling.from_options(options)
    arguments = {**options, "max_new_tokens": 15, "inputs_embeds": object(),
                 "output_hidden_states": True, "return_dict_in_generate": True, **changes}
    with pytest.raises(ServiceError) as error:
        graph.generate(**arguments)
    assert error.value.code == "cuda_graph_incompatible_call"


def test_graph_and_official_paths_have_separate_model_and_conditioning_cache_identity(settings, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    content = b"controlled weights identity fixture"
    blob = tmp_path / digest(content.decode())
    # Local paths intentionally use their own manifest, independent of hub blob names.
    (snapshot / "model.safetensors").write_bytes(content)
    configured = settings.model_copy(update={"model_id": str(snapshot)})
    official = OfficialQwen(configured)
    graph = OfficialQwen(configured.model_copy(update={"cuda_graph_enabled": True}))
    _, old_identity = official.snapshot("base")
    _, graph_identity = graph.snapshot("base")
    assert "cuda_graph" not in old_identity and graph_identity["cuda_graph"]
    assert old_identity["weights"] == graph_identity["weights"]
    assert digest(old_identity) != digest(graph_identity)
    voice = {"conditioning_key": "controlled-key", "reference": {"original_sha256": blob.name}, "mode": "icl"}
    assert official.prompt_key(voice) != graph.prompt_key(voice)
