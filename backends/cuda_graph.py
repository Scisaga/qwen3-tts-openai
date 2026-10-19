"""Owned-model CUDA graph for Qwen's fixed residual-codebook prediction loop.

The static-cache capture strategy is adapted from Faster Qwen3-TTS, MIT licensed.
See NOTICE and licenses/faster-qwen3-tts.txt. This adapter targets the project's
pinned qwen-tts / Transformers versions and leaves outer generation to the SDK.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

from errors import ServiceError

CUDA_GRAPH_VERSION = "qwen-predictor-static-v1"


@dataclass(frozen=True)
class PredictorSampling:
    do_sample: bool
    top_k: int
    top_p: float
    temperature: float

    @classmethod
    def from_options(cls, options):
        return cls(options["do_sample"], options["top_k"], options["top_p"], options["temperature"])


class PredictorCudaGraph:
    def __init__(self, predictor):
        import torch
        from transformers import StaticCache

        self.torch = torch
        self.predictor = predictor
        self.model = predictor.model
        self.config = self.model.config
        self.projection = predictor.small_to_mtp_projection
        parameter = next(predictor.parameters())
        self.device, self.dtype = parameter.device, parameter.dtype
        if self.device.type != "cuda" or self.dtype != torch.float16:
            raise ServiceError("cuda_graph_incompatible_model", "CUDA graph requires a CUDA FP16 predictor", 503)
        self.codebooks = self.config.num_code_groups - 1
        if self.codebooks != 15 or self.model.has_sliding_layers:
            raise ServiceError("cuda_graph_incompatible_model", "CUDA graph requires 16 code groups and full attention", 503)
        self.capacity = self.codebooks + 2
        self.cache = StaticCache(config=self.config, max_cache_len=self.capacity)
        heads = self.config.num_key_value_heads
        dummy = torch.zeros(1, heads, 1, self.config.head_dim, dtype=self.dtype, device=self.device)
        # Transformers 4.57.3 StaticLayer takes one tensor for lazy initialization.
        for layer in self.cache.layers:
            layer.lazy_initialization(dummy)
        talker_hidden = predictor.get_input_embeddings()[0].embedding_dim
        self.input = torch.zeros(1, 2, talker_hidden, dtype=self.dtype, device=self.device)
        self.output = torch.zeros(1, self.codebooks, dtype=torch.long, device=self.device)
        self.positions = [torch.arange(2, device=self.device)] + [
            torch.tensor([index + 1], device=self.device) for index in range(1, self.codebooks)
        ]
        keys = torch.arange(self.capacity, device=self.device)
        self.masks = [{"full_attention": torch.where(
            keys[None, :] <= positions[:, None],
            torch.zeros((), dtype=self.dtype, device=self.device),
            torch.full((), torch.finfo(self.dtype).min, dtype=self.dtype, device=self.device),
        )[None, None, :, :]} for positions in self.positions]
        self.graph = None
        self.sampling = None
        self.processors = []
        self.capture_count = 0
        self.replay_count = 0
        self.capture_seconds = 0.0

    def _sample(self, logits):
        torch = self.torch
        # GenerationMixin converts final logits to FP32 before applying warpers.
        scores = logits[:, -1, :].float()
        if not self.sampling.do_sample:
            return scores.argmax(dim=-1, keepdim=True)
        for processor in self.processors:
            scores = processor(None, scores)
        return torch.multinomial(torch.softmax(scores, dim=-1), num_samples=1)

    def _loop(self):
        self.cache.reset()
        hidden = self.projection(self.input)
        for index in range(self.codebooks):
            result = self.model(inputs_embeds=hidden, attention_mask=self.masks[index],
                                position_ids=self.positions[index][None, :],
                                past_key_values=self.cache, cache_position=self.positions[index],
                                use_cache=True, output_hidden_states=False, return_dict=True)
            token = self._sample(self.predictor.lm_head[index](result.last_hidden_state))
            self.output[:, index:index + 1].copy_(token)
            if index + 1 < self.codebooks:
                hidden = self.projection(self.predictor.get_input_embeddings()[index](token))

    def configure(self, options):
        sampling = PredictorSampling.from_options(options)
        if self.graph is not None and sampling == self.sampling:
            return 0.0
        torch = self.torch
        from transformers.generation.logits_process import TemperatureLogitsWarper, TopKLogitsWarper, TopPLogitsWarper

        # One graph per owned model, so parameter changes cannot grow an unbounded cache.
        self.graph = None
        self.sampling = sampling
        self.processors = []
        if sampling.do_sample:
            if sampling.temperature != 1.0:
                self.processors.append(TemperatureLogitsWarper(sampling.temperature))
            self.processors.append(TopKLogitsWarper(sampling.top_k))
            if sampling.top_p < 1.0:
                self.processors.append(TopPLogitsWarper(sampling.top_p))
        started = time.monotonic()
        try:
            with torch.inference_mode(), torch.random.fork_rng(devices=[self.device.index]):
                stream = torch.cuda.Stream(device=self.device)
                stream.wait_stream(torch.cuda.current_stream(self.device))
                with torch.cuda.stream(stream):
                    for _ in range(3):
                        self._loop()
                torch.cuda.current_stream(self.device).wait_stream(stream)
                stream.synchronize()
                graph = torch.cuda.CUDAGraph()
                with torch.cuda.graph(graph, stream=stream):
                    self._loop()
                torch.cuda.current_stream(self.device).wait_stream(stream)
                stream.synchronize()
                self.graph = graph
        except Exception as error:
            self.graph = None
            raise ServiceError("cuda_graph_failed", "CUDA graph capture failed; disable CUDA_GRAPH_ENABLED to use the official path", 503,
                               {"exception": type(error).__name__, "reason": str(error)[-1000:]}) from error
        elapsed = time.monotonic() - started
        self.capture_count += 1
        self.capture_seconds += elapsed
        return elapsed

    def generate(self, **kwargs):
        expected = {"inputs_embeds", "max_new_tokens", "do_sample", "top_p", "top_k", "temperature",
                    "output_hidden_states", "return_dict_in_generate"}
        if (set(kwargs) != expected or kwargs["max_new_tokens"] != self.codebooks
                or not kwargs["return_dict_in_generate"] or not kwargs["output_hidden_states"]
                or PredictorSampling.from_options(kwargs) != self.sampling):
            raise ServiceError("cuda_graph_incompatible_call", "Unexpected predictor SDK arguments; refusing to change generation semantics", 503)
        value = kwargs["inputs_embeds"]
        if value.shape != self.input.shape or value.dtype != self.dtype or value.device != self.device:
            raise ServiceError("cuda_graph_incompatible_call", "Unexpected predictor input shape, dtype or device", 503)
        with self.torch.inference_mode():
            self.input.copy_(value)
            self.graph.replay()
            self.replay_count += 1
            # SDK consumes sequences only. Clone because the graph reuses its output storage.
            return SimpleNamespace(sequences=self.output.clone())

    @contextmanager
    def activate(self):
        missing = object()
        original = self.predictor.__dict__.get("generate", missing)
        self.predictor.generate = self.generate
        try:
            yield
        finally:
            if original is missing:
                del self.predictor.generate
            else:
                self.predictor.generate = original

    def status(self):
        return {"implementation": CUDA_GRAPH_VERSION, "scope": "code_predictor", "cache": "static",
                "cache_capacity": self.capacity, "captured": self.graph is not None,
                "capture_count": self.capture_count, "capture_seconds": self.capture_seconds,
                "replay_count": self.replay_count}
