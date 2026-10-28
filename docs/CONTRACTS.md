# Current implementation contracts

Scope: one native Terran participant, three functional agents (`economy`, `production`, `tactics`),
versus a built-in computer. Native allied-player cooperation is not implemented. Stages and role goals
come from the experiment definition, not autonomous model task decomposition. Label scripted and sandbox modes.
Current runs record `evaluation_version="staged-v4-phase-handoff"`; individual mission definitions have their own version.

## Browser API (same origin)

- `GET /api/health`: `{status, version, capabilities, model}`. No secrets or inference.
- `GET /api/capabilities`: `{backends, roles, tools, player_mode, scenarios}`. Each scenario is a mission definition.
- `GET /api/runs`: `{runs: [RunSummary, ...]}`.
- `POST /api/runs`: RunConfig below; returns the created RunDetail. A conflicting active run returns 409.
- `GET /api/runs/{id}`: RunDetail.
- `POST /api/runs/{id}/control`: `{action: "pause"|"resume"|"stop"}`.
- `GET /api/runs/{id}/events?after=0&limit=200`: `{events: [...], cursor}`.
- `GET /api/runs/{id}/snapshots?after=0&limit=200`: `{snapshots: [...], cursor}`.
- `GET /api/runs/{id}/frame`: current native JPEG/PNG, 404 if unavailable; never a synthetic replacement.
- `GET /api/runs/{id}/artifacts`: `{artifacts: [{name, size, url}, ...]}`.
- `GET /api/runs/{id}/artifacts/{name}`: allowlisted exports or native `.SC2Replay` only.
- `WS /api/runs/{id}/stream`: `{type:"state",data:RunDetail}` or `{type:"event",data:Event}`.

RunConfig defaults:

```json
{"name":"","backend":"sc2","policy":"scripted","scenario":"build_and_scout",
 "map_name":"Simple64","seed":7,"max_rounds":60,"max_game_seconds":300,
 "advance_loops":112,"decision_timeout":30,"communication":true,
 "execution_guidance":true,"guidance_profile":"none","replay":true}
```

Backend is `sc2|sandbox`; policy is `scripted|llm`. Scenarios are `build_and_scout`, `full_match`,
`expansion_logistics`, `army_readiness`, `expansion_and_victory`. Bounds: rounds 1–500,
game seconds 10–3600, advance loops 8–448, decision timeout 1–120 seconds. Unknown fields are rejected.
API defaults do not automatically apply mission recommendations. The UI defaults to native SC2 and LLM;
explicit scenario selection can apply `suggested_budget` and `suggested_map`, preserving a custom map.

RunSummary: `{id,name,status,created_at,updated_at,round,backend,policy,scenario,map_name,result,error}`.
RunDetail adds `{config,observation,agents,metrics,milestones,evaluation,evaluation_version,source_fingerprint,
model_name,termination_reason,...}`. Historical `model_name` is separate from the current model service.
An explicitly selected reference profile also persists `reference_plan`; it is not a model-generated plan.
Statuses: `starting,running,deciding,paused,stopping,completed,stopped,failed,interrupted`.
Run result: `success,failure,incomplete,stopped,error`; `completed` status alone does not mean success.
Agent: `{id,name,status,goal,last_reason,last_actions,messages,stats}`; stats include actual request `calls`,
failed role decisions `errors`, and nullable `input_tokens/output_tokens`.
Event: `{seq,run_id,time,game_loop,round,type,actor_id,message,data}`.
Snapshot: `{seq,round,time,observation}`.
The UI supports `?run=<id>&panel=timeline|results|replay&view=rgb|tactical`; links never start or control a run.

## Observation evidence

Base fields: `{engine,engine_identity,game_loop,game_time,player_id,minerals,vespene,supply_used,supply_cap,
workers,army_count,units,structures,unit_counts,map,enemy_visible,scouted,result,score,rendering,warnings}`.
Units expose `{tag,type,x,y,health,health_max,shield,owner,is_structure,is_idle,is_ready,...}`.
`structures` counts completed buildings; legacy `unit_counts` can include unfinished entities.
Prefer native `ready_counts` for completed entities. `pending_counts` counts unfinished entities;
`queued_counts` counts actual training orders, never completed units. `production` contains actual producer orders.

Native additions:

- `worker_state`: observed idle/harvesting/constructing/moving tags, eligible builders, resource assignments,
  and gas `assigned_harvesters/ideal_harvesters/remaining_capacity`; unavailable optional counts remain null.
