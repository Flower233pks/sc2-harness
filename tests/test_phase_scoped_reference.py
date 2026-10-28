"""Reference-phase advice preserves independent model choices and default prompts."""

import asyncio
import hashlib
import json
from copy import deepcopy

import httpx
import pytest

from sc2_harness import policies
from sc2_harness.policies import LLMPolicy


@pytest.mark.parametrize("phase,advance", [
    ("reference_opening", False),
    ("form_reference_force", False),
    ("recommended_assault_phase", True),
])
def test_reference_main_army_search_advice_is_scoped_to_assault(phase, advance):
    policy = object.__new__(LLMPolicy)
    context = policies.action_context("tactics", {})
    reference = {"profile": "marine_reference_v1", "advisory": True, "phase": phase,
                 "focus": "Use the current phase and public evidence."}
    prompt = policy._reference_system_prompt("tactics", context, reference)
    assert ("first advance/search it with the army" in prompt) is advance
    assert ("continue MAIN-ARMY search/advance" in prompt) is advance
    assert ("There is no mandatory forward attack" in prompt) is not advance
    if not advance:
        assert "target options, not orders to send an isolated unit" in prompt
    assert "Only observed native Victory wins" in prompt


@pytest.mark.parametrize("phase,army,chosen", [
    ("form_reference_force", 1, [{"tool": "attack", "args": {"x": 31, "y": 43}}]),
    ("recommended_assault_phase", 12, []),
])
def test_phase_advice_does_not_mask_early_attack_or_force_assault(phase, army, chosen):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "goal": "Choose from current evidence", "reason": "Model-selected legal action",
                    "actions": chosen, "messages": []})}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8}})

        observation = {
            "engine": "sc2", "army_count": army,
            "map": {"width": 100, "height": 100, "enemy_start": [31, 43]},
            "task": {"scenario": "full_match", "reference_plan": {
                "profile": "marine_reference_v1", "advisory": True,
                "phase": phase, "focus": "Coordinate this phase using current evidence."}},
            "action_state": {"attack": {"available": True, "unit_tags": [7]}}}
        before = deepcopy(observation)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await LLMPolicy("http://localhost/v1", "test", client=client).decide(
                "tactics", observation, [])
        schema = requests[0]["response_format"]["json_schema"]["schema"]
        choices = schema["properties"]["actions"]["items"]["oneOf"]
        assert [entry["properties"]["tool"]["const"] for entry in choices] == ["attack"]
        assert result["actions"] == chosen
        assert observation == before
        assert len(requests) == 1 and result["request_count"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("role,digest", [
    ("economy", "c3c60463b22a98a637abe00fa02f2d68bffeb63a5e0232ae666916fd6d468ea9"),
    ("production", "828b5165b3577a58f92530d7427ea0ccb17f658059b6beb8754f43fb38a1099b"),
    ("tactics", "9fdb23973076360b5c6b311bf911947486fdcc7e628e697929d3f55e5435bdde"),
])
def test_phase_scope_candidate_leaves_none_prompts_byte_identical(role, digest):
    policy = object.__new__(LLMPolicy)
    task = {"scenario": "full_match", "guidance_profile": "none", "continuous_role_duty": True}
    prompt = policy._system_prompt(role, policies.action_context(role, {}), task=task)
    assert hashlib.sha256(prompt.encode()).hexdigest() == digest
