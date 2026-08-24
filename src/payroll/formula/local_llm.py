"""Local Qwen inference adapters for offline formula extraction/review."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QwenLocalCompletionClient:
    """Lazy local client supporting `transformers` or vLLM offline inference."""
    backend: str = field(default_factory=lambda: os.getenv("FORMULA_LLM_BACKEND", "transformers"))
    model_id: str = field(default_factory=lambda: os.getenv("QWEN_MODEL_ID", "Qwen/Qwen2.5-3B-Instruct"))
    max_new_tokens: int = field(default_factory=lambda: int(os.getenv("QWEN_MAX_NEW_TOKENS", "1024")))
    _tokenizer: Any = field(init=False, default=None, repr=False)
    _model: Any = field(init=False, default=None, repr=False)

    def complete(self, *, system: str, user: str) -> str:
        if self.backend == "transformers": return self._transformers_complete(system, user)
        if self.backend == "vllm": return self._vllm_complete(system, user)
        raise ValueError("FORMULA_LLM_BACKEND must be transformers or vllm")

    def _messages(self, system: str, user: str) -> list[dict[str, str]]:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _transformers_complete(self, system: str, user: str) -> str:
        if self._model is None:
            try:
                from transformers import AutoModelForCausalLM, AutoTokenizer
            except ImportError as exc: raise RuntimeError("install transformers, accelerate and torch") from exc
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            self._model = AutoModelForCausalLM.from_pretrained(self.model_id, torch_dtype="auto", device_map="auto")
            self._model.eval()
        inputs = self._tokenizer.apply_chat_template(self._messages(system, user), add_generation_prompt=True,
                                                      tokenize=True, return_dict=True, return_tensors="pt").to(self._model.device)
        outputs = self._model.generate(**inputs, max_new_tokens=self.max_new_tokens, do_sample=False)
        generated = outputs[0][inputs["input_ids"].shape[-1]:]
        return self._tokenizer.decode(generated, skip_special_tokens=True).strip()

    def _vllm_complete(self, system: str, user: str) -> str:
        if self._model is None:
            try:
                from vllm import LLM
            except ImportError as exc: raise RuntimeError("install vllm in a compatible CUDA environment") from exc
            self._model = LLM(model=self.model_id, dtype="auto", gpu_memory_utilization=0.80)
            try:
                from transformers import AutoTokenizer
            except ImportError as exc: raise RuntimeError("install transformers for Qwen chat template") from exc
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        from vllm import SamplingParams
        prompt = self._tokenizer.apply_chat_template(self._messages(system, user), tokenize=False, add_generation_prompt=True)
        result = self._model.generate([prompt], SamplingParams(temperature=0, max_tokens=self.max_new_tokens), use_tqdm=False)
        return result[0].outputs[0].text.strip()
