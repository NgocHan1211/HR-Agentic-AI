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
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer
            except ImportError as exc: raise RuntimeError("install transformers, accelerate and torch") from exc
            use_cuda = torch.cuda.is_available()
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            if self._tokenizer.pad_token_id is None:
                # Qwen tokenizers often have no pad token; without one, generate() can
                # behave unpredictably (including emitting an immediate EOS -> empty output).
                self._tokenizer.pad_token = self._tokenizer.eos_token
            model_options: dict[str, Any] = {"torch_dtype": torch.float16 if use_cuda else torch.float32}
            if use_cuda:
                model_options["device_map"] = "auto"
            self._model = AutoModelForCausalLM.from_pretrained(self.model_id, **model_options)
            self._model.eval()
            if os.getenv("QWEN_DEBUG"):
                print(f"[QwenLocalCompletionClient] cuda_available={use_cuda}; device_map={'auto' if use_cuda else 'default CPU'}")
        inputs = self._tokenizer.apply_chat_template(self._messages(system, user), add_generation_prompt=True,
                                                      tokenize=True, return_dict=True, return_tensors="pt").to(self._model.device)
        outputs = self._model.generate(
            **inputs,
            max_new_tokens=self.max_new_tokens,
            min_new_tokens=8,  # forbid an immediate empty/EOS-only generation
            do_sample=False,
            pad_token_id=self._tokenizer.pad_token_id,
        )
        generated = outputs[0][inputs["input_ids"].shape[-1]:]
        text = self._tokenizer.decode(generated, skip_special_tokens=True).strip()
        if os.getenv("QWEN_DEBUG"):
            print(f"[QwenLocalCompletionClient] generated_tokens={generated.shape[-1]} raw={text[:500]!r}")
        if not text:
            raise RuntimeError(
                f"Qwen ({self.model_id}) generated an empty response "
                f"({generated.shape[-1]} tokens decoded to nothing). "
                "Try a larger/less-quantized model, increase QWEN_MAX_NEW_TOKENS, "
                "or set QWEN_DEBUG=1 to inspect raw generation."
            )
        return text

    def _vllm_complete(self, system: str, user: str) -> str:
        if self._model is None:
            try:
                import torch
                from vllm import LLM
            except ImportError as exc: raise RuntimeError("install vllm in a compatible CUDA environment") from exc
            if not torch.cuda.is_available():
                raise RuntimeError("vLLM requires an NVIDIA CUDA GPU, but torch.cuda.is_available() is False")
            self._model = LLM(model=self.model_id, dtype="auto", gpu_memory_utilization=0.80)
            try:
                from transformers import AutoTokenizer
            except ImportError as exc: raise RuntimeError("install transformers for Qwen chat template") from exc
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        from vllm import SamplingParams
        prompt = self._tokenizer.apply_chat_template(self._messages(system, user), tokenize=False, add_generation_prompt=True)
        result = self._model.generate(
            [prompt],
            SamplingParams(temperature=0, max_tokens=self.max_new_tokens, min_tokens=8),
            use_tqdm=False,
        )
        text = result[0].outputs[0].text.strip()
        if os.getenv("QWEN_DEBUG"):
            print(f"[QwenLocalCompletionClient] raw={text[:500]!r}")
        if not text:
            raise RuntimeError(
                f"Qwen ({self.model_id}) generated an empty response via vLLM. "
                "Try increasing QWEN_MAX_NEW_TOKENS or set QWEN_DEBUG=1 to inspect raw generation."
            )
        return text
