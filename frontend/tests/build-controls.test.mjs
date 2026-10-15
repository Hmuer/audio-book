import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const ts = require('typescript');

function load(relativePath, dependencies = {}, suffix = '') {
  const source = readFileSync(new URL(relativePath, import.meta.url), 'utf8') + suffix;
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  new Function('exports', 'require', 'module', output)(module.exports, name => dependencies[name] ?? require(name), module);
  return module.exports;
}

const state = load('../src/lib/buildState.ts');

function harness(apiOverrides = {}) {
  const slots = [];
  let cursor = 0;
  let effects = [];
  const react = {
    ...require('react'),
    useMemo(factory) { return factory(); },
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
      return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = { current: initial };
      return slots[index];
    },
    useEffect(effect, deps) {
      const index = cursor++;
      const previous = slots[index];
      if (!previous || deps.some((value, i) => value !== previous.deps[i])) {
        previous?.cleanup?.();
        slots[index] = { deps };
        effects.push(() => { slots[index].cleanup = effect(); });
      }
    },
  };
  const components = load('../src/components/ProjectDetailPage.tsx', {
    react,
    '@/lib/api': { api: apiOverrides, errToLog: e => e.message },
    '@/lib/buildState': state,
    '@/lib/time': { relativeTime: () => '刚刚', parseTime: value => new Date(value) },
    '@/lib/voiceUtils': {},
    './VoicePicker': {},
    './WaveformPlayer': { __esModule: true, default: () => null },
    './ProjectListPage': { StatusBadge: () => null },
  }, '\nexport { BuildRow, BuildsTab, BuildDetailContent };\n');
  return {
    render(name, props) { cursor = 0; return components[name](props); },
    runEffects() { const queued = effects; effects = []; queued.forEach(effect => effect()); },
    dispose() { slots.forEach(slot => slot?.cleanup?.()); },
  };
}

function nodes(tree, predicate) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(child => nodes(child, predicate));
  return [...(predicate(tree) ? [tree] : []), ...nodes(tree.props?.children, predicate)];
}

function text(tree) {
  if (tree == null || typeof tree === 'boolean') return '';
  if (typeof tree !== 'object') return String(tree);
  if (Array.isArray(tree)) return tree.map(text).join('');
  return text(tree.props?.children);
}

function button(tree, label) {
  return nodes(tree, node => node.type === 'button' && text(node) === label)[0];
}

