/* A same-origin observer. Game state, decisions and results come exclusively from the API. */
const $ = (id) => document.getElementById(id);
const roles = {
  economy: { name: '经济 Agent', label: 'ECONOMY', icon: 'box' },
  production: { name: '生产 Agent', label: 'PRODUCTION', icon: 'grid' },
  tactics: { name: '战术 Agent', label: 'TACTICS', icon: 'crosshair' },
};
const statusNames = { starting: '启动中', running: '执行中', deciding: '决策中', paused: '已暂停', stopping: '停止中', completed: '已完成', stopped: '已停止', failed: '失败', interrupted: '运行中断', idle: '待命', waiting: '等待', thinking: '决策中', executing: '执行中', error: '错误', ready: '就绪', finished: '已结束' };
const resultNames = { success: '任务通过', failure: '任务未通过', incomplete: '未完成', stopped: '已停止', error: '执行错误', Victory: '胜利', Defeat: '战败', Tie: '平局' };
const scenarioNames = { build_and_scout: '建立生产与侦察', full_match: '完整对局', expansion_logistics: '扩张与资源供给', army_readiness: '步兵产能与升级备战', expansion_and_victory: '扩张后赢下对局' };
const activeStatuses = new Set(['starting', 'running', 'deciding', 'paused', 'stopping']);
const allowedPanels = new Set(['timeline', 'results', 'replay']);
const allowedViews = new Set(['rgb', 'tactical']);
const initialSelection = new URLSearchParams(location.search);
let viewSelectedExplicitly = allowedViews.has(initialSelection.get('view'));
const state = { runs: [], run: null, selectedId: null, events: [], eventIds: new Set(), cursor: 0, health: null, capabilities: null, view: viewSelectedExplicitly ? initialSelection.get('view') : 'rgb', panel: allowedPanels.has(initialSelection.get('panel')) ? initialSelection.get('panel') : 'timeline', socket: null, reconnect: null, reconnectAttempt: 0, polling: false, refreshing: false, connection: 'idle', snapshots: [], replayIndex: -1, replayTimer: null, artifacts: [], frameKey: null, frameObjectUrl: null, frameLoading: false, frameAvailable: false, controlsPending: false };
let toastTimer;
let selectVersion = 0;
let stopTargetId = null;
let configScenarioSelection = 'build_and_scout';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function icon(name) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#icon-${name}`);
  node.append(use);
  node.setAttribute('aria-hidden', 'true');
  return node;
}
function string(value, fallback = '—') { return value === null || value === undefined || value === '' ? fallback : String(value); }
function number(value, fallback = '—') { return value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 1 }) : fallback; }
function percentage(value) { return value !== null && value !== undefined && Number.isFinite(Number(value)) ? `${number(Number(value) * 100)}%` : '—'; }
function missionDefinition(scenario) {
  const catalog = state.capabilities?.scenarios || state.health?.capabilities?.scenarios;
  return Array.isArray(catalog) ? catalog.find((mission) => mission?.id === scenario) : null;
}
function scenarioName(scenario) { return missionDefinition(scenario)?.name || scenarioNames[scenario] || string(scenario); }
function seconds(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  const s = Math.max(0, Math.floor(Number(value)));
  return `${Math.floor(s / 60).toString().padStart(2, '0')}:${(s % 60).toString().padStart(2, '0')}`;
}
function formatTime(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleTimeString('zh-CN', { hour12: false }); }
function shortDate(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? '' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); }
function actorName(id) { return roles[id]?.name || string(id, '系统'); }
function safeJson(value) { try { return JSON.stringify(value, null, 2); } catch { return String(value); } }
function toast(message) {
  $('toast').textContent = message;
  $('toast').classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.add('hidden'), 6500);
}
function errorText(payload, fallback) {
  if (typeof payload === 'string') return payload.slice(0, 1600);
  if (Array.isArray(payload?.detail)) return payload.detail.map((x) => `${(x.loc || []).slice(1).join('.')}: ${x.msg || safeJson(x)}`).join('\n');
  if (payload?.detail && typeof payload.detail === 'object') return safeJson(payload.detail).slice(0, 1600);
  return string(payload?.detail || payload?.message || payload?.error, fallback).slice(0, 1600);
}
async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout || 20000);
  try {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options, headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers }, signal: controller.signal });
    const contentType = response.headers.get('content-type') || '';
    const data = contentType.includes('json') ? await response.json() : await response.text();
    if (!response.ok) throw new Error(errorText(data, `请求失败（HTTP ${response.status}）`));
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('服务响应超时，请查看运行服务状态。');
    throw error;
  } finally { clearTimeout(timer); }
}

async function refreshHealth() {
  try {
    const [health, capabilities] = await Promise.all([api('/api/health'), api('/api/capabilities')]);
    state.health = health;
    state.capabilities = capabilities;
    $('system-status').querySelector('.status-dot').className = `status-dot ${['ok', 'healthy', 'ready'].includes(health.status) ? 'live' : 'pending'}`;
    $('system-status-text').textContent = ['ok', 'healthy', 'ready'].includes(health.status) ? '运行服务在线' : '环境需要检查';
    renderModelFooter();
    renderHealth();
    updateConfigNote();
  } catch (error) {
    $('system-status').querySelector('.status-dot').className = 'status-dot error';
    $('system-status-text').textContent = '运行服务不可达';
    $('health-content').replaceChildren(el('p', 'form-error', error.message));
    if (!state.run) showNotice(`无法连接运行服务：${error.message}`, true);
  }
}
function renderModelFooter() {
  const model = state.health?.model || {};
  const serviceName = typeof model === 'string' ? model : (model.model || model.name || model.model_name);
  const serviceReady = typeof model === 'object' && (model.configured || model.available || model.ready);
  const serviceLabel = `当前服务：${serviceName || '未配置'}`;
  const run = state.run;
  if (run?.policy === 'scripted') {
    $('model-name').textContent = '规则脚本 · 不调用模型';
    $('model-description').textContent = `${serviceLabel}（与该局无关）`;
    $('model-dot').className = 'status-dot';
  } else if (run?.policy === 'llm') {
    let historicalName = typeof run.model_name === 'string' ? run.model_name.trim() : '';
    if (!historicalName) {
      const recordedNames = [...new Set(state.events.filter((event) => event.type === 'agent.decision')
        .map((event) => event.data?.model).filter((value) => typeof value === 'string' && value.trim()
          && !['llm', 'scripted', 'scripted-baseline'].includes(value)).map((value) => value.trim()))];
      historicalName = recordedNames.join(' / ');
    }
    $('model-name').textContent = historicalName || '历史模型未记录';
    $('model-description').textContent = historicalName ? `该局模型 · ${serviceLabel}` : `${serviceLabel}（不代表该局）`;
    $('model-dot').className = `status-dot${historicalName ? '' : ' pending'}`;
  } else {
    $('model-name').textContent = serviceName || '模型服务未配置';
    $('model-description').textContent = serviceReady ? '当前服务已配置 · 实际可用性以调用为准' : '尚未配置模型服务';
    $('model-dot').className = `status-dot ${serviceReady ? 'live' : 'pending'}`;
  }
}
function healthEntry(title, description, details) {
  const node = el('div', 'health-entry');
  node.append(el('strong', '', title), el('p', '', description));
  if (details) node.append(el('pre', '', safeJson(details)));
  return node;
}
function renderHealth() {
  const root = $('health-content');
  root.replaceChildren();
  const health = state.health;
  if (!health) { root.append(el('p', '', '尚未取得服务状态。')); return; }
  root.append(healthEntry('运行服务', `状态：${health.status || '未知'} · 版本：${health.version || '未提供'}`));
  const capabilities = health.capabilities || state.capabilities;
  if (capabilities) root.append(healthEntry('后端预检与能力', '原生 SC2 需要引擎、地图和游戏依赖。缺少依赖时不会自动切换到沙盒。', capabilities));
  root.append(healthEntry('模型服务', 'API Key 等凭据不在浏览器展示。配置存在并不代表模型调用已经验证。', health.model));
  root.append(healthEntry('支持范围', '一个原生玩家，对抗内置电脑；经济、生产、战术为功能 Agent。沙盒与规则脚本不能作为原生游戏或模型效果证据。'));
}
function showNotice(message, isError = false) {
  $('notice').textContent = message || '';
  $('notice').classList.toggle('hidden', !message);
  $('notice').classList.toggle('error', isError);
}