- `upgrades`: `{completed:[UpgradeIdName],in_progress:[{upgrade,unit_tag,ability_id,progress}]}`.
  Completed upgrades come from the native player upgrade list; orders are not completion evidence.
- `action_state`: `{tool:{available,unit_tags,reason,source,game_loop,...}}`, from current native abilities,
  ownership, readiness, resources and prerequisites. `gather` can add `resources`; `scout` can add
  `default_unit_tag/default_available`. Availability is not a promise that every argument or placement will work.
- `map`: `{width,height,start,enemy_start,expansions,resource_diagnostics}`. Expansion positions are derived
  from native neutral mineral snapshots. A known position does not prove an agent visited it; unreported
  snapshot contents are not assumed zero. Detailed resource diagnostics are omitted from model prompts.
- `score`: native cumulative `collected_minerals/collected_vespene/spent_minerals/spent_vespene` and other
  reported score fields. `killed_minerals/killed_vespene/lost_minerals/lost_vespene` retain official
  `{none,army,economy,technology,upgrade}` categories. Missing values remain null; do not infer casualty counts.

Native `result` is null or `Victory|Defeat|Tie`. Rendering is `rgb|unavailable` for SC2, `tactical` for sandbox.
Model observations contain only visible enemies; spectator enemy information must not leak.

## Decision, lifecycle and commands

Execution guidance v4 records `coordination.phase_handoff` when the observed evaluation stage changes.
It archives prior-stage prompt history and team messages from future inputs, without deleting durable evidence.
`task.phase_handoff` contains prior/current phase, observed game time, completed stage IDs and mission completion.
`task.continuous_role_duty` is true for an unfulfilled native Victory criterion: entering a stage is not success.
Each guided message records `phase`, `round`, and `game_time`. Decisions retain `context_reset` metadata
(`reset`, `from`, `to`, `discarded_messages`). Same-stage bounded history remains available;
guidance-disabled runs keep the legacy history behavior. No gameplay action is chosen by this handoff.
Structured provider context-overflow errors may trim old history and retry within the original
deadline and attempt limit. Other client errors do not trigger this recovery. `context_recovery`
records `overflow_errors,recovery_attempts,trimmed_history_pairs,proactive_trimmed_pairs` on decisions
and errors. Character trimming is a preventive heuristic, not a token-fit guarantee. Every HTTP
attempt remains counted; unreported usage, including failed attempts, stays unknown.

Model output: `{goal:string,reason:string,actions:[{tool,args}],messages:[{to:"all"|role,text}]}`.
Reason is a short public explanation, not private chain of thought. At most two actions and two messages.
Empty actions are valid. Each active role uses its own bounded context; messages are delivered on later rounds.

The policy receives observed state, `last_receipts` and `task:{scenario,objective,role,role_objective,milestones}`.
`RunConfig.reasoning_budget_tokens` defaults to 0 (the existing fast profile); positive budgets up to 1024
enable the explicit finite-reasoning profile, with 1536 total output tokens and one history turn. The provider
must support the requested reasoning parameters. Successful decisions record safe `inference_settings` and
`provider_diagnostics` (presence of reasoning content and allowlisted finish reason), never private reasoning
text. Provider completion-token usage includes generated reasoning; there is no invented per-reasoning split.
`SC2H_MODEL_CONTEXT_TOKENS` optionally declares the provider's context capacity. Public inference settings
record `context_window_tokens` and `context_window_source=configured|unknown`; this is not automatic
provider discovery. Every actual attempt records only safe scalar diagnostics: HTTP status, finish reason,
reported prompt/completion tokens and reasoning-content presence. An output truncated at the configured
capacity is distinct from an ordinary output cap. Recovery may remove old history within the original
attempt/deadline limits; it never repairs missing headroom by appending extra correction text.
With `execution_guidance=true`, task adds `current_stage,role_phase,role_done,role_milestones,goal_gaps,focus`.
Role milestones retain `observed,target,known,done` plus `types,upgrade,requires_upgrade,min_distance,maintain`
when declared. Unknown gaps remain null. `action_context` projects allowed tools, blocked reasons, eligible
unit tags, actual work in progress and recent rejection feedback; it does not recommend a command.
Adapters missing native availability are explicitly marked unchecked, not presented as validated availability.
The prompt retains the full team's tool knowledge, owners and prerequisites even when a tool is blocked.
`action_context.team_blockers` projects only actually reported unavailable native entries; knowledge does not
grant permission. `combat_status` reports current force counts, observed damage, cumulative army score,
current primary orders and Euclidean separation when positions are complete. It does not infer an assembled
force, enemy weakness, recent score deltas or a required attack threshold. Missing evidence stays null.

