/* Run with: node --test tests/web_execution.test.cjs. No browser, API or game is started. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.style = {};
    this._text = '';
    this.value = '';
    this.scrollTop = this.scrollHeight = this.clientHeight = 0;
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => { const enabled = force === undefined ? !classes.has(name) : force; if (enabled) classes.add(name); else classes.delete(name); return enabled; },
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
}

const web = path.join(__dirname, '..', 'sc2_harness', 'web');
const source = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
const definitions = source.slice(0, source.indexOf("$('new-run').addEventListener"));

function setup(run, events = [], search = '') {
  const elements = new Map();
  const getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const context = vm.createContext({
    URL, URLSearchParams, location: { search, href: `http://localhost/${search}` },
    history: { replaceState: (_state, _unused, url) => { context.savedUrl = String(url); } },
    document: { getElementById, createElement: (tag) => new Element(tag),
      createTextNode: (text) => { const node = new Element('#text'); node.textContent = text; return node; } },
  });
  vm.runInContext(definitions, context);
  context.testRun = run; context.testEvents = events;
  vm.runInContext('state.run = testRun; state.events = testEvents;', context);
  return { context, get: getElementById };
}

function results(metrics) {
  const ui = setup({ backend: 'sc2', status: 'completed', policy: 'llm', metrics, milestones: [] });
  vm.runInContext('renderResults()', ui.context);
  return new Map(ui.get('results-grid').children.map((tile) => [tile.children[0].textContent, tile]));
}

test('ACK-following failures have independent cards and do not change accepted/rejected counts', () => {
  const metrics = { commands_accepted: 9, commands_rejected: 2,
    commands_execution_failed: 3, commands_execution_partial: 1 };
  const before = JSON.stringify(metrics), tiles = results(metrics);
  assert.equal(tiles.get('后续执行失败').children[1].textContent, '3条');
  assert.equal(tiles.get('后续部分失败').children[1].textContent, '1条');
  assert.equal(tiles.get('已接受指令').children[1].textContent, '9条');
  assert.equal(tiles.get('已拒绝指令').children[1].textContent, '2条');
  assert.match(tiles.get('后续执行失败').title, /不计为初始拒绝/);
  assert.equal(JSON.stringify(metrics), before);
});

test('old missing or null execution counters stay unknown; explicit zero stays zero', () => {
  for (const metrics of [{}, { commands_execution_failed: null, commands_execution_partial: null }]) {
    const tiles = results(metrics);
    assert.equal(tiles.get('后续执行失败').children[1].textContent, '—条');
    assert.equal(tiles.get('后续部分失败').children[1].textContent, '—条');
  }
  const tiles = results({ commands_execution_failed: 0, commands_execution_partial: 0 });
  assert.equal(tiles.get('后续执行失败').children[1].textContent, '0条');
  assert.equal(tiles.get('后续部分失败').children[1].textContent, '0条');
});

test('both follow-up outcomes can be located by execution, error, and command filters', () => {
  const { context } = setup(null);
  for (const type of ['command.execution_failed', 'command.execution_partial']) {
    context.event = { type, message: 'engine order result', data: { command_id: 'c1' } };
    for (const filter of ['all', 'execution', 'error', 'command']) {
      context.filter = filter;
      assert.equal(vm.runInContext('eventMatchesFilter(event, filter)', context), true);
    }
    assert.equal(vm.runInContext('eventCategory(event)', context), 'error');
  }
  for (const type of ['command.rejected', 'command.accepted', 'command.completed', 'agent.error']) {
    context.event = { type };
    assert.equal(vm.runInContext("eventMatchesFilter(event, 'execution')", context), false);
  }
});

test('execution events retain real data and keyboard-accessible detail controls', () => {
  const events = ['command.execution_failed', 'command.execution_partial'].map((type, index) => ({
    type, actor_id: 'production', round: index, message: '<unsafe text>', data: { command_id: `c${index}` },
  }));
  const { context, get } = setup(null, events);
  get('event-filter').value = 'execution';
  vm.runInContext('renderEvents()', context);
  const rows = get('event-list').children;
  assert.equal(rows.length, 2);
  for (const row of rows) {
    assert.equal(row.tabIndex, 0);
    assert.equal(row.attributes.role, 'button');
    assert.equal(row.attributes['aria-expanded'], 'false');
    assert.match(row.title, /初始指令接受后/);
    assert.match(row.textContent, /后续/);
    assert.match(row.textContent, /<unsafe text>/); // Text node, never HTML insertion.
  }
});

test('HTML exposes the dedicated filter with an accessible name', () => {
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  const filter = html.match(/<select\b[^>]*id="event-filter"[\s\S]*?<\/select>/)[0];
  assert.match(filter, /aria-label="[^"]+"/);
  assert.match(filter, /<option value="execution">后续执行异常<\/option>/);
});

function configForm(ui, policy = 'llm', budget = '0') {
  const values = { name: 'Native experiment', backend: 'sc2', policy, scenario: 'full_match', map_name: 'Simple96',
    seed: '19', max_rounds: '300', max_game_seconds: '2400', advance_loops: '224', decision_timeout: '45',
    reasoning_budget_tokens: budget, guidance_profile: 'none' };
  const form = ui.get('config-form');
  form.elements = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, { value, disabled: false }]));
  for (const name of ['communication', 'replay', 'execution_guidance']) form.elements[name] = { type: 'checkbox', checked: name !== 'communication' };
  form.reportValidity = () => true;
  ui.get('config-dialog').close = () => {};
  ui.context.FormData = class {
    constructor(element) { this.values = new Map(Object.entries(element.elements)
      .filter(([, field]) => !field.disabled && (field.type !== 'checkbox' || field.checked))
      .map(([key, field]) => [key, field.type === 'checkbox' ? 'on' : field.value])); }
    get(key) { return this.values.get(key) ?? null; }
  };
  return form;
}

test('decision mode has accessible HTML, an explicit fast default, and reproducible 512/1024 choices', () => {
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  const select = html.match(/<select\b[^>]*id="config-reasoning_budget_tokens"[\s\S]*?<\/select>/)[0];
  assert.match(select, /name="reasoning_budget_tokens"/);
  assert.match(select, /aria-label="模型决策模式"/);
  assert.match(select, /aria-describedby="reasoning-mode-help"/);
  assert.match(select, /<option value="0" selected>快速（默认）<\/option>/);
  assert.match(select, /<option value="512">有限思考（512，较慢）<\/option>/);
  assert.match(select, /<option value="1024">有限思考（1024，较慢）<\/option>/);
  assert.match(html, /id="reasoning-mode-help"/);
  assert.match(html, /id="inference-settings"/);
});

test('changing to scripted disables and clears the model budget; switching back retains the fast default', () => {
  for (const budget of ['512', '1024']) {
    const ui = setup(null), form = configForm(ui, 'llm', budget);
    vm.runInContext('updateConfigNote()', ui.context);
    assert.equal(form.elements.reasoning_budget_tokens.disabled, false);
    assert.match(ui.get('config-mode-note').textContent, /单轮等待可能增加/);
    assert.doesNotMatch(ui.get('config-mode-note').textContent, /快速模式为默认设置/);
    form.elements.policy.value = 'scripted';
    vm.runInContext('updateConfigNote()', ui.context);
    assert.equal(form.elements.reasoning_budget_tokens.disabled, true);
    assert.equal(form.elements.reasoning_budget_tokens.value, '0');
    form.elements.policy.value = 'llm';
    vm.runInContext('updateConfigNote()', ui.context);
    assert.equal(form.elements.reasoning_budget_tokens.disabled, false);
    assert.match(ui.get('config-mode-note').textContent, /快速模式为默认设置/);
  }
});

test('actual new-run POST sends the chosen LLM budget and forces scripted to zero without changing other fields', async () => {
  for (const [policy, budget, expected] of [['llm', '0', 0], ['llm', '512', 512], ['llm', '1024', 1024], ['scripted', '512', 0], ['scripted', '1024', 0]]) {
    const ui = setup(null);
    configForm(ui, policy, budget);
    const calls = [];
    ui.context.captureRequest = (path, options) => { calls.push({ path, options }); return { id: 'created' }; };
    vm.runInContext('api = async (path, options) => captureRequest(path, options); selectRun = async () => {}; refreshRuns = async () => {}; toast = () => {};', ui.context);
    await vm.runInContext('startRun({ preventDefault() {} })', ui.context);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, '/api/runs');
    assert.equal(calls[0].options.method, 'POST');
    assert.deepEqual(JSON.parse(calls[0].options.body), { name: 'Native experiment', backend: 'sc2', policy,
      scenario: 'full_match', map_name: 'Simple96', seed: 19, max_rounds: 300, max_game_seconds: 2400,
      advance_loops: 224, decision_timeout: 45, reasoning_budget_tokens: expected,
      communication: false, replay: true, execution_guidance: true, guidance_profile: 'none' });
    assert.equal(ui.get('start-experiment').disabled, false);
  }
});

test('historical mode remains unknown without recorded calls; new configuration is labeled as a request only', () => {
  const ui = setup({ policy: 'llm', config: {} });
  vm.runInContext('renderInferenceSettings()', ui.context);
  assert.match(ui.get('inference-settings').textContent, /历史决策模式未知/);
  ui.context.testRun = { policy: 'llm', config: { reasoning_budget_tokens: 1024 } };
  vm.runInContext('state.run = testRun; renderInferenceSettings()', ui.context);
  assert.match(ui.get('inference-settings').textContent, /请求模式：有限思考；尚无已记录的调用设置/);
  assert.doesNotMatch(ui.get('inference-settings').textContent, /有限思考请求/);
  ui.context.testRun = { policy: 'scripted', config: { reasoning_budget_tokens: 0 } };
  vm.runInContext('state.run = testRun; renderInferenceSettings()', ui.context);
  assert.match(ui.get('inference-settings').textContent, /规则脚本不调用模型/);
});

test('evidence shows each role latest recorded inference profile without inferring consumed reasoning or rewriting raw values', () => {
  const settings = { enable_thinking: true, reasoning_budget_tokens: 1024, max_output_tokens: 1536, history_turns: 1, strict_schema: true };
  const events = [
    { seq: 9, round: 3, type: 'agent.decision', actor_id: 'economy', data: { inference_settings: settings } },
    { seq: 1, round: 1, type: 'agent.decision', actor_id: 'economy', data: { inference_settings: { enable_thinking: false } } },
    { seq: 10, round: 3, type: 'agent.decision', actor_id: 'production', data: { inference_settings: { enable_thinking: false, reasoning_budget_tokens: null } } },
    { seq: 11, round: 3, type: 'agent.message', actor_id: 'tactics', data: { inference_settings: settings } },
  ];
  const before = JSON.stringify(events), ui = setup({ policy: 'llm', config: { reasoning_budget_tokens: 0 } }, events);
  vm.runInContext('renderInferenceSettings()', ui.context);
  const cards = ui.get('inference-settings').children;
  assert.match(cards[0].textContent, /经济 Agent有限思考请求 · 预算 1024 · 最近记录于第 3 轮/);
  assert.match(cards[0].textContent, /不代表实际思考内容或用量/);
  assert.equal(JSON.parse(cards[0].children[2].textContent).reasoning_budget_tokens, 1024);
  assert.match(cards[1].textContent, /生产 Agent快速模式请求/);
  assert.equal(JSON.parse(cards[1].children[2].textContent).reasoning_budget_tokens, null);
  assert.match(cards[2].textContent, /战术 Agent尚无已记录的调用设置/);
  assert.equal(JSON.stringify(events), before);
});

test('decision-mode additions preserve initial run/panel/view selections and unrelated URL parameters', () => {
  const ui = setup(null, [], '?run=historical-id&panel=replay&view=tactical&keep=original');
  assert.equal(vm.runInContext('state.panel', ui.context), 'replay');
  assert.equal(vm.runInContext('state.view', ui.context), 'tactical');
  assert.equal(vm.runInContext("initialSelection.get('run')", ui.context), 'historical-id');
  vm.runInContext('persistSelection()', ui.context);
  const params = new URL(ui.context.savedUrl).searchParams;
  assert.equal(params.get('run'), 'historical-id');
  assert.equal(params.get('keep'), 'original');
  assert.equal(params.get('panel'), 'replay');
  assert.equal(params.get('view'), 'tactical');
});

test('combat guidance selector has a default none option and describes its restricted advisory scope', () => {
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  const select = html.match(/<select\b[^>]*id="config-guidance_profile"[\s\S]*?<\/select>/)[0];
  assert.match(select, /name="guidance_profile"/);
  assert.match(select, /aria-label="作战指导"/);
  assert.match(select, /aria-describedby="guidance-profile-help"/);
  assert.match(select, /disabled/);
  assert.match(select, /<option value="none" selected>自由协商（默认）<\/option>/);
  assert.match(select, /<option value="marine_reference_v1">给定 Marine 参考策略<\/option>/);
  assert.match(html, /id="guidance-settings"/);
});

test('combat guidance is enabled only for LLM full-match guided runs and invalid transitions clear it', () => {
  for (const invalid of ['scripted', 'build_and_scout', 'execution_guidance']) {
    const ui = setup(null), form = configForm(ui);
    form.elements.guidance_profile.value = 'marine_reference_v1';
    vm.runInContext('updateConfigNote()', ui.context);
    assert.equal(form.elements.guidance_profile.disabled, false);
    assert.match(ui.get('config-mode-note').textContent, /给定策略下的协同执行，不代表自主战略生成/);
    assert.match(ui.get('config-mode-note').textContent, /不是当前事实或强制验收门槛/);
    if (invalid === 'scripted') form.elements.policy.value = 'scripted';
    else if (invalid === 'build_and_scout') form.elements.scenario.value = 'build_and_scout';
    else form.elements.execution_guidance.checked = false;
    vm.runInContext('updateConfigNote()', ui.context);
    assert.equal(form.elements.guidance_profile.disabled, true);
    assert.equal(form.elements.guidance_profile.value, 'none');
    form.elements.policy.value = 'llm'; form.elements.scenario.value = 'full_match'; form.elements.execution_guidance.checked = true;
    vm.runInContext('updateConfigNote()', ui.context);
    assert.equal(form.elements.guidance_profile.disabled, false);
    assert.equal(form.elements.guidance_profile.value, 'none');
    assert.match(ui.get('config-mode-note').textContent, /自由协商，未给定/);
  }
});

test('new-run POST preserves the selected reference profile only when the actual run settings permit it', async () => {
  for (const invalid of [null, 'scripted', 'expansion_and_victory', 'execution_guidance']) {
    const ui = setup(null), form = configForm(ui);
    form.elements.guidance_profile.value = 'marine_reference_v1';
    if (invalid === 'scripted') form.elements.policy.value = 'scripted';
    else if (invalid === 'expansion_and_victory') form.elements.scenario.value = invalid;
    else if (invalid === 'execution_guidance') form.elements.execution_guidance.checked = false;
    const calls = [];
    ui.context.captureRequest = (path, options) => { calls.push({ path, options }); return { id: 'created' }; };
    vm.runInContext('api = async (path, options) => captureRequest(path, options); selectRun = async () => {}; refreshRuns = async () => {}; toast = () => {};', ui.context);
    await vm.runInContext('startRun({ preventDefault() {} })', ui.context);
    const config = JSON.parse(calls[0].options.body);
    assert.equal(config.guidance_profile, invalid === null ? 'marine_reference_v1' : 'none');
    assert.equal(config.scenario, form.elements.scenario.value);
    assert.equal(config.execution_guidance, form.elements.execution_guidance.checked);
    assert.equal(config.communication, false);
    assert.equal(config.reasoning_budget_tokens, 0);
  }
});

test('historical guidance reads saved configuration and plan rather than the new-run form or invented defaults', () => {
  const plan = { profile: 'marine_reference_v1', advisory: true, phase: 'form_reference_force',
    soft_targets: [{ id: 'ready_marines', target: 12, observed: 3, known: true, met: false }] };
  const run = { policy: 'llm', config: { guidance_profile: 'marine_reference_v1' }, reference_plan: plan };
  const original = JSON.stringify(run), ui = setup(run);
  configForm(ui).elements.guidance_profile.value = 'none';
  vm.runInContext('renderGuidanceSettings()', ui.context);
  assert.match(ui.get('guidance-settings').textContent, /给定策略下的协同执行，不代表自主战略生成/);
  assert.match(ui.get('guidance-settings').textContent, /不代表已集结、已进攻或原生胜利/);
  assert.deepEqual(JSON.parse(ui.get('guidance-settings').children[1].children[2].textContent), plan);
  assert.equal(JSON.stringify(run), original);
  ui.context.testRun = { policy: 'llm', config: {} };
  vm.runInContext('state.run = testRun; renderGuidanceSettings()', ui.context);
  assert.match(ui.get('guidance-settings').textContent, /历史模式未知/);
  assert.doesNotMatch(ui.get('guidance-settings').textContent, /已记录配置：自由协商/);
  ui.context.testRun = { policy: 'llm', config: { guidance_profile: 'none' } };
  configForm(ui).elements.guidance_profile.value = 'marine_reference_v1';
  vm.runInContext('state.run = testRun; renderGuidanceSettings()', ui.context);
  assert.match(ui.get('guidance-settings').textContent, /已记录配置：自由协商/);
});

test('replay guidance does not backfill an old snapshot with the current reference phase', () => {
  const ui = setup({ config: { guidance_profile: 'marine_reference_v1' },
    reference_plan: { phase: 'recommended_assault_phase', advisory: true } });
  vm.runInContext('state.replayIndex = 0; state.snapshots = [{ observation: {} }]; renderGuidanceSettings()', ui.context);
  assert.match(ui.get('guidance-settings').textContent, /该快照未记录参考计划/);
  assert.doesNotMatch(ui.get('guidance-settings').textContent, /recommended_assault_phase/);
  vm.runInContext("state.snapshots[0].reference_plan = {phase: 'form_reference_force', advisory: true}; renderGuidanceSettings()", ui.context);
  assert.match(ui.get('guidance-settings').textContent, /该快照记录的参考计划/);
  assert.match(ui.get('guidance-settings').textContent, /form_reference_force/);
  assert.doesNotMatch(ui.get('guidance-settings').textContent, /recommended_assault_phase/);
});
