from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_base_url: str = ""
    model_name: str = ""
    model_api_key: str = ""
    max_active_runs: int = 1
    model_context_tokens: int | None = None

    def __post_init__(self):
        if self.model_context_tokens is not None and (
            not isinstance(self.model_context_tokens, int) or isinstance(self.model_context_tokens, bool)
            or self.model_context_tokens <= 0
        ):
            raise ValueError("Configured model context tokens must be a positive integer")

    @classmethod
    def from_env(cls) -> Settings:
        context = os.getenv("SC2H_MODEL_CONTEXT_TOKENS", "").strip()
        return cls(
            data_dir=Path(os.getenv("SC2H_DATA_DIR", "runtime")).resolve(),
            model_base_url=os.getenv("SC2H_MODEL_BASE_URL", "").strip(),
            model_name=os.getenv("SC2H_MODEL_NAME", "").strip(),
            model_api_key=os.getenv("SC2H_MODEL_API_KEY", "").strip(),
            model_context_tokens=int(context) if context else None,
        )

    def public_model(self) -> dict:
        # No endpoint query strings, credentials, authorization headers or keys are public.
        return {"configured": bool(self.model_base_url and self.model_name), "name": self.model_name}

    def redact(self, message: object) -> str:
        value = str(message)
        if self.model_api_key:
            value = value.replace(self.model_api_key, "[REDACTED]")
        return value[:2000]

    def clean(self, value):
        if isinstance(value, str):
            return value.replace(self.model_api_key, "[REDACTED]") if self.model_api_key else value
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, dict):
            return {key: ("[REDACTED]" if str(key).lower() in {"authorization", "api_key", "password"}
                          else self.clean(item)) for key, item in value.items()}
        return value