### Candidate05: current facts and bounded input

`combat_status.visible_enemy_targets` contains bounded `structures` and `units` lists with observed
`tag,type,x,y,health,health_max,shield`, at most eight per category, plus `counts`, `omitted` and visibility
coverage metadata. The scope is currently visible enemies in the supplied observation, not the whole map.
Native public observations are visible-only; other adapters require explicit visibility flags. Unknown
visibility is reported rather than treated as an observed target. `map.enemy_start` remains a prior location,
not evidence of a currently visible building or its health.

`combat_status.army_health` aggregates ready owned nonworker, nonstructure units in the supplied units:
`ready_units,known_units,unknown_units,full_health_units,damaged_units,health_sum,health_max_sum,coverage,
coverage_complete,scope`. Health sums cover known HP only, excluding shields. A short damaged-unit sample
does not describe the entire army; missing health or incomplete coverage does not become a fabricated zero.
These facts do not select an assault target, force size, regroup point or tactical action.

The prompt distinguishes the price of a NEW request from already existing pending construction, queued
production and research in progress. The original price is not an unpaid remainder; research at an
Engineering Bay does not itself occupy a separate Barracks. Pending work is still not a ready prerequisite,
and builder/order evidence matters for stalled construction. This is tool knowledge, not an automatic build order.

Guided current-user JSON uses a fixed 24,000-character bound. Only when initially over that bound, a finite
projection sequence removes identical duplicate counts and whole-army argument tags, omits worker/producer
detail from tactics input, and bounds receipt detail samples. Economy/production then omit known own
nonworker/nonstructure combat-unit samples if needed; if still oversized, parameter-free tools' eligible tag
arrays become counts of those supplied arrays, explicitly not native candidate totals. Unknown unit/tool
classifications and scout identity remain. Tactics instead bounds own combat samples to 12 and 8 if needed.
The E/P steps are `omit_off_role_combat_samples` and `count_parameter_free_tool_candidates`.
It preserves the task, current visible target facts, aggregate army evidence, native permissions,
ready/pending/queued distinctions and scout identity. `input_projection.budget` records `limit_chars`,
`initial_chars` and applied `steps`; sample omissions remain explicit. Original snapshots and Trace are unchanged.
The projection never truncates a JSON string or edits model actions. If still too large, `input_too_large`
remains a zero-HTTP error. This current-user character bound is not a tokenizer or a guarantee that the total
system/history/current prompt plus output fits a configured context window. A model's nearby target coordinates
remain its proposal; the framework does not snap them to an observed target or attack on the model's behalf.

### Optional provided-strategy reference profile

`RunConfig.guidance_profile` is `none` by default or `marine_reference_v1`. The reference option requires
`scenario=full_match`, `policy=llm` and `execution_guidance=true`; sandbox may exercise the contract in tests
but cannot provide native-game evidence. `none` preserves the existing role negotiation mode. The reference
option is an explicitly provided Marine strategy for an LLM collaborative-execution baseline, not a claim
that the model devised the strategy. It adds no planner LLM, scripted action fallback or extra native player.
All three active roles still make actual model requests and choose their own permitted actions.

`run.reference_plan` and snapshots persist `{profile,version,revision,label,advisory,phase,...}` with the full
team's soft targets and observed timing. The profile name and `version=1` are unchanged; the current
`revision=3` uses `reference_opening`, `form_reference_force` and `recommended_assault_phase`. Opening
references are 14 workers, one ready Supply Depot and one ready Barracks. Observing all three moves to
formation, which references about 20 workers, two Barracks, 12 ready Marines and four free supply.
Formation remains latched if setup later suffers losses. Observing 12 ready Marines, even before all setup
references, latches the assault advice. Neither transition proves assembly, an issued attack or native Victory.
`coordination.reference_phase_changed` records `revision`, `from_revision`, prior/current phase and the
observed team targets; it is explicitly not an agent action. Legacy missing revisions remain unknown.