async function refreshRuns() {
  try {
    const payload = await api('/api/runs');
    state.runs = Array.isArray(payload) ? payload : (payload.runs || []);
    renderRunList();
    if (!state.selectedId && state.runs.length) {
      const requested = new URLSearchParams(location.search).get('run');
      const selected = state.runs.find((run) => run.id === requested) || state.runs[0];
      await selectRun(selected.id);
    }
  } catch (error) { if (!state.runs.length) $('run-list').replaceChildren(el('div', 'sidebar-empty', `记录获取失败\n${error.message}`)); }
}
function renderRunList() {
  $('run-count').textContent = state.runs.length;
  const filter = $('run-filter').value;
  const runs = state.runs.filter((run) => filter === 'all' || (filter === 'active' ? activeStatuses.has(run.status) : !activeStatuses.has(run.status)));
  const list = $('run-list');
  list.replaceChildren();
  if (!runs.length) {
    const empty = el('div', 'sidebar-empty', filter === 'all' ? '尚无实验记录' : '没有符合条件的实验');
    empty.append(el('br'), el('span', '', filter === 'all' ? '新建一局，开始记录决策与结果。' : '切换筛选查看其他记录。'));
    list.append(empty);
    return;
  }
  for (const run of runs) {
    const button = el('button', `run-item${run.id === state.selectedId ? ' active' : ''}`);
    button.type = 'button';
    button.title = `${run.name || run.id} · ${statusNames[run.status] || run.status}`;
    const top = el('div', 'run-item-top');
    top.append(el('span', `status-dot ${run.status === 'failed' ? 'error' : activeStatuses.has(run.status) ? (run.status === 'paused' ? 'pending' : 'live') : ''}`), el('span', 'run-item-title', run.name || run.map_name || run.id));
    const bottom = el('div', 'run-item-bottom');
    bottom.append(el('span', `run-mode-label ${run.backend === 'sandbox' ? 'sandbox' : ''}`, run.backend === 'sandbox' ? 'SANDBOX' : 'SC2'));
    bottom.append(el('span', '', statusNames[run.status] || run.status));
    bottom.append(el('time', '', shortDate(run.created_at)));
    button.append(top, bottom);
    button.addEventListener('click', () => selectRun(run.id));
    list.append(button);
  }
}
function closeStream() {
  clearTimeout(state.reconnect);
  state.reconnect = null;
  if (state.socket) { state.socket.onclose = null; state.socket.close(); state.socket = null; }
}
async function selectRun(id) {
  if (!id) return;
  const version = ++selectVersion;
  closeStream();
  stopReplay();
  releaseFrame();
  state.selectedId = id;
  state.run = null;
  state.events = [];
  state.eventIds = new Set();
  state.cursor = 0;
  state.snapshots = [];
  state.replayIndex = -1;
  state.artifacts = [];
  state.frameKey = null;
  state.reconnectAttempt = 0;
  state.connection = 'connecting';
  $('replay-slider').disabled = true;
  $('replay-slider').value = '0';
  $('replay-slider').max = '0';
  $('replay-position').textContent = '0 / 0';
  $('replay-play').disabled = true;
  $('exit-replay').disabled = true;
  $('load-replay').textContent = '加载状态记录';
  const url = new URL(location.href);
  url.searchParams.set('run', id);
  history.replaceState(null, '', url);
  renderRunList();
  renderEvents();
  renderArtifacts();
  renderAll();
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (version !== selectVersion) return;
    state.run = run;
    if (run.backend === 'sandbox' && !viewSelectedExplicitly) setView('tactical');
    renderAll();
    await Promise.all([loadEvents(id, version), loadArtifacts(id, version)]);
    if (version === selectVersion) connectStream(id, version);
  } catch (error) {
    if (version !== selectVersion) return;
    state.connection = 'error';
    showNotice(`实验读取失败：${error.message}`, true);
    renderConnection();
  }
}
function addEvents(events) {
  let changed = false;
  for (const event of events || []) {
    const identity = String(event.seq ?? `${event.time}:${event.type}:${event.actor_id}:${event.message}`);
    if (state.eventIds.has(identity)) continue;
    state.eventIds.add(identity);
    state.events.push(event);
    changed = true;
  }
  if (changed) {
    state.events.sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0));
    renderEvents();
    renderMessages();
    renderModelFooter();
    renderInferenceSettings();
  }
}
async function loadEvents(id, version = selectVersion) {
  try {
    const payload = await api(`/api/runs/${encodeURIComponent(id)}/events?after=${state.cursor}&limit=200`);
    if (version !== selectVersion || id !== state.selectedId) return;
    addEvents(payload.events);
    state.cursor = payload.cursor ?? state.cursor;
  } catch { /* The polling loop retries persisted events; socket events remain visible. */ }
}
async function loadArtifacts(id, version = selectVersion) {
  try {
    const payload = await api(`/api/runs/${encodeURIComponent(id)}/artifacts`);
    if (version !== selectVersion || id !== state.selectedId) return;
    state.artifacts = payload.artifacts || [];
    renderArtifacts();
  } catch (error) { if (version === selectVersion) $('artifact-list').replaceChildren(el('span', 'small-muted', `证据文件暂不可读取：${error.message}`)); }
}
function connectStream(id, version) {
  if (version !== selectVersion) return;
  closeStream();
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${protocol}//${location.host}/api/runs/${encodeURIComponent(id)}/stream`);
  state.socket = socket;
  socket.onopen = () => {
    if (version !== selectVersion) { socket.close(); return; }
    state.connection = 'connected'; state.reconnectAttempt = 0; renderConnection();
    refreshSelected();
  };
  socket.onmessage = (message) => {
    if (version !== selectVersion) return;
    try {
      const payload = JSON.parse(message.data);
      if (payload.type === 'state' && payload.data) { state.run = payload.data; renderAll(); }
      if (payload.type === 'event' && payload.data) addEvents([payload.data]);
    } catch { /* Invalid messages do not modify the authoritative state. */ }
  };
  socket.onerror = () => { if (version === selectVersion) { state.connection = 'reconnecting'; renderConnection(); } };
  socket.onclose = () => {
    if (version !== selectVersion) return;
    state.connection = 'reconnecting';
    renderConnection();
    const delay = Math.min(20000, 1000 * 2 ** state.reconnectAttempt++);
    state.reconnect = setTimeout(() => connectStream(id, version), delay);
  };
}
async function refreshSelected() {
  if (!state.selectedId || state.refreshing) return;
  const id = state.selectedId;
  const version = selectVersion;
  state.refreshing = true;
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (version !== selectVersion) return;
    const wasActive = activeStatuses.has(state.run?.status);
    state.run = run;
    if (state.connection !== 'connected') state.connection = 'polling';
    renderAll();
    await loadEvents(id, version);
    if (wasActive && !activeStatuses.has(run.status)) { loadArtifacts(id, version); refreshRuns(); }
  } catch (error) {
    if (version === selectVersion) { state.connection = 'error'; renderConnection(); showNotice(`连接中断，保留最后取得的状态：${error.message}`, true); }
  } finally { state.refreshing = false; }
}

