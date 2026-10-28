"""Small public contracts; native SC2 and model dependencies are optional."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROLES = ("economy", "production", "tactics")
ROLE_NAMES = {"economy": "经济", "production": "建设与生产", "tactics": "侦察与作战"}
TERMINAL = frozenset({"completed", "stopped", "failed", "interrupted"})


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(default="", max_length=100)
    backend: Literal["sc2", "sandbox"] = "sc2"
    policy: Literal["scripted", "llm"] = "scripted"
    scenario: Literal["build_and_scout", "full_match", "expansion_logistics", "army_readiness",
                      "expansion_and_victory"] = "build_and_scout"
    map_name: str = Field(default="Simple64", min_length=1, max_length=80)
    seed: int = Field(default=7, ge=0, le=2**31 - 1)
    max_rounds: int = Field(default=60, ge=1, le=500)
    max_game_seconds: int = Field(default=300, ge=10, le=3600)
    advance_loops: int = Field(default=112, ge=8, le=448)
    decision_timeout: float = Field(default=30, ge=1, le=120)
    communication: bool = True
    execution_guidance: bool = True
    guidance_profile: Literal["none", "marine_reference_v1"] = "none"
    reasoning_budget_tokens: int = Field(default=0, ge=0, le=1024, strict=True)
    replay: bool = True

    @model_validator(mode="after")
    def guidance_matches_experiment(self):
        if self.guidance_profile != "none" and (
            self.policy != "llm" or self.scenario != "full_match" or not self.execution_guidance
        ):
            raise ValueError("A reference guidance profile requires full_match, llm and execution_guidance=true")
        return self

    @field_validator("map_name")
    @classmethod
    def map_is_a_name(cls, value: str) -> str:
        # Do not let browser input become an arbitrary local map/file path.
        if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-'" for c in value):
            raise ValueError("map_name must be a map name, not a file path")
        return value


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "resume", "stop"]


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)


def initial_agents() -> list[dict]:
    return [
        {"id": role, "name": ROLE_NAMES[role], "status": "idle", "goal": "等待任务",
         "last_reason": "", "last_actions": [], "messages": [],
         "stats": {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0}}
        for role in ROLES
    ]