`task.reference_plan` is the current role's projection: it retains plan identity/timing and adds `role`,
`role_targets`, `role_progress`, `continuous_duty` and `focus`, omitting the duplicated full-team `soft_targets`
and `continuous_duties` maps. Targets
retain `tool,target,observed,known,met,gap,progress,in_progress` (actual pending/queued counts). The global
plan and raw snapshots remain unchanged. Only role-local numeric setup goals are presented as assignments;
opening tactics has no numeric setup target or mandatory scout. A role may choose empty actions while
teammates' funded work progresses. Existing work is already paid, not another charge against current minerals.
Revision 3 distinguishes setup counts from continuous responsibilities: 12 Marines is a launch reference,
never a production quota/cap. The full plan stores `continuous_duties` per role; the role projection retains
only its own `continuous_duty:{owner,activity,continues_after_setup,until,progress}`. Economy progress reports
workers, free population supply and observed Marine supply blockers. Production reports ready/queued
Marines, idle ready Barracks and native training availability. Tactics reports advisory initial-hint progress.
Missing availability/coverage remains unknown. These are supplied strategy responsibilities, not autonomous
model task decomposition. This advice does not mask otherwise legal tools or automatically reserve shared resources.

`initial_hint_public_progress` is derived from public ready owned nonworker/nonstructure positions and
`map.enemy_start`, not hidden enemies or `scouted`. It reports hint position, radius 15, ready-army total,
known-position count, nearby count, coverage, complete-position flag, strict-majority flag and observed timing.
`initial_hint_reached` latches only in the recommended assault phase when all expected army positions are
known and at least two units plus a strict majority are within that radius. Before the launch-reference
phase, a pair of scouts cannot latch it. A prior latch is reused only under the same profile/version/revision
and hint, with a previous assault phase; a changed revision is not backfilled. This says the public main army
was observed near a search hint, not that it cleared the location, found enemies, issued an attack or won.
Only tactics receives this detailed progress in its role projection. The current policy separates tactical
advice by reference phase: opening/formation coordinates observation, assembly and defense, with no mandatory
forward attack or initial-hint visit. A visible enemy building alone is not an instruction to send an isolated
unit. Justified early scouting, defense or combat remains legal; this is not a tool mask or a hard 12-unit gate.
In the recommended assault phase, the existing guidance prioritizes current living visible targets, then
initial main-army search if unvisited, then continued main-army search elsewhere without Victory. That assault
wording and the economy/production prompts are unchanged by the phase-only fix. The model still selects the
destination and legal actions. One scout's movement
does not count as main-army progress. Neither the hint latch nor continuous duties alter formal mission evaluation.

The framework does not automatically train units, issue attack orders or inject a replacement model decision.
Native Victory remains the only `full_match` success criterion, including a Victory before the suggested force size. Defeat, budget exhaustion
and diagnostic stop retain their existing outcomes.

Offline demo cohorts include `config.guidance_profile` separately from model `inference_settings`. Historical
missing values remain null/unknown rather than being backfilled as `none`. Unknown, explicit `none` and
`marine_reference_v1` cannot combine maps to satisfy the same three-map result; each known profile may qualify
independently under the original native Victory, wall-time and actual-LLM evidence requirements. Source
fingerprints also separate revisions; the profile string alone cannot identify a historical plan revision.
Historical raw records and earlier results are not rewritten.

v3 lifecycle: a guided role with `role_done=true` waits without invoking its policy. The coordinator records
`coordination.role_wait` with `request_count=0,actions=[]`; it is not an `agent.decision`. New stage targets
reactivate roles. Explicit `maintain=true` goals use current values for role scheduling even when the historical
milestone is latched. Shared victory goals keep all three roles active. Guidance also rotates shared-resource
arbitration priority. Disabling guidance preserves the legacy comparison path; communication is a separate switch.
Neither mode silently falls back to scripted actions or sandbox.

Tools:

- economy: `train_worker`, `gather`, `build_supply`, `build_refinery`, `expand`.
- production: `build_barracks`, `build_engineering_bay`, `train_marine`, `research_infantry`.
- tactics: `scout`, `attack`, `retreat`, `hold`.

Tools are Terran mechanical executors, not whole-game strategy scripts. Building operations accept optional
`{x,y}`; mobility accepts optional `{x,y}`, with optional `unit_tag` for scouting. Gathering accepts
`{resource:"minerals"|"gas",max_workers:1..24}`; gas assignment respects reported capacity and per-batch
reservations. Default scouting reuses the tracked eligible scout. No arbitrary code execution.
An explicit mineral-gather request selects idle SCVs first, then observed SCVs gathering at ready owned
refineries, bounded by max_workers and native ability. Construction, movement, tracked scouting, existing
mineral harvesters and same-batch reservations are protected; return-cargo source is not guessed. Command
details retain observed source-resource/target provenance. This does not automatically rebalance the economy.
Command: `{command_id,actor_id,tool,args}`.
Receipt: `{command_id,actor_id,tool,status:"accepted"|"rejected"|"completed",reason,details}`.
Engine acceptance is not confirmed arrival, collected resources, completed production, or mission success.
Interrupted in-flight commands receive `command.outcome_unknown`, not an invented result or automatic replay.
Native construction receipts may include `details.pathing` from ability/placement/path preflight. It is not a
construction-completion guarantee. Later observed action errors preserve the initial `status="accepted"`;
`details.execution_status` records `failed|partial|pending_observation` separately. Unique unit/ability matches
carry the command identity; ambiguous/unmatched errors remain in `observation.unmatched_action_errors`.
The runtime emits `command.execution_failed|command.execution_partial` and counts them independently in
`commands_execution_failed|commands_execution_partial`; these do not become initial rejections or completion.
Tactics commands affect the eligible army as a whole. Empty actions preserve existing orders. `hold` issues
HOLD POSITION and cancels travel/attack-move; it does not mean maintain the previous attack.