function currentObservation() {
  const observation = state.replayIndex >= 0 ? state.snapshots[state.replayIndex]?.observation : state.run?.observation;
  return observation && typeof observation === 'object' && Object.keys(observation).length ? observation : null;
}
function renderAll() {
  const run = state.run;
  $('mission-title').textContent = run ? (run.name || `${run.map_name || 'SC2'} 实验`) : (state.selectedId ? '正在读取实验' : '等待新的任务');
  $('mission-kicker').textContent = run ? `${run.backend === 'sandbox' ? 'SANDBOX / INTERFACE TEST' : 'STARCRAFT II / EXPERIMENT'}` : 'STARCRAFT II / EXPERIMENT';
  const meta = $('mission-meta'); meta.replaceChildren();
  if (run) {
    const statusClass = ['failed', 'interrupted'].includes(run.status) ? 'failed' : ['paused', 'starting', 'deciding', 'stopping'].includes(run.status) ? 'pending' : ['stopped', 'completed'].includes(run.status) ? 'neutral' : '';
    meta.append(el('span', `status-pill ${statusClass}`, statusNames[run.status] || run.status));
    for (const value of [run.map_name, scenarioName(run.scenario), run.policy === 'llm' ? '真实模型策略' : '规则脚本策略']) { meta.append(el('span', 'meta-separator', '/'), el('span', '', value)); }
  } else meta.append(el('span', '', '选择或创建实验，查看真实执行过程'));
  const warnings = [];
  if (run?.backend === 'sandbox') warnings.push('接口测试沙盒：当前数据来自确定性测试适配器，不是星际争霸Ⅱ游戏，不可作为原生游戏验收证据。');
  if (run?.policy === 'scripted') warnings.push('规则脚本策略：本局不调用大模型，动作由固定基线产生。');
  if (run?.config?.guidance_profile === 'marine_reference_v1') warnings.push('给定 Marine 参考策略：本局测试给定策略下的协同执行，不代表自主战略生成；软目标不改变原生胜负验收。');
  if (state.replayIndex >= 0) warnings.push('正在查看历史状态重建，右侧 Agent 信息与事件列表仍属于所选实验记录；此视图不是原生游戏录像。');
  $('mode-warning').textContent = warnings.join(' ');
  $('mode-warning').classList.toggle('hidden', !warnings.length);
  if (run?.error) showNotice(`运行失败：${typeof run.error === 'string' ? run.error : safeJson(run.error)}`, true);
  else if (state.connection !== 'error') {
    const observationWarnings = run?.observation?.warnings || [];
    showNotice(observationWarnings.length ? observationWarnings.map((item) => typeof item === 'string' ? item : safeJson(item)).join('；') : null);
  }
  $('pause-run').disabled = state.controlsPending || !['starting', 'running', 'deciding'].includes(run?.status);
  $('resume-run').disabled = state.controlsPending || run?.status !== 'paused';
  $('stop-run').disabled = state.controlsPending || !activeStatuses.has(run?.status) || run?.status === 'stopping';
  $('evidence-id').textContent = run ? string(run.id).slice(0, 22).toUpperCase() : 'NO RUN SELECTED';
  $('load-replay').disabled = !run;
  renderResources(); renderStage(); renderAgents(); renderMessages(); renderResults(); renderConnection(); renderModelFooter(); renderInferenceSettings(); renderGuidanceSettings(); updateWallTime();
}
function renderResources() {
  const observation = currentObservation();
  $('mineral-value').textContent = number(observation?.minerals);
  $('gas-value').textContent = number(observation?.vespene);
  $('supply-value').textContent = `${number(observation?.supply_used)} / ${number(observation?.supply_cap)}`;
  $('unit-value').textContent = `${number(observation?.workers)} / ${number(observation?.army_count)}`;
  $('game-time').textContent = seconds(observation?.game_time) === '—' ? '00:00' : seconds(observation.game_time);
}
function updateWallTime() {
  const run = state.run;
  if (!run) { $('wall-time').textContent = '—'; return; }
  const metricSeconds = run.metrics?.wall_seconds ?? run.metrics?.elapsed_seconds;
  if (metricSeconds !== undefined) { $('wall-time').textContent = seconds(metricSeconds); return; }
  const start = new Date(run.created_at).valueOf();
  const end = activeStatuses.has(run.status) ? Date.now() : new Date(run.updated_at).valueOf();
  $('wall-time').textContent = Number.isFinite(start) && Number.isFinite(end) ? seconds((end - start) / 1000) : '—';
}
function renderConnection() {
  const labels = { idle: '未连接对局', connecting: '连接中', connected: '事件流已连接', reconnecting: '重新连接中', polling: '轮询同步', error: '连接已中断' };
  $('stream-label').textContent = state.selectedId ? labels[state.connection] || state.connection : labels.idle;
  $('stream-dot').className = `status-dot ${['connected', 'polling'].includes(state.connection) ? 'live' : state.connection === 'error' ? 'error' : 'pending'}`;
}
function persistSelection() {
  const url = new URL(location.href);
  url.searchParams.set('panel', state.panel);
  url.searchParams.set('view', state.view);
  history.replaceState(null, '', url);
}
function setView(view, explicitlySelected = false) {
  if (!allowedViews.has(view)) return;
  if (explicitlySelected) viewSelectedExplicitly = true;
  state.view = view;
  document.querySelectorAll('.view-tab').forEach((button) => { const active = button.dataset.view === view; button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active)); });
  persistSelection();
  renderStage();
}
function releaseFrame() {
  if (state.frameObjectUrl) URL.revokeObjectURL(state.frameObjectUrl);
  state.frameObjectUrl = null;
  state.frameAvailable = false;
  $('native-frame').removeAttribute('src');
}
async function refreshFrame() {
  const run = state.run;
  if (!run || run.backend === 'sandbox' || state.view !== 'rgb' || state.replayIndex >= 0 || state.frameLoading) return;
  const rendering = run.observation?.rendering;
  if (!run.observation || (rendering && rendering !== 'rgb')) return;
  const key = `${run.id}:${run.round}:${run.observation.game_loop}:${run.status}`;
  if (state.frameKey === key) return;
  state.frameKey = key;
  state.frameLoading = true;
  const id = run.id;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(id)}/frame?v=${encodeURIComponent(key)}`, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok || !(response.headers.get('content-type') || '').startsWith('image/')) { if (id === state.selectedId) { state.frameAvailable = false; renderStage(); } return; }
    const blob = await response.blob();
    if (id !== state.selectedId) return;
    const url = URL.createObjectURL(blob);
    if (state.frameObjectUrl) URL.revokeObjectURL(state.frameObjectUrl);
    state.frameObjectUrl = url;
    state.frameAvailable = true;
    $('native-frame').src = url;
    renderStage();
  } catch { if (id === state.selectedId) { state.frameAvailable = false; renderStage(); } }
  finally { state.frameLoading = false; }
}
function renderStage() {
  const observation = currentObservation();
  const replaying = state.replayIndex >= 0;
  const tactical = state.view === 'tactical';
  const round = replaying ? state.snapshots[state.replayIndex]?.round : state.run?.round;
  $('round-label').textContent = `ROUND ${round === undefined || round === null ? '—' : String(round).padStart(3, '0')}`;
  $('view-label').textContent = tactical ? (replaying ? 'RECORDED STATE / TACTICAL RECONSTRUCTION' : 'TACTICAL STATE / DATA VIEW') : 'NATIVE GAME VIEW';
  $('view-description').textContent = tactical ? (state.run?.backend === 'sandbox' ? '沙盒状态示意 · 非原生游戏' : '游戏状态示意 · 非原生 RGB') : 'SC2 引擎输出 · 原生 RGB';
  $('stage-status').textContent = replaying ? `HISTORY ${state.replayIndex + 1} / ${state.snapshots.length}` : (state.run?.status || 'NO ACTIVE SESSION').toUpperCase();
  const showTactical = tactical && !!observation;
  const showFrame = !tactical && !replaying && state.frameAvailable;
  $('native-frame').classList.toggle('hidden', !showFrame);
  $('tactical-canvas').classList.toggle('hidden', !showTactical);
  $('tactical-legend').classList.toggle('hidden', !showTactical);
  $('stage-empty').classList.toggle('hidden', showTactical || showFrame);
  $('stage-create').classList.toggle('hidden', !!state.run);
  if (!showTactical && !showFrame) {
    if (!state.run) {
      $('stage-empty-title').textContent = state.selectedId ? '正在读取实验状态' : '战场尚未接入';
      $('stage-empty-text').textContent = '原生游戏画面将在引擎启动后显示。\n系统不会用示意画面替代真实游戏画面。';
    } else if (state.run.backend === 'sandbox') {
      $('stage-empty-title').textContent = '沙盒不提供原生游戏画面';
      $('stage-empty-text').textContent = '本局使用接口测试适配器，没有运行星际争霸Ⅱ。\n切换“战术态势”查看沙盒状态示意。';
    } else if (replaying) {
      $('stage-empty-title').textContent = '历史记录不包含原生视频帧';
      $('stage-empty-text').textContent = '切换“战术态势”查看已记录状态。\n原生 SC2Replay 文件可在“回放与证据”下载。';
    } else if (state.run.status === 'failed') {
      $('stage-empty-title').textContent = '游戏环境未能启动';
      $('stage-empty-text').textContent = '查看上方错误和环境检查，确认引擎、地图及运行依赖。\n本局没有可展示的原生游戏画面。';
    } else if (tactical) {
      $('stage-empty-title').textContent = '等待第一份游戏状态';
      $('stage-empty-text').textContent = '单位、建筑和资源将按后端的实际观察绘制。';
    } else {
      $('stage-empty-title').textContent = '原生 RGB 暂不可用';
      $('stage-empty-text').textContent = '游戏运行状态与画面输出分别检查。\n可切换“战术态势”查看已有状态；示意图不会冒充原生画面。';
    }
  }
  $('stage-caption').textContent = replaying ? '历史状态重建仅复现已保存的观察；不代表原生视频或新的执行结果。' : tactical ? '战术视图依据实际 observation 绘制，只显示后端提供的单位与可见敌方；不是原生游戏截图。' : '画面与战术状态分别展示；模型决策等待期间，游戏时间可能暂停。';
  if (showTactical) requestAnimationFrame(drawTactical);
  if (!tactical && !replaying) refreshFrame();
}
function drawTactical() {
  const observation = currentObservation();
  const canvas = $('tactical-canvas');
  if (!observation || canvas.classList.contains('hidden')) return;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width || !height) return;
  const dpr = Math.min(devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.fillStyle = '#0b1520'; ctx.fillRect(0, 0, width, height);
  const map = observation.map || {};
  const mapWidth = Number(map.width) || 128, mapHeight = Number(map.height) || 128;
  const padding = { x: 48, top: 39, bottom: 66 };
  const scale = Math.min((width - padding.x * 2) / mapWidth, (height - padding.top - padding.bottom) / mapHeight);
  const left = (width - mapWidth * scale) / 2, top = padding.top + ((height - padding.top - padding.bottom) - mapHeight * scale) / 2;
  const point = (x, y) => [left + Number(x) * scale, top + (mapHeight - Number(y)) * scale];
  ctx.strokeStyle = '#1b2b3b'; ctx.lineWidth = 1;
  for (let x = 0; x <= mapWidth; x += 8) { const p = point(x, 0), q = point(x, mapHeight); ctx.beginPath(); ctx.moveTo(...p); ctx.lineTo(...q); ctx.stroke(); }
  for (let y = 0; y <= mapHeight; y += 8) { const p = point(0, y), q = point(mapWidth, y); ctx.beginPath(); ctx.moveTo(...p); ctx.lineTo(...q); ctx.stroke(); }
  ctx.strokeStyle = '#344c60'; ctx.strokeRect(left, top, mapWidth * scale, mapHeight * scale);
  ctx.font = '8px Consolas, monospace'; ctx.fillStyle = '#59758d';
  for (let x = 0; x <= mapWidth; x += 32) { const p = point(x, 0); ctx.fillText(String(x), p[0] - 4, p[1] + 13); }
  for (const expansion of map.expansions || []) {
    if (!Array.isArray(expansion) || expansion.length < 2) continue;
    const p = point(...expansion); ctx.strokeStyle = '#8b7048'; ctx.beginPath(); ctx.arc(...p, 5, 0, Math.PI * 2); ctx.stroke();
  }
  if (Array.isArray(map.start)) { const p = point(...map.start); ctx.fillStyle = '#6c9eb7'; ctx.fillText('START', p[0] + 8, p[1] + 12); }
  for (const unit of observation.units || []) {
    if (!Number.isFinite(Number(unit.x)) || !Number.isFinite(Number(unit.y))) continue;
    const p = point(unit.x, unit.y);
    const friendly = String(unit.owner) === String(observation.player_id) || unit.owner === 'self' || unit.owner === 'ally' || unit.owner === undefined;
    const neutral = unit.owner === 'neutral' || unit.owner === 0 || unit.owner === 16;
    ctx.fillStyle = neutral ? '#58778c' : friendly ? (unit.is_structure ? '#7caef1' : '#6ad6e6') : '#ef9292';
    if (unit.is_structure) { ctx.fillRect(p[0] - 4, p[1] - 4, 8, 8); ctx.strokeStyle = friendly ? '#b0ceef' : '#f6b2b2'; ctx.strokeRect(p[0] - 4, p[1] - 4, 8, 8); }
    else { ctx.beginPath(); ctx.arc(...p, neutral ? 2 : 3, 0, Math.PI * 2); ctx.fill(); }
    const health = Number(unit.health), healthMax = Number(unit.health_max);
    if (healthMax > 0 && health >= 0 && health < healthMax) { ctx.fillStyle = '#263b4a'; ctx.fillRect(p[0] - 4, p[1] - 8, 8, 2); ctx.fillStyle = friendly ? '#75c8a3' : '#ef9292'; ctx.fillRect(p[0] - 4, p[1] - 8, 8 * Math.min(1, health / healthMax), 2); }
  }
  if (!observation.units?.length) { ctx.fillStyle = '#617f96'; ctx.font = '11px sans-serif'; ctx.textAlign = 'center'; ctx.fillText('当前观察中没有单位', width / 2, height / 2); ctx.textAlign = 'left'; }
}

function renderAgents() {
  const root = $('agent-list'); root.replaceChildren();
  const agents = state.run?.agents || [];
  for (const [id, role] of Object.entries(roles)) {
    const agent = agents.find((item) => item.id === id) || {};
    const card = el('article', 'agent-card'); card.dataset.role = id;
    const heading = el('div', 'agent-card-heading');
    const mark = el('div', 'agent-role-icon'); mark.append(icon(role.icon));
    const title = el('div', 'agent-role-title'); title.append(el('strong', '', role.name), el('span', '', role.label));
    const status = agent.status || 'idle';
    const badge = el('span', `agent-state ${['thinking', 'deciding', 'executing', 'running'].includes(status) ? 'busy' : status === 'error' || status === 'failed' ? 'error' : ''}`);
    badge.append(el('span', `status-dot ${['thinking', 'deciding', 'executing', 'running'].includes(status) ? 'live' : status === 'error' ? 'error' : ''}`), el('span', '', statusNames[status] || status));
    heading.append(mark, title, badge);
    card.append(heading, el('p', 'agent-goal', agent.goal || (state.run ? '尚未产生决策目标' : '等待实验分配任务')));
    if (agent.last_reason) card.append(el('p', 'agent-reason', agent.last_reason));
    const actions = el('div', 'agent-action-list');
    for (const action of (agent.last_actions || []).slice(0, 4)) actions.append(el('span', 'action-tag', typeof action === 'string' ? action : action.tool || 'action'));
    if (!actions.childElementCount) actions.append(el('span', 'action-tag', 'NO COMMAND'));
    card.append(actions);
    const stats = agent.stats || {};
    const calls = stats.calls ?? stats.model_calls ?? stats.decisions ?? stats.turns;
    const latency = stats.last_latency ?? stats.last_latency_seconds ?? stats.latency_seconds;
    const footer = el('div', 'agent-card-footer');
    footer.append(el('span', '', `模型请求 ${number(calls)} 次`), el('span', '', latency === undefined ? '耗时 —' : `最近 ${number(latency)} s`));
    card.append(footer); root.append(card);
  }
}
function messageItems() {
  const messages = [];
  const seen = new Set();
  const add = (from, to, text, time, seq) => {
    if (!text || typeof text !== 'string') return;
    const key = `${from}:${to}:${text}:${seq ?? time ?? ''}`;
    if (seen.has(key)) return;
    seen.add(key); messages.push({ from, to, text, time, seq });
  };
  for (const event of state.events) {
    if (!/message|communication|comms/.test(event.type || '')) continue;
    const data = event.data || {};
    if (Array.isArray(data.messages)) for (const message of data.messages) add(event.actor_id || data.from, message.to, message.text, event.time, event.seq);
    else add(event.actor_id || data.from || data.sender, data.to || data.recipient || 'all', data.text || data.message?.text || event.message, event.time, event.seq);
  }
  if (!messages.length) for (const agent of state.run?.agents || []) for (const message of agent.messages || []) { if (typeof message === 'string') add(agent.id, 'all', message); else add(message.from || agent.id, message.to || 'all', message.text || message.message, message.time, message.seq); }
  return messages;
}
function renderMessages() {
  const messages = messageItems();
  $('message-count').textContent = messages.length;
  const root = $('message-list');
  const nearBottom = root.scrollHeight - root.clientHeight - root.scrollTop < 40;
  root.replaceChildren();
  if (!messages.length) { const empty = el('div', 'content-empty', '尚无 Agent 通信'); empty.append(el('br'), el('span', '', state.run?.config?.communication === false ? '本局已关闭通信' : '这里仅显示实际产生的消息')); root.append(empty); return; }
  for (const message of messages.slice(-80)) {
    const item = el('article', 'message-item'); const heading = el('div', 'message-item-heading');
    heading.append(el('strong', '', actorName(message.from)), el('span', 'message-to', `→ ${message.to === 'all' ? '全体' : actorName(message.to)}`));
    if (message.time) heading.append(el('time', '', formatTime(message.time)));
    item.append(heading, el('p', '', message.text)); root.append(item);
  }
  if (nearBottom) root.scrollTop = root.scrollHeight;
}
function executionIssueLabel(event) {
  if (event.type === 'command.execution_failed') return '后续执行失败';
  if (event.type === 'command.execution_partial') return '后续部分失败';
  return '';
}
function eventCategory(event) {
  if (executionIssueLabel(event)) return 'error';
  const type = `${event.type || ''} ${event.message || ''}`.toLowerCase();
  if (/error|fail|reject|timeout|invalid|错误|超时|拒绝/.test(type)) return 'error';
  if (/message|communication|comms/.test(type)) return 'message';
  if (/command|receipt|validation|accepted|proposal|outcome|tool/.test(type)) return 'command';
  if (/decision|model|llm|policy|agent/.test(type)) return 'decision';
  return 'lifecycle';
}
function eventMatchesFilter(event, filter) {
  const executionIssue = Boolean(executionIssueLabel(event));
  return filter === 'all' || (filter === 'execution' ? executionIssue : eventCategory(event) === filter
    || (filter === 'command' && executionIssue));
}
function renderEvents() {
  $('event-count').textContent = state.events.length;
  const root = $('event-list');
  const scrollTop = root.scrollTop;
  const wasAtBottom = root.scrollHeight - root.clientHeight - scrollTop < 40;
  const filter = $('event-filter').value;
  const search = $('event-search').value.trim().toLowerCase();
  const events = state.events.filter((event) => eventMatchesFilter(event, filter) && (!search || `${event.message} ${event.type} ${event.actor_id} ${safeJson(event.data)}`.toLowerCase().includes(search)));
  root.replaceChildren();
  if (!events.length) { root.append(el('div', 'content-empty', state.events.length ? '没有符合筛选条件的事件' : '暂无执行事件')); return; }
  for (const event of events.slice(-600)) {
    const row = el('div', `event-row ${eventCategory(event) === 'error' ? 'error' : ''}`);
    row.tabIndex = 0; row.setAttribute('role', 'button'); row.setAttribute('aria-expanded', 'false');
    const issueLabel = executionIssueLabel(event);
    row.title = issueLabel ? `${issueLabel}：初始指令接受后观察到的问题；展开查看实际事件数据` : '展开查看实际事件数据';
    const time = el('span', 'event-time', `${formatTime(event.time)} / R${event.round ?? '—'}`);
    const actor = el('span', 'event-actor', actorName(event.actor_id));
    const message = el('span', 'event-message'); message.append(el('span', 'event-type', event.type || 'event'), document.createTextNode(`${issueLabel ? `${issueLabel} · ` : ''}${event.message || '（无消息正文）'}`));
    row.append(time, actor, message);
    const toggle = () => {
      const existing = row.querySelector('.event-details');
      if (existing) { existing.remove(); row.setAttribute('aria-expanded', 'false'); }
      else { row.append(el('pre', 'event-details', safeJson(event.data || {}))); row.setAttribute('aria-expanded', 'true'); }
    };
    row.addEventListener('click', toggle);
    row.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); } });
    root.append(row);
  }
  root.scrollTop = wasAtBottom ? root.scrollHeight : scrollTop;
}
function metricValue(metrics, ...keys) { for (const key of keys) if (metrics[key] !== undefined && metrics[key] !== null) return metrics[key]; return undefined; }
function metricLatency(metrics, key) {
  const milliseconds = metrics[`${key}_ms`];
  if (milliseconds !== null && milliseconds !== undefined) return Number.isFinite(Number(milliseconds)) ? (Number(milliseconds) / 1000).toFixed(2) : '—';
  const value = metricValue(metrics, key, `${key}_seconds`);
  return value === undefined ? '—' : number(value);
}
function tokenSummary(metrics, agents) {
  const nonnegative = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const callCount = (value) => Number.isSafeInteger(value) && value >= 0;
  const fields = { input_tokens: ['input_tokens', 'prompt_tokens'], output_tokens: ['output_tokens', 'completion_tokens'] };
  const summary = { input_tokens: null, output_tokens: null, derived_fields: [] };
  const ledger = Array.isArray(agents) && agents.length > 0 && callCount(metrics?.model_calls)
    && agents.every((agent) => callCount(agent?.stats?.calls))
    && agents.reduce((total, agent) => total + agent.stats.calls, 0) === metrics.model_calls;
  for (const [field, aliases] of Object.entries(fields)) {
    const reported = aliases.map((key) => metrics?.[key]).find((value) => value !== null && value !== undefined);
    if (reported !== undefined) {
      summary[field] = nonnegative(reported) ? reported : null;
      continue;
    }
    if (!ledger || !agents.every((agent) => agent.stats.calls === 0 || nonnegative(agent.stats[field]))) continue;
    const total = agents.reduce((sum, agent) => sum + (agent.stats.calls === 0 ? 0 : agent.stats[field]), 0);
    if (nonnegative(total)) {
      summary[field] = total;
      summary.derived_fields.push(field);
    }
  }
  return summary;
}
function renderResults() {
  const run = state.run, metrics = run?.metrics || {};
  const tokens = tokenSummary(metrics, run?.agents);
  const evaluation = run?.evaluation || {};
  const stages = Array.isArray(evaluation.stages) ? evaluation.stages : [];
  const stageTotal = metricValue(metrics, 'stages_total') ?? (stages.length || undefined);
  const stageComplete = metricValue(metrics, 'stages_completed') ?? (stages.length ? stages.filter((stage) => stage.complete).length : undefined);
  const nativeResult = metrics.engine_result ?? run?.observation?.result ?? evaluation.engine_result;
  const engineKnown = metrics.engine_result !== undefined || run?.observation?.result !== undefined || evaluation.engine_result !== undefined;
  const gameResult = !run ? '—' : run.backend === 'sandbox' ? '不适用（沙盒）' : resultNames[nativeResult] || (engineKnown ? '尚无终局' : '—');
  const root = $('results-grid'); root.replaceChildren();
  const tiles = [
    ['任务结果', run ? resultNames[run.result] || (activeStatuses.has(run.status) ? '待验收' : '未提供') : '—', '', true],
    ['原生游戏胜负', gameResult, '', true],
    ['当前阶段', evaluation.current_stage?.label || (evaluation.complete ? '全部阶段达标' : '—'), '', true],
    ['阶段完成', stageTotal ? `${number(stageComplete)} / ${number(stageTotal)} · ${percentage(metricValue(metrics, 'stage_completion_rate') ?? (stageComplete / stageTotal))}` : '—', '', true],
    ['决策轮数', number(run?.round), '轮'],
    ['模型调用', number(metricValue(metrics, 'model_calls', 'llm_calls', 'calls')), '次'],
    ['已接受指令', number(metricValue(metrics, 'accepted_commands', 'commands_accepted', 'accepted')), '条'],
    ['已拒绝指令', number(metricValue(metrics, 'rejected_commands', 'commands_rejected', 'rejected')), '条'],
    ['后续执行失败', number(metrics.commands_execution_failed), '条', false, '初始指令被接受后，后续观察确认的执行失败；不计为初始拒绝，也不代表任务完成。旧记录未报告时显示 —。'],
    ['后续部分失败', number(metrics.commands_execution_partial), '条', false, '初始指令被接受后，后续观察确认的部分执行问题；与初始拒绝分别记录。旧记录未报告时显示 —。'],
    ['指令拒绝率', percentage(metricValue(metrics, 'command_rejection_rate')), '', true],
    ['里程碑完成率', percentage(metricValue(metrics, 'milestone_completion_rate')), '', true],
    [tokens.derived_fields.length ? '输入/输出token（角色账本汇总）' : '输入 / 输出 token', `${number(tokens.input_tokens)} / ${number(tokens.output_tokens)}`, '', true,
      tokens.derived_fields.length ? `原始总量未报告：${tokens.derived_fields.join(' / ')}。仅汇总调用次数与全局对齐且用量完整的角色账本；零请求角色计 0，实际调用缺失用量仍显示 —。原始数据未修改。` : '服务端报告的用量；未报告的实际调用用量保持未知。'],
    ['角色有效决策 p50 / p95', run?.policy === 'scripted' ? '不调用模型' : `${metricLatency(metrics, 'latency_p50')} / ${metricLatency(metrics, 'latency_p95')}`, run?.policy === 'scripted' ? '' : 's', true],
    ['整轮决策 p50 / p95', `${metricLatency(metrics, 'round_decision_latency_p50')} / ${metricLatency(metrics, 'round_decision_latency_p95')}`, 's', true],
  ];
  for (const [label, value, unit, text, title] of tiles) { const tile = el('div', 'result-tile'); if (title) tile.title = title; tile.append(el('span', '', label)); const strong = el('strong', text ? 'text-result' : '', value); if (unit) strong.append(el('em', '', unit)); tile.append(strong); root.append(tile); }
  const stageList = $('mission-stages'); stageList.replaceChildren();
  if (!stages.length) stageList.append(el('span', 'small-muted', '本局未记录阶段评估'));
  for (const stage of stages) {
    const card = el('div', `mission-stage${stage.complete ? ' complete' : stage.status === 'active' ? ' active' : ''}`);
    const heading = el('div', 'mission-stage-heading');
    heading.append(el('strong', '', stage.label || stage.id), el('span', '', stage.complete ? '已达标' : stage.censored ? '截止未达标' : stage.status === 'active' ? '当前阶段' : '待解锁'));
    const progress = el('div', 'mission-stage-progress'), bar = el('span');
    bar.style.width = `${Number.isFinite(Number(stage.progress)) ? Math.min(100, Math.max(0, Number(stage.progress) * 100)) : 0}%`;
    progress.append(bar);
    card.append(heading, progress, el('span', 'small-muted', `进度 ${percentage(stage.progress)} · 首次观察达标 ${seconds(stage.achieved_at_game_time)}`));
    stageList.append(card);
  }
  const milestones = $('milestones'); milestones.replaceChildren();
  const entries = run?.milestones || [];
  if (Array.isArray(entries) && entries.length) for (const milestone of entries) {
    const done = milestone.completed || milestone.achieved || milestone.status === 'completed' || milestone.status === 'achieved';
    const chip = el('span', `milestone-chip ${done ? 'done' : ''}`, `${done ? '✓ ' : '○ '}${milestone.name || milestone.label || milestone.id || safeJson(milestone)}`);
    chip.title = `当前观测 ${number(milestone.value)} / ${number(milestone.target)}；首次观察达标 ${seconds(milestone.achieved_at_game_time)}${milestone.censored ? '；截止未达标' : ''}`;
    milestones.append(chip);
  }
  else if (entries && typeof entries === 'object' && Object.keys(entries).length) for (const [name, value] of Object.entries(entries)) milestones.append(el('span', `milestone-chip ${value === true || value?.completed ? 'done' : ''}`, `${value === true || value?.completed ? '✓ ' : '○ '}${name}`));
  else milestones.append(el('span', 'small-muted', '尚无经过观察确认的里程碑'));
}

function setPanel(panel) {
  if (!allowedPanels.has(panel)) return;
  state.panel = panel;
  for (const button of document.querySelectorAll('.evidence-tab')) { const active = button.dataset.panel === panel; button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active)); }
  for (const name of ['timeline', 'results', 'replay']) $(`${name}-panel`).classList.toggle('hidden', name !== panel);
  persistSelection();
  if (panel === 'replay' && state.selectedId) loadArtifacts(state.selectedId);
}
function renderArtifacts() {
  const root = $('artifact-list'); root.replaceChildren();
  if (!state.artifacts.length) { root.append(el('span', 'small-muted', '尚无可下载证据文件；原生回放通常在对局结束后生成。')); return; }
  for (const artifact of state.artifacts) {
    let url;
    try { url = new URL(artifact.url || `/api/runs/${encodeURIComponent(state.selectedId)}/artifacts/${encodeURIComponent(artifact.name)}`, location.origin); } catch { continue; }
    if (url.origin !== location.origin || !url.pathname.startsWith(`/api/runs/${encodeURIComponent(state.selectedId)}/artifacts/`)) continue;
    const link = el('a', 'artifact-link'); link.href = url.href; link.setAttribute('download', artifact.name || 'evidence'); link.append(icon('file'), el('span', '', artifact.name || '证据文件'));
    if (artifact.size !== undefined) link.append(el('small', '', artifact.size > 1024 * 1024 ? `${(artifact.size / 1024 / 1024).toFixed(1)} MB` : `${(artifact.size / 1024).toFixed(1)} KB`));
    root.append(link);
  }
  if (!root.childElementCount) root.append(el('span', 'small-muted', '没有符合本实验下载范围的证据文件。'));
}
function renderInferenceSettings() {
  const root = $('inference-settings'); root.replaceChildren();
  const run = state.run;
  if (!run) { root.append(el('span', 'small-muted', '选择实验后查看已记录的调用设置。')); return; }
  const latest = new Map();
  for (const event of state.events) {
    const settings = event.type === 'agent.decision' ? event.data?.inference_settings : null;
    if (!roles[event.actor_id] || !settings || typeof settings !== 'object' || Array.isArray(settings) || !Object.keys(settings).length) continue;
    const previous = latest.get(event.actor_id);
    if (!previous || Number(event.seq || 0) >= Number(previous.seq || 0)) latest.set(event.actor_id, event);
  }
  if (!latest.size) {
    const requested = run.config?.reasoning_budget_tokens;
    const label = run.policy === 'scripted' ? '规则脚本不调用模型。' : Number.isInteger(requested) && requested >= 0 && requested <= 1024
      ? `请求模式：${requested > 0 ? '有限思考' : '快速'}；尚无已记录的调用设置。`
      : '本局未记录模型决策设置，历史决策模式未知。';
    root.append(el('span', 'small-muted', label));
    return;
  }
  for (const role of Object.keys(roles)) {
    const event = latest.get(role);
    if (!event) { root.append(healthEntry(roles[role].name, '尚无已记录的调用设置。')); continue; }
    const settings = event.data.inference_settings;
    const mode = settings.enable_thinking === true ? '有限思考请求' : settings.enable_thinking === false ? '快速模式请求' : '决策模式未明确记录';
    const budget = Number.isInteger(settings.reasoning_budget_tokens) ? ` · 预算 ${settings.reasoning_budget_tokens}` : '';
    root.append(healthEntry(roles[role].name, `${mode}${budget} · 最近记录于第 ${number(event.round)} 轮。设置代表请求参数，不代表实际思考内容或用量。`, settings));
  }
}
function renderGuidanceSettings() {
  const root = $('guidance-settings'); root.replaceChildren();
  const run = state.run;
  if (!run) { root.append(el('span', 'small-muted', '选择实验后查看实际保存的作战指导。')); return; }
  const profile = run.config?.guidance_profile;
  const label = profile === 'marine_reference_v1'
    ? '已记录配置：给定 Marine 参考策略。该基线测试给定策略下的协同执行，不代表自主战略生成；软目标不是当前事实或强制成功门槛。'
    : profile === 'none' ? '已记录配置：自由协商，未给定 Marine 参考策略。'
      : '本局未明确记录作战指导配置，历史模式未知。';
  root.append(healthEntry('作战指导配置', label, profile === undefined ? undefined : { guidance_profile: profile }));
  const historical = state.replayIndex >= 0;
  const plan = historical ? state.snapshots[state.replayIndex]?.reference_plan : run.reference_plan;
  if (plan && typeof plan === 'object' && !Array.isArray(plan)) {
    root.append(healthEntry(historical ? '该快照记录的参考计划' : '本局已记录的参考计划',
      '以下为实际保存字段；目标数、已观察值和建议阶段分别读取，不代表已集结、已进攻或原生胜利。', plan));
  } else if (profile === 'marine_reference_v1') {
    root.append(el('span', 'small-muted', historical ? '该快照未记录参考计划，不用当前计划补写历史。' : '尚无已记录的参考计划；仅凭配置不推断已执行。'));
  }
}
async function loadReplay() {
  const id = state.selectedId; if (!id) return;
  const version = selectVersion;
  $('load-replay').disabled = true; $('load-replay').textContent = '读取记录…';
  try {
    const snapshots = []; let cursor = 0;
    for (let page = 0; page < 100; page++) {
      const payload = await api(`/api/runs/${encodeURIComponent(id)}/snapshots?after=${cursor}&limit=200`);
      if (version !== selectVersion) return;
      const rows = payload.snapshots || [];
      snapshots.push(...rows);
      const nextCursor = payload.cursor ?? cursor;
      if (!rows.length || rows.length < 200 || nextCursor === cursor) break;
      cursor = nextCursor;
    }
    if (!snapshots.length) { toast('本局尚无可重建的状态记录。'); return; }
    state.snapshots = snapshots;
    $('replay-slider').max = snapshots.length - 1; $('replay-slider').disabled = false;
    $('replay-play').disabled = false; $('exit-replay').disabled = false;
    applyReplay(0);
  } catch (error) { toast(`回放读取失败：${error.message}`); }
  finally { if (version === selectVersion) { $('load-replay').disabled = false; $('load-replay').textContent = '重新加载记录'; } }
}
function applyReplay(index) {
  if (!state.snapshots.length) return;
  state.replayIndex = Math.max(0, Math.min(Number(index), state.snapshots.length - 1));
  $('replay-slider').value = state.replayIndex;
  $('replay-position').textContent = `${state.replayIndex + 1} / ${state.snapshots.length}`;
  setView('tactical'); renderAll();
}
function stopReplay() {
  if (state.replayTimer) clearInterval(state.replayTimer);
  state.replayTimer = null;
  $('replay-play').replaceChildren(icon('play'));
  $('replay-play').setAttribute('aria-label', '播放状态回放');
}
function exitReplay() { stopReplay(); state.replayIndex = -1; $('exit-replay').disabled = true; renderAll(); }
function toggleReplay() {
  if (state.replayTimer) { stopReplay(); return; }
  if (!state.snapshots.length) return;
  if (state.replayIndex >= state.snapshots.length - 1) applyReplay(0);
  $('replay-play').replaceChildren(icon('pause')); $('replay-play').setAttribute('aria-label', '暂停状态回放');
  $('exit-replay').disabled = false;
  state.replayTimer = setInterval(() => { if (state.replayIndex >= state.snapshots.length - 1) stopReplay(); else applyReplay(state.replayIndex + 1); }, 550);
}

function openConfig() { $('config-error').classList.add('hidden'); updateConfigNote(); $('config-dialog').showModal(); }
function applyMissionBudget() {
  const form = $('config-form');
  const scenario = form.elements.scenario.value;
  const mission = missionDefinition(scenario);
  const previousMap = missionDefinition(configScenarioSelection)?.suggested_map || 'Simple64';
  const currentMap = form.elements.map_name.value.trim();
  if (typeof mission?.suggested_map === 'string' && (currentMap === 'Simple64' || currentMap === previousMap)) {
    form.elements.map_name.value = mission.suggested_map;
  }
  configScenarioSelection = scenario;
  const budget = mission?.suggested_budget;
  if (!budget || typeof budget !== 'object') return;
  for (const key of ['max_rounds', 'max_game_seconds', 'advance_loops']) {
    const input = form.elements[key], value = budget[key];
    if (Number.isInteger(value) && value >= Number(input.min) && value <= Number(input.max)) input.value = String(value);
  }
}
function updateConfigNote() {
  const form = $('config-form');
  const backend = form.elements.backend.value, policy = form.elements.policy.value;
  const reasoning = form.elements.reasoning_budget_tokens;
  reasoning.disabled = policy !== 'llm';
  if (reasoning.disabled) reasoning.value = '0';
  const guidance = form.elements.guidance_profile;
  guidance.disabled = policy !== 'llm' || form.elements.scenario.value !== 'full_match' || !form.elements.execution_guidance.checked;
  if (guidance.disabled) guidance.value = 'none';
  const notes = [];
  if (backend === 'sandbox') notes.push('接口沙盒不会运行星际争霸Ⅱ，也不产生原生游戏验收结果。');
  else notes.push('原生 SC2：启动时检查游戏引擎、地图与依赖，缺失时明确失败，不自动回退。');
  if (policy === 'scripted') notes.push('规则脚本是固定动作基线，不代表大模型决策。');
  else {
    notes.push('LLM 策略调用服务端配置的模型；各功能 Agent 分别保存决策状态。');
    notes.push(Number(reasoning.value) > 0 ? '有限思考模式允许更长的决策过程，单轮等待可能增加；实际请求设置保存在本局证据中。' : '快速模式为默认设置；实际请求设置保存在本局证据中。');
  }
  const mission = missionDefinition(form.elements.scenario.value);
  if (mission?.stages?.length > 1) notes.push(`${mission.stages.length} 阶段任务，按真实观察逐步验收；预算为建议，尚非实测成绩。扩张任务需地图存在可达的第二资源区。`);
  if (mission?.suggested_map) notes.push(`推荐${mission.stages?.length > 1 ? '完整对战' : ''}地图：${mission.suggested_map}。手动指定的其他地图会保留。`);
  notes.push(form.elements.execution_guidance.checked ? '状态感知执行指导已开启：提供机械可用性和本角色目标，动作仍由策略决定。' : '状态感知执行指导已关闭：保留原始对照路径。');
  notes.push(guidance.value === 'marine_reference_v1'
    ? '给定 Marine 参考策略：本基线测试给定策略下的协同执行，不代表自主战略生成。软目标不是当前事实或强制验收门槛，动作仍由模型决定。'
    : '作战指导：自由协商，未给定 Marine 参考策略。');
  $('config-mode-note').textContent = notes.join(' ');
  $('config-mode-note').classList.toggle('warning', backend === 'sandbox' || policy === 'scripted');
}
async function startRun(event) {
  event.preventDefault();
  const form = $('config-form');
  if (!form.reportValidity()) return;
  const data = new FormData(form);
  const config = Object.fromEntries(['name', 'backend', 'policy', 'scenario', 'map_name'].map((key) => [key, String(data.get(key) || '').trim()]));
  for (const key of ['seed', 'max_rounds', 'max_game_seconds', 'advance_loops', 'decision_timeout']) config[key] = Number(data.get(key));
  config.reasoning_budget_tokens = config.policy === 'llm' ? Number(data.get('reasoning_budget_tokens') || 0) : 0;
  config.communication = form.elements.communication.checked; config.replay = form.elements.replay.checked;
  config.execution_guidance = form.elements.execution_guidance.checked;
  config.guidance_profile = config.policy === 'llm' && config.scenario === 'full_match' && config.execution_guidance && data.get('guidance_profile') === 'marine_reference_v1' ? 'marine_reference_v1' : 'none';
  const button = $('start-experiment'); button.disabled = true;
  $('config-error').classList.add('hidden');
  try {
    const run = await api('/api/runs', { method: 'POST', body: JSON.stringify(config), timeout: 30000 });
    $('config-dialog').close();
    const id = run.id || run.run?.id;
    if (!id) throw new Error('服务没有返回实验 ID。');
    state.runs = [run, ...state.runs.filter((item) => item.id !== id)];
    await selectRun(id); await refreshRuns();
    toast(config.backend === 'sandbox' ? '接口沙盒已创建；本局不是原生游戏。' : '原生实验已创建，正在检查环境并启动。');
  } catch (error) { $('config-error').textContent = error.message; $('config-error').classList.remove('hidden'); }
  finally { button.disabled = false; }
}
async function controlRun(action) {
  if (!state.selectedId || state.controlsPending) return;
  const id = state.selectedId;
  state.controlsPending = true; renderAll();
  try {
    await api(`/api/runs/${encodeURIComponent(id)}/control`, { method: 'POST', body: JSON.stringify({ action }) });
    toast({ pause: '暂停请求已发送；当前动作可能需要结束后才暂停。', resume: '继续请求已发送。', stop: '停止请求已发送，正在保存结果。' }[action]);
    await refreshSelected(); await refreshRuns();
  } catch (error) { toast(`操作未完成：${error.message}`); }
  finally { state.controlsPending = false; renderAll(); }
}
function requestStop() {
  if (!state.run || state.controlsPending || !activeStatuses.has(state.run.status) || state.run.status === 'stopping') return;
  stopTargetId = state.selectedId;
  $('stop-run-name').textContent = state.run.name || state.run.id;
  $('confirm-stop').disabled = false;
  if (!$('stop-dialog').open) $('stop-dialog').showModal();
}
function confirmStop() {
  const target = stopTargetId;
  stopTargetId = null;
  $('confirm-stop').disabled = true;
  $('stop-dialog').close();
  if (!target || target !== state.selectedId || !activeStatuses.has(state.run?.status) || state.run?.status === 'stopping') {
    toast('实验状态已变化，没有发送新的停止请求。');
    return;
  }
  controlRun('stop');
}

$('new-run').addEventListener('click', openConfig);
for (const [key, min, max] of [['max_rounds', 1, 500], ['max_game_seconds', 10, 3600], ['advance_loops', 8, 448], ['decision_timeout', 1, 120]]) {
  const input = $('config-form').elements[key]; input.min = String(min); input.max = String(max);
}
$('stage-create').addEventListener('click', openConfig);
$('close-config').addEventListener('click', () => $('config-dialog').close());
$('config-form').addEventListener('submit', startRun);
$('config-form').addEventListener('change', updateConfigNote);
$('config-scenario').addEventListener('change', () => { applyMissionBudget(); updateConfigNote(); });
$('system-status').addEventListener('click', () => { renderHealth(); $('health-dialog').showModal(); });
$('close-health').addEventListener('click', () => $('health-dialog').close());
$('refresh-health').addEventListener('click', refreshHealth);
$('run-filter').addEventListener('change', renderRunList);
$('pause-run').addEventListener('click', () => controlRun('pause'));
$('resume-run').addEventListener('click', () => controlRun('resume'));
$('stop-run').addEventListener('click', requestStop);
$('cancel-stop').addEventListener('click', () => $('stop-dialog').close());
$('confirm-stop').addEventListener('click', confirmStop);
$('stop-dialog').addEventListener('close', () => { stopTargetId = null; });
document.querySelectorAll('.view-tab').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view, true)));
document.querySelectorAll('.evidence-tab').forEach((button) => button.addEventListener('click', () => setPanel(button.dataset.panel)));
$('event-filter').addEventListener('change', renderEvents);
$('event-search').addEventListener('input', renderEvents);
$('load-replay').addEventListener('click', loadReplay);
$('replay-play').addEventListener('click', toggleReplay);
$('replay-slider').addEventListener('input', (event) => { stopReplay(); $('exit-replay').disabled = false; applyReplay(event.target.value); });
$('exit-replay').addEventListener('click', exitReplay);
$('native-frame').addEventListener('error', () => { state.frameAvailable = false; renderStage(); });
new ResizeObserver(() => { if (state.view === 'tactical') drawTactical(); }).observe($('battle-stage'));
window.addEventListener('beforeunload', () => { closeStream(); stopReplay(); releaseFrame(); });
setInterval(refreshSelected, 4000);
setInterval(refreshRuns, 15000);
setInterval(refreshHealth, 30000);
setInterval(updateWallTime, 1000);
setPanel(state.panel);
setView(state.view);
renderAll();
await Promise.all([refreshHealth(), refreshRuns()]);