function rowProps(status) {
  return {
    projectId: 'project',
    item: { build_id: 'build', status, total_chapters: 3, completed_chapters: 1, created_at: '2026-10-15' },
    expanded: false, projectBlocked: state.isBuildBlocking(status), playingKey: null,
    onToggleExpand() {}, onTogglePlay() {}, onReload() {}, onBuildUpdated() {},
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

test('构建状态轮询、暂停保护及控制动作矩阵', () => {
  const matrix = {
    queued: [true, true, ['pause', 'stop']], running: [true, true, ['pause', 'stop']],
    pausing: [true, true, ['stop']], paused: [false, true, ['resume', 'stop']],
    stopping: [true, true, []], cancelled: [false, false, []],
    failed: [false, false, []], success: [false, false, []], partial_success: [false, false, []],
    synthesizing: [false, false, []], preparing: [false, false, []], unknown: [false, false, []],
  };
  for (const [status, [poll, blocking, actions]] of Object.entries(matrix)) {
    assert.equal(state.isBuildRunning(status), poll, status);
    assert.equal(state.isBuildBlocking(status), blocking, status);
    assert.deepEqual(['pause', 'resume', 'stop'].filter(action => state.canControlBuild(status, action)), actions, status);
  }
});

test('真实控制 API 使用 POST、返回服务端状态并保留 HTTP 错误', async () => {
  const { api } = load('../src/lib/api.ts');
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push([url, init.method]);
    return { ok: true, status: 200, json: async () => ({ build_id: 'build', status: 'pausing' }) };
  };
  try {
    for (const action of ['Pause', 'Resume', 'Stop']) {
      assert.equal((await api[`build${action}`]('project', 'build')).status, 'pausing');
    }
    assert.deepEqual(calls, ['pause', 'resume', 'stop'].map(action => [`/api/projects/project/builds/build/${action}`, 'POST']));
    for (const status of [409, 404, 403]) {
      globalThis.fetch = async () => ({ ok: false, status, json: async () => ({ detail: '控制被拒绝' }) });
      await assert.rejects(api.buildResume('project', 'build'), new RegExp(`HTTP ${status}: 控制被拒绝`));
    }
  } finally { globalThis.fetch = original; }
});

test('未展开行直接提供对应按钮，运行及控制中禁止删除，停止不能继续', () => {
  for (const status of ['queued', 'running', 'pausing', 'paused', 'stopping', 'cancelled']) {
    const h = harness();
    const tree = h.render('BuildRow', rowProps(status));
    for (const [action, label] of [['pause', '暂停'], ['resume', '继续'], ['stop', '停止']]) {
      assert.equal(!!button(tree, label), state.canControlBuild(status, action), `${status}: ${label}`);
    }
    const deleteButton = nodes(tree, node => node.type === 'button' && String(node.props.title).includes('删除'))[0];
    assert.equal(deleteButton.props.disabled, state.isBuildBlocking(status));
    assert.equal(nodes(tree, node => node.props?.role === 'status').length > 0, status !== 'cancelled');
  }
});

test('暂停请求互斥、按钮禁用并立即应用响应状态', async () => {
  const pending = deferred();
  let calls = 0;
  const props = rowProps('running');
  const h = harness({ buildPause: () => { calls++; return pending.promise; } });
  props.onBuildUpdated = updated => { props.item = { ...props.item, ...updated }; };
  const first = h.render('BuildRow', props);
  const click = button(first, '暂停').props.onClick;
  const request = click();
  await click();
  assert.equal(calls, 1);
  const waiting = h.render('BuildRow', props);
  assert.equal(button(waiting, '请求中…').props.disabled, true);
  assert.equal(button(waiting, '停止').props.disabled, true);
  pending.resolve({ build_id: 'build', status: 'pausing' });
  await request;
  assert.equal(props.item.status, 'pausing');
  assert.equal(button(h.render('BuildRow', props), '停止').props.disabled, false);
});

test('停止必须二次确认，说明保留产物、不可继续及在途计费', async () => {
  let calls = 0;
  const h = harness({ buildStop: async () => { calls++; return { build_id: 'build', status: 'stopping' }; } });
  const props = rowProps('paused');
  button(h.render('BuildRow', props), '停止').props.onClick();
  assert.equal(calls, 0);
  const confirmation = h.render('BuildRow', props);
  assert.match(text(confirmation), /不能继续原构建/);
  assert.match(text(confirmation), /产物和段缓存会保留/);
  assert.match(text(confirmation), /可能继续计费/);
  assert.match(text(confirmation), /ZIP 分片/);
  await button(confirmation, '确认停止').props.onClick();
  assert.equal(calls, 1);
  assert.equal(button(h.render('BuildRow', props), '确认停止'), undefined);
});

test('继续失败显示错误并解除请求锁，刷新暂停态不会自动继续', async () => {
  let calls = 0;
  const h = harness({ buildResume: async () => { calls++; throw new Error('HTTP 409: 输入已变化'); } });
  const props = rowProps('paused');
  const initial = h.render('BuildRow', props);
  h.runEffects();
  assert.equal(calls, 0);
  await button(initial, '继续').props.onClick();
  const failed = h.render('BuildRow', props);
  assert.match(text(nodes(failed, node => node.props?.role === 'alert')), /HTTP 409: 输入已变化/);
  assert.equal(button(failed, '继续').props.disabled, false);
  await button(failed, '继续').props.onClick();
  assert.equal(calls, 2);
});

test('展开详情在暂停及停止终态补拉，控制中保留轮询，暂停后不轮询', async () => {
  const originalInterval = globalThis.setInterval;
  const originalClear = globalThis.clearInterval;
  const timers = new Map();
  let count = 0;
  let calls = 0;
  globalThis.setInterval = fn => { timers.set(++count, fn); return count; };
  globalThis.clearInterval = id => timers.delete(id);
  const detail = { build_id: 'build', artifacts: [{ chapter_idx: 0, status: 'success' }] };
  const h = harness({ buildGet: async () => { calls++; return detail; } });
  const props = { ...rowProps('running'), expanded: true };
  try {
    for (const status of ['running', 'pausing', 'paused', 'stopping', 'cancelled']) {
      props.item = { ...props.item, status };
      props.projectBlocked = state.isBuildBlocking(status);
      h.render('BuildRow', props);
      h.runEffects();
      await Promise.resolve();
      assert.equal(timers.size, state.isBuildRunning(status) ? 1 : 0, status);
    }
    assert.equal(calls, 5);
    const final = h.render('BuildRow', props);
    assert.equal(nodes(final, node => node.props?.detail === detail).length, 1);
  } finally {
    h.dispose();
    globalThis.setInterval = originalInterval;
    globalThis.clearInterval = originalClear;
  }
});

test('暂停态禁用新建并提示继续或停止，不打开构建表单', () => {
  const h = harness();
  const tree = h.render('BuildsTab', {
    projectId: 'project', project: { chapter_count: 3 }, builds: [rowProps('paused').item],
    voices: [], playingKey: null, onTogglePlay() {}, onReload() {}, onBuildUpdated() {},
  });
  assert.equal(button(tree, '开始生成').props.disabled, true);
  assert.match(button(tree, '开始生成').props.title, /继续或停止现有构建/);
  assert.match(text(tree), /已有暂停构建/);
});

test('真实列表徽标显示暂停中、已暂停、停止中、已停止', () => {
  const { StatusBadge } = load('../src/components/ProjectListPage.tsx', {
    '@/lib/api': {}, '@/lib/time': {}, '@/lib/buildState': state, './CreateAudiobookDialog': {},
  });
  for (const [status, label] of [['pausing', '暂停中'], ['paused', '已暂停'], ['stopping', '停止中'], ['cancelled', '已停止']]) {
    assert.equal(text(StatusBadge({ status })), label);
  }
});

test('暂停态已有章节仍提供播放器、MP3 下载与 LRC 下载', async () => {
  const h = harness({ buildChapterSigns: async () => ({ 0: '/signed/chapter.mp3' }) });
  const props = {
    projectId: 'project', projectBlocked: true, playingKey: null, onTogglePlay() {}, onReload() {},
    detail: { build_id: 'build', status: 'paused', speed: 1, artifacts: [{ chapter_idx: 0, status: 'done', title: '第一章' }] },
  };
  h.render('BuildDetailContent', props);
  h.runEffects();
  await Promise.resolve();
  await Promise.resolve();
  const tree = h.render('BuildDetailContent', props);
  const player = nodes(tree, node => node.props?.src === '/signed/chapter.mp3')[0];
  assert.ok(player);
  assert.equal(typeof player.props.onDownload, 'function');
  assert.equal(text(player.props.extraActions), 'LRC');
});

test('迟到的旧详情响应不能覆盖暂停后补拉的产物', async () => {
  const old = deferred();
  const latest = { build_id: 'build', status: 'paused', artifacts: [{ chapter_idx: 0, status: 'done' }] };
  let calls = 0;
  const h = harness({ buildGet: () => ++calls === 1 ? old.promise : Promise.resolve(latest) });
  const props = { ...rowProps('paused'), expanded: true };
  h.render('BuildRow', props);
  h.runEffects();
  props.item = { ...props.item, status: 'cancelled' };
  h.render('BuildRow', props);
  h.runEffects();
  await Promise.resolve();
  old.resolve({ build_id: 'build', status: 'running', artifacts: [] });
  await Promise.resolve();
  assert.equal(nodes(h.render('BuildRow', props), node => node.props?.detail === latest).length, 1);
});


const flush = () => new Promise(resolve => setImmediate(resolve));

function detailProps(artifacts = []) {
  return {
    projectId: 'project', projectBlocked: true, playingKey: null, onTogglePlay() {}, onReload() {},
    detail: { build_id: 'build', status: 'paused', total_chapters: 3, speed: 1, artifacts },
  };
}

function buildsTab(tree) {
  return nodes(tree, node => Array.isArray(node.props?.builds))[0];
}

async function openBuilds(h, props) {
  h.render('default', props);
  h.runEffects();
  await flush();
  button(h.render('default', props), '构建').props.onClick();
  return h.render('default', props);
}

test('reload 在控制响应前发起，迟到响应不能覆盖 pausing、paused、stopping 状态', async () => {
  for (const status of ['pausing', 'paused', 'stopping']) {
    const old = deferred();
    let calls = 0;
    const props = { projectId: 'project', voices: [] };
    const project = { project_id: 'project', name: '项目', status: 'ready', chapter_count: 3 };
    const h = harness({
      projectGet: async () => project,
      buildList: () => ++calls === 1 ? Promise.resolve([rowProps('running').item]) : old.promise,
    });
    try {
      const initial = await openBuilds(h, props);
      const reloading = buildsTab(initial).props.onReload();
      buildsTab(initial).props.onBuildUpdated({ build_id: 'build', status });
      old.resolve([rowProps('running').item]);
      await reloading;
      assert.equal(buildsTab(h.render('default', props)).props.builds[0].status, status);
    } finally { h.dispose(); }
  }
});

test('较新 reload 先完成时，旧 reload 的成功或失败均不覆盖页面', async () => {
  for (const fails of [false, true]) {
    const old = deferred();
    let calls = 0;
    const props = { projectId: 'project', voices: [] };
    const project = { project_id: 'project', name: '项目', status: 'ready', chapter_count: 3 };
    const h = harness({
      projectGet: async () => project,
      buildList: () => ++calls === 2 ? old.promise : Promise.resolve([rowProps(calls === 1 ? 'running' : 'paused').item]),
    });
    try {
      const initial = await openBuilds(h, props);
      const reload = buildsTab(initial).props.onReload;
      const previous = reload();
      await reload();
      if (fails) old.reject(new Error('旧请求失败'));
      else old.resolve([rowProps('running').item]);
      await previous;
      const tree = h.render('default', props);
      assert.equal(buildsTab(tree).props.builds[0].status, 'paused');
      assert.doesNotMatch(text(tree), /旧请求失败/);
    } finally { h.dispose(); }
  }
});

test('pending 转 done 时在相同章节数下补签，已签章节不重复请求', async () => {
  const calls = [];
  const h = harness({ buildChapterSigns: async (_, __, start, count) => {
    calls.push([start, count]);
    return Object.fromEntries(Array.from({ length: count }, (_, i) => [start + i, `/signed/${start + i}.mp3`]));
  } });
  const props = detailProps([{ chapter_idx: 0, status: 'done' }, { chapter_idx: 1, status: 'pending' }]);
  h.render('BuildDetailContent', props);
  h.runEffects();
  await flush();
  assert.deepEqual(calls, [[0, 1]]);
  props.detail = { ...props.detail, artifacts: [{ chapter_idx: 0, status: 'done' }, { chapter_idx: 1, status: 'done' }] };
  h.render('BuildDetailContent', props);
  h.runEffects();
  await flush();
  const tree = h.render('BuildDetailContent', props);
  assert.deepEqual(calls, [[0, 1], [1, 1]]);
  assert.ok(nodes(tree, node => node.props?.src === '/signed/1.mp3').length);
  h.runEffects();
  await flush();
  assert.equal(calls.length, 2);
});

test('补签取消的旧响应不推进签名缓存，后续 done 章节仍补签', async () => {
  const old = deferred();
  let calls = 0;
  const h = harness({ buildChapterSigns: (_, __, start, count) => {
    calls++;
    if (calls === 1) return old.promise;
    return Promise.resolve(Object.fromEntries(Array.from({ length: count }, (_, i) => [start + i, `/new/${start + i}.mp3`])));
  } });
  const props = detailProps([{ chapter_idx: 0, status: 'done' }, { chapter_idx: 1, status: 'pending' }]);
  h.render('BuildDetailContent', props);
  h.runEffects();
  props.detail = { ...props.detail, artifacts: [{ chapter_idx: 0, status: 'done' }, { chapter_idx: 1, status: 'done' }] };
  h.render('BuildDetailContent', props);
  h.runEffects();
  await flush();
  old.resolve({ 0: '/old/0.mp3' });
  await flush();
  const tree = h.render('BuildDetailContent', props);
  assert.ok(nodes(tree, node => node.props?.src === '/new/0.mp3').length);
  assert.ok(nodes(tree, node => node.props?.src === '/new/1.mp3').length);
  assert.equal(nodes(tree, node => node.props?.src === '/old/0.mp3').length, 0);
});

test('切换构建清空章节与 ZIP 签名，迟到签名不污染新构建', async () => {
  const old = deferred();
  const h = harness({
    buildChapterSigns: (_, id) => id === 'build' ? old.promise : Promise.resolve({ 0: '/new.mp3' }),
    buildDownloadAll: async (_, id) => `/${id}.zip`,
  });
  const props = detailProps([{ chapter_idx: 0, status: 'done' }]);
  props.detail.zip_shards = [{ idx: 0, filename: 'old.zip', start_chapter: 1, end_chapter: 3 }];
  h.render('BuildDetailContent', props);
  h.runEffects();
  props.detail = { ...props.detail, build_id: 'new', zip_shards: [] };
  h.render('BuildDetailContent', props);
  h.runEffects();
  await flush();
  old.resolve({ 0: '/old.mp3' });
  await flush();
  const tree = h.render('BuildDetailContent', props);
  assert.ok(nodes(tree, node => node.props?.src === '/new.mp3').length);
  assert.equal(nodes(tree, node => node.props?.src === '/old.mp3' || node.props?.href === '/build.zip').length, 0);
});

test('部分单卷 ZIP 显示章节范围，仅覆盖全部章节时显示全部', async () => {
  for (const [start, end, idx, label] of [[1, 2, 0, '下载第001-002章 ZIP'], [2, 3, 4, '下载第002-003章 ZIP'], [1, 3, 0, '下载全部 ZIP']]) {
    const h = harness({ buildDownloadAll: async (_, __, shard) => `/shard-${shard}.zip` });
    const props = detailProps();
    props.detail.zip_shards = [{ idx, filename: 'part.zip', start_chapter: start, end_chapter: end }];
    h.render('BuildDetailContent', props);
    h.runEffects();
    await flush();
    const tree = h.render('BuildDetailContent', props);
    const link = nodes(tree, node => node.type === 'a' && node.props.href === `/shard-${idx}.zip`)[0];
    assert.equal(text(link), label);
    if (end - start + 1 < 3) assert.doesNotMatch(text(tree), /下载全部 ZIP/);
  }
});

test('projectBlocked 禁用暂停构建及历史构建删除，确认后变为阻塞也不能删除', async () => {
  for (const status of ['paused', 'success', 'cancelled', 'failed']) {
    let calls = 0;
    const h = harness({ buildDelete: async () => { calls++; } });
    const props = { ...rowProps(status), projectBlocked: false };
    const deleteButton = nodes(h.render('BuildRow', props), node => node.type === 'button' && String(node.props.title).includes('删除'))[0];
    assert.equal(deleteButton.props.disabled, false);
    deleteButton.props.onClick();
    assert.ok(button(h.render('BuildRow', props), '确认删除'));
    props.projectBlocked = true;
    const blocked = h.render('BuildRow', props);
    const blockedDelete = nodes(blocked, node => node.type === 'button' && String(node.props.title).includes('删除'))[0];
    assert.equal(blockedDelete.props.disabled, true);
    assert.match(blockedDelete.props.title, /暂停/);
    assert.equal(button(blocked, '确认删除'), undefined);
    blockedDelete.props.onClick();
    assert.equal(button(h.render('BuildRow', props), '确认删除'), undefined);
    assert.equal(calls, 0);
  }
});
