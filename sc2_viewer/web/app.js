"use strict";

const ROLE_LABELS = Object.freeze({economy: "经济", production: "生产", tactics: "战术"});
const TERMINAL_STATUSES = new Set(["completed", "stopped", "failed", "interrupted"]);
const RUN_ID = /^[a-f0-9]{32}$/;
const POLL_MS = 2000;

function publicText(value, fallback = "—", limit = 1600) {
  if (typeof value !== "string" || !value.trim()) return fallback;
  return value.slice(0, limit).replace(/(?:[A-Za-z]:[\\/]|\/(?:home|Users|usr|tmp|opt|var)\/)[^\s\n,;"'<>]*/g, "[内部路径已省略]");
}
function finiteNumber(value) { return typeof value === "number" && Number.isFinite(value) ? value : null; }
function numberText(value, digits = 0) {
  const number = finiteNumber(value);
  return number === null ? "—" : number.toLocaleString("zh-CN", {maximumFractionDigits: digits});
}
function clockText(value) {
  const number = finiteNumber(value);
  if (number === null || number < 0) return "—";
  const seconds = Math.floor(number);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function isTerminal(run) { return TERMINAL_STATUSES.has(run?.status); }
function nativeResult(run) {
  const result = run?.observation?.result ?? run?.metrics?.engine_result;
  return ["Victory", "Defeat", "Tie"].includes(result) ? result : null;
}
function statusText(run) {
  const native = nativeResult(run);
  if (native) return {Victory: "原生胜利", Defeat: "原生失败", Tie: "原生平局"}[native];
  return {starting: "启动中", running: "运行中", deciding: "模型决策中", paused: "对局已暂停", stopping: "正在结束", completed: "本局已结束", stopped: "本局已停止", failed: "实验异常结束", interrupted: "本局已中断"}[run?.status] || "状态未知";
}
function sourceMode(run) {
  const engine = run?.backend ?? run?.config?.backend;
  const policy = run?.policy ?? run?.config?.policy;
  const facts = [engine === "sc2" ? "原生 StarCraft II" : engine === "sandbox" ? "Sandbox 接口沙盒，非原生 SC2" : "运行环境未报告", policy === "llm" ? "真实模型策略" : policy === "scripted" ? "Scripted 规则基线，非模型决策" : "策略未报告"];
  facts.push(run?.config?.guidance_profile === "marine_reference_v1" ? "给定 Marine 参考策略下的协同执行" : "未记录给定 Marine 策略");
  return facts.join(" · ") + "。一个原生玩家内的三个功能 Agent；模型使用结构化状态和工具，不是视觉键鼠操作。";
}
function mergeBySeq(existing, incoming) {
  const merged = new Map(existing.map(row => [row.seq, row]));
  for (const row of incoming) if (row && Number.isSafeInteger(row.seq)) merged.set(row.seq, row);
  return [...merged.values()].sort((left, right) => left.seq - right.seq);
}
function visibleEnemy(unit) {
  if (unit?.owner !== "enemy" || unit.visible === false || unit.is_visible === false || [2, 3, 4].includes(unit.display_type)) return false;
  // The viewer backend supplies the same visible-only native observation contract.
  return true;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {publicText, finiteNumber, numberText, clockText, isTerminal, nativeResult, statusText, sourceMode, mergeBySeq, visibleEnemy};
}

if (typeof document !== "undefined") {
  const byId = id => document.getElementById(id);
  const state = {config: null, runId: null, run: null, runs: [], events: [], snapshots: [], eventCursor: 0, snapshotCursor: 0, generation: 0, busyFor: null, view: "rgb", history: false, historyIndex: 0, playing: null, mediaKey: null, mediaKind: null, mediaFailed: false, lastListAt: 0};
  const image = byId("native-image");
  const canvas = byId("tactical-canvas");
  function text(id, value) { byId(id).textContent = value; }
  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }
  function notice(message) { text("notice", message); byId("notice").classList.toggle("hidden", !message); }
  function connection(message, error = false) { text("connection", message); byId("connection").classList.toggle("error", error); }
  async function getJSON(path) {
    const response = await fetch(path, {method: "GET", cache: "no-store", credentials: "same-origin", signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`);
    return response.json();
  }
  async function pages(kind, cursor, generation) {
    const rows = [];
    const pageSize = kind === "snapshots" ? 100 : 500;
    for (let count = 0; count < 5; count++) {
      const page = await getJSON(`/api/runs/${state.runId}/${kind}?after=${cursor}&limit=${pageSize}`);
      if (generation !== state.generation) return null;
      if (!Array.isArray(page[kind])) throw new Error("观众记录格式异常");
      rows.push(...page[kind]);
      const next = Number.isSafeInteger(page.cursor) ? page.cursor : cursor;
      if (page[kind].length < pageSize || next <= cursor) { cursor = next; break; }
      cursor = next;
    }
    return {rows, cursor};
  }
  function displayedSnapshot() { return state.history ? state.snapshots[state.historyIndex] || null : null; }
  function displayedObservation() { return displayedSnapshot()?.observation || state.run?.observation || {}; }
  function displayedEvents() {
    const selected = displayedSnapshot();
    return selected ? state.events.filter(event => finiteNumber(event.round) !== null && event.round <= selected.round) : state.events;
  }
  function renderRuns() {
    const container = byId("run-list");
    container.replaceChildren();
    text("run-count", numberText(state.runs.length));
    if (!state.runs.length) { container.append(element("p", "empty", "暂无已公开的实验")); return; }
    for (const run of state.runs) {
      const button = element("button", `run-item${run.id === state.runId ? " active" : ""}`);
      button.type = "button";
      button.setAttribute("aria-pressed", String(run.id === state.runId));
      button.append(element("strong", "", publicText(run.map_name ?? run.config?.map_name, publicText(run.name, "已公开实验"), 120)));
      const result = nativeResult(run);
      button.append(element("span", result === "Victory" ? "result-win" : result === "Defeat" ? "result-loss" : "", statusText(run)));
      button.append(element("span", "small-mono", `${run.id.slice(0, 8)} · ${publicText(run.policy ?? run.config?.policy, "策略未知", 24)}`));
      button.addEventListener("click", () => selectRun(run.id));
      container.append(button);
    }
  }
  function renderHeading() {
    const run = state.run;
    if (!run) return;
    text("run-title", publicText(run.name, publicText(run.map_name ?? run.config?.map_name, "公开实验"), 140));
    const meta = byId("run-meta");
    meta.replaceChildren();
    const native = nativeResult(run);
    meta.append(element("span", `status-chip ${native === "Victory" ? "win" : native === "Defeat" ? "loss" : !isTerminal(run) ? "active" : ""}`, statusText(run)));
    meta.append(document.createTextNode(`${publicText(run.map_name ?? run.config?.map_name, "地图未知", 80)} · ${publicText(run.scenario ?? run.config?.scenario, "任务未知", 50)} · ROUND ${numberText(run.round)}`));
    text("mode-note", sourceMode(run));
    text("model-name", publicText(run.model_name, "本局模型未报告", 100));
    text("evidence-id", run.id.slice(0, 12));
    const observation = displayedObservation();
    text("minerals", numberText(observation.minerals)); text("vespene", numberText(observation.vespene));
    text("supply", `${numberText(observation.supply_used)} / ${numberText(observation.supply_cap)}`);
    text("units", `${numberText(observation.workers)} / ${numberText(observation.army_count)}`);
    text("game-time", clockText(observation.game_time)); text("wall-time", clockText(run.metrics?.wall_seconds));
    text("game-clock-label", state.history ? "所选历史游戏时间" : "游戏时间");
    text("round-label", `ROUND ${numberText(displayedSnapshot()?.round ?? run.round)}${state.history ? " / HISTORY" : ""}`);
    text("history-current", isTerminal(run) ? "返回本局末状态" : "返回最新状态");
    text("agent-context", state.history ? "所选状态之前已记录的公开决策；缺失记录保留未知。" : "显示公开目标、理由与动作，不展示模型私有推理。");
  }
  function renderAgents() {
    const container = byId("agent-list"); container.replaceChildren();
    const events = displayedEvents();
    for (const [role, label] of Object.entries(ROLE_LABELS)) {
      const agent = (state.run?.agents || []).find(item => item.id === role) || {};
      const latest = [...events].reverse().find(event => event.type === "agent.decision" && event.actor_id === role);
      const data = state.history ? latest?.data || {} : {goal: agent.last_goal ?? agent.goal ?? latest?.data?.goal, reason: agent.last_reason ?? latest?.data?.reason, actions: agent.last_actions ?? latest?.data?.actions};
      const card = element("section", `agent-card ${role}`);
      const heading = element("div", "agent-card-header");
      heading.append(element("i", "agent-bullet"), element("strong", "", label), element("span", "", state.history ? latest ? `R${latest.round}` : "记录未知" : publicText(agent.status, "未知", 30)));
      card.append(heading, element("div", "agent-goal", publicText(data.goal, "尚无已记录目标", 240)), element("p", "agent-reason", publicText(data.reason, "尚无公开决策理由", 800)));
      const actions = Array.isArray(data.actions) ? data.actions : null;
      card.append(element("pre", "agent-actions", actions === null ? "动作未知" : actions.length ? actions.slice(0, 2).map(action => `${publicText(action.tool, "未知工具", 60)} ${publicText(JSON.stringify(action.args || {}), "{}", 300)}`).join("\n") : "actions=[] · 本轮未请求新动作"));
      const stats = agent.stats || {};
      card.append(element("div", "agent-stats", `本局累计请求 ${numberText(stats.calls)} · 决策错误 ${numberText(stats.errors)}\n输入 / 输出 token ${numberText(stats.input_tokens)} / ${numberText(stats.output_tokens)}`));
      container.append(card);
    }
  }
  function renderCommunications() {
    const messages = displayedEvents().filter(event => event.type === "agent.message").slice(-35).reverse();
    const container = byId("message-list"); container.replaceChildren(); text("message-count", String(messages.length));
    if (!messages.length) { container.append(element("p", "empty", "此处尚无已记录通信")); return; }
    for (const event of messages) {
      const data = event.data || {};
      const message = element("article", "message-card");
      const from = ROLE_LABELS[data.from ?? event.actor_id] || publicText(data.from ?? event.actor_id, "未知", 40);
      const to = ROLE_LABELS[data.to] || (data.to === "all" ? "全队" : "未知");
      message.append(element("small", "", `${from} → ${to} · R${numberText(event.round)}`), element("p", "", publicText(data.text, publicText(event.message, "消息内容未报告", 500), 500)));
      container.append(message);
    }
  }
  function eventDescription(event) {
    const data = event.data || {};
    if (event.type === "agent.decision") return ["公开决策", publicText(data.goal, publicText(data.reason, "已记录决策", 350), 350), ""];
    if (event.type === "agent.message") return ["协作消息", publicText(data.text, "消息内容未报告", 350), ""];
    if (event.type === "command.accepted") return ["指令 ACK · 非完成", `${publicText(data.tool, "工具未知", 60)} · ${publicText(data.reason, "引擎已接受", 300)}`, ""];
    if (event.type === "command.rejected") return ["指令初始拒绝", `${publicText(data.tool, "工具未知", 60)} · ${publicText(data.reason, "原因未报告", 300)}`, "rejected"];
    if (["command.execution_failed", "command.execution_partial"].includes(event.type)) return ["ACK 后执行异常", `${publicText(data.tool, "工具未知", 60)} · ${event.type === "command.execution_failed" ? "失败" : "部分失败"}`, "failure"];
    if (event.type === "agent.error") return ["模型决策错误", publicText(data.code, "错误类型未报告", 100), "failure"];
    if (event.type === "coordination.reference_phase_changed") return ["给定参考阶段变化", "观测投影更新；不代表已下达攻击或任务完成。", ""];
    if (event.type === "run.finished") return ["本局结束", "终态指标见结果面板。", ""];
    return null;
  }
  function renderEvents() {
    const container = byId("event-list"); container.replaceChildren();
    const rows = displayedEvents().map(event => ({event, copy: eventDescription(event)})).filter(row => row.copy).slice(-80).reverse();
    if (!rows.length) { container.append(element("p", "empty", "尚无已记录公开事件")); return; }
    for (const {event, copy} of rows) {
      const row = element("article", "event-row");
      const time = typeof event.time === "string" && !Number.isNaN(Date.parse(event.time)) ? new Date(event.time).toLocaleTimeString("zh-CN", {hour12: false}) : "时间未知";
      row.append(element("div", "event-time", `R${numberText(event.round)}\n${time}`), element("div", "event-actor", ROLE_LABELS[event.actor_id] || "系统"));
      const content = element("div", "event-copy"); content.append(element("strong", copy[2], copy[0]), document.createTextNode(copy[1])); row.append(content); container.append(row);
    }
  }
  function metric(label, value, note = "") {
    const node = element("div", "metric"); node.append(element("span", "", label), element("strong", "", value));
    if (note) node.append(element("small", "", note)); return node;
  }
  function renderResults() {
    const run = state.run; if (!run) return;
    const m = run.metrics || {}; const container = byId("results-grid"); container.replaceChildren();
    const values = [
      ["原生胜负", nativeResult(run) || "尚未报告", isTerminal(run) ? "本局已结束，按实际原生结果显示" : "本局尚未终态"],
      ["模型 HTTP 请求", numberText(m.model_calls), `决策错误 ${numberText(m.model_errors)}`],
      ["指令 ACK / 初始拒绝", `${numberText(m.commands_accepted)} / ${numberText(m.commands_rejected)}`, "ACK 不代表动作已完成"],
      ["后续失败 / 部分失败", `${numberText(m.commands_execution_failed)} / ${numberText(m.commands_execution_partial)}`, "与初始拒绝分开统计"],
      ["角色延迟 p50 / p95", `${numberText(finiteNumber(m.latency_p50_ms) === null ? null : m.latency_p50_ms / 1000, 2)} / ${numberText(finiteNumber(m.latency_p95_ms) === null ? null : m.latency_p95_ms / 1000, 2)} s`, "本局单角色决策分布"],
      ["整轮延迟 p50 / p95", `${numberText(finiteNumber(m.round_decision_latency_p50_ms) === null ? null : m.round_decision_latency_p50_ms / 1000, 2)} / ${numberText(finiteNumber(m.round_decision_latency_p95_ms) === null ? null : m.round_decision_latency_p95_ms / 1000, 2)} s`, "并行角色的整轮等待，未报告时保留未知"],
      ["输入 / 输出 token", `${numberText(m.input_tokens)} / ${numberText(m.output_tokens)}`, "输出可能包含服务端思考 token，未报告项不填零"],
    ];
    for (const valuesForMetric of values) container.append(metric(...valuesForMetric));
    const evaluationOutcome = publicText(run.evaluation?.outcome, m.task_success === true ? "success" : m.task_success === false ? "未记录成功" : "未知", 50);
    const lines = [`实验记录：${publicText(run.result, "结果未报告", 60)}；任务评估：${evaluationOutcome}`];
    const config = run.config || {};
    lines.push(`地图 ${publicText(config.map_name ?? run.map_name, "未知", 80)} · seed ${numberText(config.seed)} · 每轮推进 ${numberText(config.advance_loops)} loops`);
    lines.push(`本局作战指导：${config.guidance_profile === "marine_reference_v1" ? "明确给定 Marine 参考策略；不等于模型自主制定战略" : publicText(config.guidance_profile, "未报告", 80)}`);
    if (finiteNumber(config.reasoning_budget_tokens) !== null) lines.push(`请求的思考预算：${numberText(config.reasoning_budget_tokens)} token（配置值，不代替实际调用证据）`);
    const plan = run.reference_plan;
    if (plan) lines.push(`观测参考阶段：${publicText(plan.phase, "未知", 80)}；阶段不是攻击命令或成功门槛。`);
    const milestones = Array.isArray(run.milestones) ? run.milestones : Array.isArray(run.evaluation?.milestones) ? run.evaluation.milestones : [];
    for (const item of milestones.slice(0, 12)) lines.push(`${item.achieved === true ? "✓" : "○"} ${publicText(item.label ?? item.id, "里程碑", 150)}${finiteNumber(item.value) !== null && finiteNumber(item.target) !== null ? ` ${numberText(item.value)} / ${numberText(item.target)}` : ""}`);
    const score = m.native_score || run.observation?.score;
    if (score?.available === true) lines.push(`原生累计军队 / 经济矿物损失：${numberText(score.lost_minerals?.army)} / ${numberText(score.lost_minerals?.economy)}（资源价值，不是伤亡人数）`);
    text("evaluation-detail", lines.join("\n"));
  }
  function renderHistory() {
    const count = state.snapshots.length; const slider = byId("history-slider");
    slider.disabled = !count; slider.max = String(Math.max(0, count - 1)); slider.value = String(state.history ? state.historyIndex : Math.max(0, count - 1));
    byId("history-play").disabled = count < 2; byId("history-current").disabled = !state.history;
    text("history-position", `${count ? Number(slider.value) + 1 : 0} / ${count}`);
    text("history-count", `${numberText(count)} 个已加载状态`);
    text("history-note", state.history ? "正在浏览历史状态；角色和通信也按所选轮次展示。播放仅移动本地滑块，不控制游戏。" : "滑块与播放只浏览已记录状态，不控制游戏。原生录像和状态记录使用独立时间轴。");
  }
  function emptyStage(title, description) { text("empty-title", title); text("empty-text", description); byId("stage-empty").classList.remove("hidden"); }
  function mediaLoaded(key, kind) {
    if (state.mediaKey !== key || state.view !== "rgb" || state.history) return;
    image.classList.remove("hidden"); byId("stage-empty").classList.add("hidden"); state.mediaFailed = false;
    text("view-label", kind === "recording" ? "已结束对局 · 已录制画面（非直播）" : isTerminal(state.run) ? "已结束对局 · 最后保存帧（非直播）" : "当前原生 RGB · 非连续视频流");
    text("media-status", kind === "recording" ? "RECORDED / NOT LIVE" : isTerminal(state.run) ? "LAST FRAME / NOT LIVE" : state.run?.status === "paused" ? "PAUSED / LAST FRAME" : "FRAME REFRESH / 2s");
  }
  function setMedia(kind, force = false) {
    if (!state.runId) return;
    const terminal = isTerminal(state.run);
    const key = `${state.runId}:${kind}:${terminal ? "terminal" : state.run?.status || "unknown"}`;
    if (!force && state.mediaKey === key) return;
    state.mediaKey = key; state.mediaKind = kind; state.mediaFailed = false;
    image.onload = () => mediaLoaded(key, kind);
    image.onerror = () => {
      if (state.mediaKey !== key) return;
      image.classList.add("hidden");
      if (kind === "recording") {
        text("media-description", "本局未提供可用录像，回退至最后保存帧；可切换战术态势浏览历史状态。最后帧不是直播。");
        setMedia("frame");
      } else {
        state.mediaFailed = true;
        emptyStage("真实画面暂不可用", terminal ? "本局没有可读取的录像或最后帧。可切换战术态势查看已保存状态。" : "原生画面尚未生成或暂时读取失败。页面不会用战术图冒充真实画面。");
      }
    };
    image.src = kind === "recording" ? `/api/runs/${state.runId}/recording.webp` : `/api/runs/${state.runId}/frame${terminal ? "" : `?v=${Date.now()}`}`;
  }
  function refreshMedia() {
    if (!state.run) return;
    const tactical = state.view === "tactical" || state.history;
    byId("view-rgb").classList.toggle("active", !tactical); byId("view-rgb").setAttribute("aria-selected", String(!tactical));
    byId("view-tactical").classList.toggle("active", tactical); byId("view-tactical").setAttribute("aria-selected", String(tactical));
    canvas.classList.toggle("hidden", !tactical); image.classList.toggle("hidden", tactical || state.mediaFailed || !image.complete || !image.naturalWidth);
    byId("tactical-legend").classList.toggle("hidden", !tactical);
    if (tactical) {
      text("view-label", state.history ? "历史公开状态 · 战术示意（非原生画面）" : "公开状态 · 战术示意（非原生画面）");
      text("media-status", state.history ? "HISTORICAL SNAPSHOT" : isTerminal(state.run) ? "FINAL STATE / NOT LIVE" : "OBSERVED STATE");
      text("media-description", "仅按已公开的位置与可见敌方绘图；不是完整地图视野，也不代表模型视觉输入。地图原点和坐标来自游戏状态。");
      drawTactical();
    } else {
      const recordingAvailable = state.run.recording_available !== false;
      text("media-status", isTerminal(state.run) ? state.mediaKind === "frame" || !recordingAvailable ? "LAST FRAME / NOT LIVE" : "RECORDED / NOT LIVE" : state.run.status === "paused" ? "PAUSED / LAST FRAME" : "FRAME REFRESH / 2s");
      text("media-description", isTerminal(state.run) ? state.mediaKind === "frame" || !recordingAvailable ? "本局没有可用录像，显示最后保存帧（非直播）；可切换战术态势浏览历史状态。" : "已结束对局：播放真实墙钟抽样录制画面（非直播、非固定游戏帧率）；状态回放使用独立时间轴。" : state.run.status === "paused" ? "本局已暂停；显示最近可读取的真实帧。观众页面不控制游戏。" : "SC2 原生 RGB，按帧更新。模型决策等待期间，游戏时间可能暂停。");
      if (!state.mediaKey || !state.mediaKey.startsWith(state.runId + ":")) {
        emptyStage("正在读取真实画面", isTerminal(state.run) ? "优先读取已录制画面；若未提供则显示最后保存帧。" : "画面与公开状态每两秒更新；模型决策期间游戏时间可能暂停。");
        setMedia(isTerminal(state.run) && recordingAvailable ? "recording" : "frame");
      } else if (!isTerminal(state.run) && state.run.status !== "paused") setMedia("frame", true);
      else if (isTerminal(state.run) && !state.mediaKey.endsWith(":terminal")) setMedia(recordingAvailable ? "recording" : "frame");
      else if (!state.mediaFailed && image.complete && image.naturalWidth) mediaLoaded(state.mediaKey, state.mediaKind);
    }
  }
  function drawTactical() {
    if (state.view !== "tactical" && !state.history) return;
    const observation = displayedObservation(); const map = observation.map || {};
    const width = finiteNumber(map.width); const height = finiteNumber(map.height);
    if (width === null || height === null || width <= 0 || height <= 0 || !Array.isArray(observation.units)) {
      emptyStage("该状态缺少可绘制的地图信息", "地图尺寸或单位数据未报告；不会补造位置。"); return;
    }
    byId("stage-empty").classList.add("hidden");
    const bounds = canvas.getBoundingClientRect(); const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.max(1, Math.round(bounds.width * ratio)); canvas.height = Math.max(1, Math.round(bounds.height * ratio));
    const context = canvas.getContext("2d"); context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const margin = 40; const scale = Math.min((bounds.width - margin * 2) / width, (bounds.height - margin * 2) / height);
    if (scale <= 0) return;
    const originX = (bounds.width - width * scale) / 2; const originY = (bounds.height - height * scale) / 2;
    const point = (x, y) => [originX + x * scale, originY + (height - y) * scale];
    context.fillStyle = "#0a1520"; context.fillRect(0, 0, bounds.width, bounds.height);
    context.strokeStyle = "#253c4d"; context.strokeRect(originX, originY, width * scale, height * scale);
    context.strokeStyle = "#182b3a"; context.lineWidth = 1;
    for (let x = 16; x < width; x += 16) { const [px] = point(x, 0); context.beginPath(); context.moveTo(px, originY); context.lineTo(px, originY + height * scale); context.stroke(); }
    for (let y = 16; y < height; y += 16) { const [, py] = point(0, y); context.beginPath(); context.moveTo(originX, py); context.lineTo(originX + width * scale, py); context.stroke(); }
    for (const position of Array.isArray(map.expansions) ? map.expansions : []) {
      if (!Array.isArray(position) || finiteNumber(position[0]) === null || finiteNumber(position[1]) === null) continue;
      const [x, y] = point(position[0], position[1]); context.strokeStyle = "#536777"; context.strokeRect(x - 3, y - 3, 6, 6);
    }
    for (const unit of observation.units) {
      if (finiteNumber(unit?.x) === null || finiteNumber(unit?.y) === null || !["self", "enemy"].includes(unit.owner)) continue;
      if (unit.owner === "enemy" && !visibleEnemy(unit)) continue;
      const [x, y] = point(unit.x, unit.y); const building = unit.is_structure === true;
      context.fillStyle = unit.owner === "enemy" ? "#f49c9c" : building ? "#9acdb7" : "#76dae7";
      if (building) context.fillRect(x - 4, y - 4, 8, 8); else { context.beginPath(); context.arc(x, y, unit.type === "SCV" ? 2 : 3, 0, Math.PI * 2); context.fill(); }
    }
    for (const [position, label, color] of [[map.start, "HOME", "#9acdb7"], [map.enemy_start, "INITIAL HINT", "#bd9a7e"]]) {
      if (!Array.isArray(position) || finiteNumber(position[0]) === null || finiteNumber(position[1]) === null) continue;
      const [x, y] = point(position[0], position[1]); context.strokeStyle = color; context.beginPath(); context.arc(x, y, 9, 0, Math.PI * 2); context.stroke(); context.font = "9px Consolas, monospace"; context.fillStyle = color; context.fillText(label, x + 11, y - 8);
    }
  }
  function render() { renderHeading(); renderAgents(); renderCommunications(); renderEvents(); renderResults(); renderHistory(); refreshMedia(); }
  function stopHistoryPlayback() { if (state.playing !== null) clearInterval(state.playing); state.playing = null; text("history-play", "播放状态记录"); }
  function returnCurrent() { stopHistoryPlayback(); state.history = false; render(); }
  async function tick() {
    const generation = state.generation;
    if (!state.runId || state.busyFor === generation) return;
    state.busyFor = generation;
    try {
      const [run, events, snapshots] = await Promise.all([getJSON(`/api/runs/${state.runId}`), pages("events", state.eventCursor, generation), pages("snapshots", state.snapshotCursor, generation)]);
      if (generation !== state.generation || !events || !snapshots) return;
      state.run = run; state.events = mergeBySeq(state.events, events.rows); state.snapshots = mergeBySeq(state.snapshots, snapshots.rows);
      state.eventCursor = events.cursor; state.snapshotCursor = snapshots.cursor;
      if (Date.now() - state.lastListAt > 10000) await refreshRuns();
      if (generation !== state.generation) return;
      notice(""); connection(`已连接 · ${new Date().toLocaleTimeString("zh-CN", {hour12: false})} 更新`);
      render();
    } catch (error) { if (generation === state.generation) { connection("暂时无法读取最新状态", true); notice(publicText(error.message, "观众服务暂不可用；保留上次已读取的状态。", 200)); } }
    finally { if (state.busyFor === generation) state.busyFor = null; }
  }
  async function refreshRuns() {
    const payload = await getJSON("/api/runs");
    const allowed = new Set(state.config.run_ids);
    state.runs = (Array.isArray(payload.runs) ? payload.runs : []).filter(run => RUN_ID.test(run.id) && allowed.has(run.id));
    state.lastListAt = Date.now(); renderRuns();
  }
  async function selectRun(id) {
    if (!RUN_ID.test(id) || !state.config.run_ids.includes(id) || id === state.runId) return;
    stopHistoryPlayback(); state.generation += 1; state.runId = id; state.run = null; state.events = []; state.snapshots = []; state.eventCursor = 0; state.snapshotCursor = 0; state.history = false; state.historyIndex = 0; state.mediaKey = null; state.mediaKind = null; state.mediaFailed = false;
    image.removeAttribute("src"); image.classList.add("hidden"); canvas.classList.add("hidden"); byId("tactical-legend").classList.add("hidden");
    byId("agent-list").replaceChildren(element("p", "empty", "正在读取本局公开决策…"));
    byId("message-list").replaceChildren(element("p", "empty", "正在读取本局通信…"));
    byId("event-list").replaceChildren(element("p", "empty", "正在读取本局记录…"));
    byId("results-grid").replaceChildren(); text("evaluation-detail", "正在读取本局指标…");
    for (const id of ["minerals", "vespene", "supply", "units", "game-time", "wall-time", "model-name"]) text(id, "—");
    const url = new URL(window.location.href); url.searchParams.set("run", id); window.history.replaceState({}, "", url);
    text("run-title", "正在读取所选实验…"); renderRuns(); emptyStage("正在加载所选实验", "只读取公开记录，不会创建或控制游戏。");
    await tick();
  }
  byId("view-rgb").addEventListener("click", () => { stopHistoryPlayback(); state.history = false; state.view = "rgb"; render(); });
  byId("view-tactical").addEventListener("click", () => { state.view = "tactical"; render(); });
  byId("history-slider").addEventListener("input", event => { stopHistoryPlayback(); state.history = true; state.view = "tactical"; state.historyIndex = Math.max(0, Math.min(state.snapshots.length - 1, Number(event.target.value))); render(); });
  byId("history-current").addEventListener("click", returnCurrent);
  byId("history-play").addEventListener("click", () => {
    if (state.playing !== null) { stopHistoryPlayback(); return; }
    if (!state.snapshots.length) return;
    state.view = "tactical"; state.history = true;
    if (state.historyIndex >= state.snapshots.length - 1) state.historyIndex = 0;
    text("history-play", "停止状态播放");
    state.playing = setInterval(() => { if (state.historyIndex >= state.snapshots.length - 1) { stopHistoryPlayback(); return; } state.historyIndex += 1; render(); }, 500);
    render();
  });
  for (const panel of ["events", "results"]) byId(`tab-${panel}`).addEventListener("click", () => {
    for (const item of ["events", "results"]) { byId(`${item}-panel`).classList.toggle("hidden", item !== panel); byId(`tab-${item}`).classList.toggle("active", item === panel); byId(`tab-${item}`).setAttribute("aria-selected", String(item === panel)); }
  });
  window.addEventListener("resize", drawTactical);
  async function boot() {
    try {
      const config = await getJSON("/api/viewer/config");
      if (config.readonly !== true || !Array.isArray(config.run_ids) || config.run_ids.some(id => typeof id !== "string" || !RUN_ID.test(id))) throw new Error("只读观众配置未通过检查");
      state.config = config; await refreshRuns();
      const requested = new URL(window.location.href).searchParams.get("run");
      const selected = config.run_ids.includes(requested) ? requested : config.run_ids.includes(config.default_run_id) ? config.default_run_id : state.runs[0]?.id;
      if (selected) await selectRun(selected); else { connection("尚无已公开实验"); emptyStage("尚无公开画面", "管理员发布实验后即可观看。"); }
      setInterval(tick, POLL_MS);
    } catch (error) { connection("观众服务未连接", true); notice(publicText(error.message, "无法读取观众配置", 200)); emptyStage("观众服务暂不可用", "页面不会启动新实验。请稍后刷新。"); }
  }
  boot();
}