## Mission evaluation and metrics

Mission definition: `{id,name,version,objective,stages,suggested_budget,suggested_map}`. Stages contain
`{id,label,policy_goal,prerequisites,criteria}`; criteria declare their owner, metric and threshold.
Legacy missions are version 1; the three long missions are version 2. See [missions.py](../sc2_harness/missions.py)
and [evaluation.py](../sc2_harness/evaluation.py) for the authoritative definitions.

Evaluation: `{scenario,mission_version,milestones,stages,current_stage,stage_completion,progress,complete,
outcome,engine_result,censored,termination_reason,observed_at_game_time}`.
Milestones include `{id,label,role,metric,value,target,known,eligible,achieved,reported,
achieved_at_game_time,censored,...}`. Stages include `{id,label,status,complete,progress,
achieved_at_game_time,censored,prerequisites,criteria,...}`; status is `active|blocked|complete`.
`stage_completion={completed,total}`. Progress can be partial; it is not task success.

Prerequisites gate later stages. Counts must exist simultaneously in an observation, not accumulate across
dead units or command receipts. Thresholds latch with the first observed game time; unachieved times are null.
Expansion requires two ready owned bases at least 12 units apart, plus a known resource cluster strictly more
than 16 from the original `map.start`. Army readiness counts 12 Marines only after infantry weapons 1 is
observed complete; its supply-capacity goal also has realtime maintenance for role scheduling.
Native Defeat always overrides milestone completion as failure. Victory and specialized task success are
separate. Budget/stop/error censor unfinished goals; native defeat is a failure, not budget censoring.
Top-level `run.result` retains `stopped/error` even if observation evaluation remains `incomplete`.

Metrics include request/token totals, accepted/rejected/proposed commands and rejection reasons, native score,
milestone/stage counts and completion rates, and successful task completion game/wall seconds. Ratios with a
zero denominator are null. `latency_p50_ms/p95_ms` summarize valid role decisions, including bounded correction
attempts; `round_decision_latency_p50_ms/p95_ms` summarize whole decision-round waits separately.
`role_decisions_scheduled` and `coordinator_wait_role_rounds` separate scheduling from model requests.
Game time advances only by engine steps; enemies also pause during inference. These are not realtime-play metrics.

## Native backend, persistence and evidence

`NativeBackend`: `__init__()`; `async start(config,run_dir)`; `async step(commands,advance_loops)`;
`async close()`; static `probe()`. Start/step return `{observation,receipts,frame_path,terminal}`.
Close returns replay/status metadata. The adapter owns the subprocess and game lifecycle; game imports are lazy.
Optional `SC2H_GAME_PYTHON` selects the game worker interpreter. JSONL stdout is protocol-only; SDK logs go
to stderr/run records. Frames and replay remain inside the run directory. Sandbox implements the same interface
for deterministic API tests and cannot substitute for native game acceptance.

Persist run/config/metrics plus append-only events and snapshots under generated run IDs. Restart marks
in-flight runs interrupted; it does not resume a game. Exports use a consistent database read snapshot and
atomic per-file replacement, with no arbitrary-path downloads. Native replay and browser tactical reconstruction
are different artifacts. Historical results are not rewritten when code or evaluation versions change.

Record actual HTTP attempts, including failed/cancelled attempts. Zero-request roles have known zero token
usage; attempted requests with missing provider usage keep the corresponding total null. For older records,
the UI may derive a missing token side only if all role call counts are valid and sum to the recorded global
count, and every active role reports that side. Zero-call roles contribute zero. Label the ledger derivation;
never overwrite the original record or derive missing latency percentiles.
Capture public model output and redacted request context for audit; never persist private reasoning or secrets.
Credentials and authorization headers must not enter events, exports, prompts, or public config.
