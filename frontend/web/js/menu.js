// 菜单：引擎状态与控制、LLM 服务商配置。
import { api, errorText } from './api.js';
import { $, $$, el, fill, toast, toastError, confirmDialog, fmtBytes, fmtDuration } from './ui.js';
import { on, emit } from './store.js';
import { renderWeb } from './webpanel.js';
import { renderStorage } from './storagepanel.js';
import { createLibrary } from './libpanel.js';

const STATE = {
  running: ['运行中', 'ok'], starting: ['启动中', 'warn'], stopping: ['停止中', 'warn'],
  stopped: ['已停止', 'bad'], unresponsive: ['无响应', 'bad'],
};
const LEVEL_TEXT = { off: '不思考', on: '思考', default: '模型默认', auto: '自动', minimal: 'minimal', low: 'low', medium: 'medium', high: 'high', xhigh: 'xhigh', max: 'max' };
const STYLE_TEXT = {
  openai: 'OpenAI 标准：reasoning_effort（不思考 = none）',
  deepseek: 'DeepSeek：thinking 开关 + reasoning_effort（low / high / max）',
  qwen: '通义千问：enable_thinking 开关',
  none: '不发送思考参数（由模型默认）',
};
const detectStyle = (url) => /deepseek/i.test(url) ? 'deepseek' : /dashscope|aliyuncs/i.test(url) ? 'qwen' : 'openai';
export const levelText = (l) => LEVEL_TEXT[l] || l;

let status = null;
const libs = {};
let panel = 'engine';
let menuVisible = false;

export function initMenu() {
  libs.chars = createLibrary('characters');
  libs.artists = createLibrary('artists');
  $('#menu-nav').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b) showPanel(b.dataset.panel);
  });
  on('tab', (tab) => {
    menuVisible = tab === 'menu';
    if (menuVisible) renderPanel();
  });
  on('status', (s) => { status = s; if (menuVisible && panel === 'engine') renderEngine(); });
  on('goto-panel', (p) => showPanel(p));
  renderEngine();
}

function showPanel(p) {
  panel = p;
  $$('#menu-nav button').forEach(b => b.classList.toggle('active', b.dataset.panel === p));
  $$('.menu-panel').forEach(x => x.classList.toggle('active', x.id === `panel-${p}`));
  renderPanel();
}

function renderPanel() {
  if (panel === 'llm') renderLlm();
  else if (panel === 'web') renderWeb();
  else if (panel === 'storage') renderStorage();
  else if (libs[panel]) libs[panel].render();
  else renderEngine();
}

// ═══════════════════════════ 引擎 ═══════════════════════════
const busy = { comfyui: null, painter: null };

let engineKey = '';

// 结构（状态、按钮、错误）变化时才重建；其余只更新数值，避免每 5 秒重绘吃掉点击。
function renderEngine() {
  const root = $('#panel-engine');
  if (!status) {
    engineKey = '';
    fill(root, engineHead(), el('div', { class: 'muted' }, '读取中…'));
    return;
  }
  const c = status.comfyui, p = status.painter;
  const key = JSON.stringify([c.state, c.pid, c.external, c.error, c.log_tail, !!c.stats, !!c.stats?.devices?.length,
    p.state, p.pid, p.external, p.error, p.log_tail, busy.comfyui, busy.painter]);
  if (key !== engineKey) {
    engineKey = key;
    fill(root, engineHead(), comfyCard(c), painterCard(p));
  }
  for (const [k, v] of Object.entries(engineValues())) {
    const node = root.querySelector(`[data-k="${k}"]`);
    if (!node) continue;
    if (k.endsWith('-m')) node.style.width = v;
    else node.textContent = v;
  }
}

function engineHead() {
  return el('div', { class: 'panel-title' }, el('h2', {}, '引擎'), el('span', { class: 'grow' }),
    el('span', { class: 'muted small' }, '每 5 秒自动刷新'),
    el('button', { class: 'btn ghost small', onclick: () => emit('refresh-status') }, '刷新'));
}

function engineValues() {
  const c = status.comfyui, p = status.painter;
  const st = c.stats;
  const dev = st?.devices?.[0];
  const used = dev ? dev.vram_total - dev.vram_free : null;
  const ram = st ? st.ram_total - st.ram_free : null;
  return {
    'c-pid': c.pid ? `PID ${c.pid}${c.external ? '（启动前已在运行，已接管）' : ''}` : '—',
    'c-up': fmtDuration(c.uptime),
    'c-ver': st?.version ? `ComfyUI ${st.version}` : '—',
    'c-torch': st?.pytorch || '—',
    'c-gpu': dev ? `显存 · ${dev.name.replace(/^cuda:\d+\s*/, '')}` : '显存',
    'c-vram': dev ? `${fmtBytes(used)} / ${fmtBytes(dev.vram_total)}` : '—',
    'c-vram-m': dev ? `${(used / dev.vram_total * 100).toFixed(1)}%` : '0%',
    'c-ram': st ? `${fmtBytes(ram)} / ${fmtBytes(st.ram_total)}` : '—',
    'c-ram-m': st ? `${(ram / st.ram_total * 100).toFixed(1)}%` : '0%',
    'p-pid': p.pid ? `PID ${p.pid}${p.external ? '（已接管）' : ''}` : '—',
    'p-up': fmtDuration(p.uptime),
    'p-queue': String(p.queue ?? '—'),
    'p-run': p.running_job || '无',
  };
}

function badge(state) {
  const [t, cls] = STATE[state] || [state, ''];
  return el('span', { class: `badge ${cls}` }, el('span', { class: `dot ${cls}` }), t);
}

function item(label, key) {
  return el('div', {}, el('span', { class: 'k', 'data-k': key + '-label' }, label), el('span', { class: 'v', 'data-k': key }, ''));
}

function meterItem(labelKey, label, key) {
  return el('div', {}, el('span', { class: 'k', 'data-k': labelKey }, label), el('span', { class: 'v', 'data-k': key }, ''),
    el('div', { class: 'meter' }, el('i', { 'data-k': key + '-m' })));
}

function comfyCard(c) {
  const b = busy.comfyui;
  const btn = (label, action, disabled, kind = '') => el('button', {
    class: `btn small ${kind}${b === action ? ' busy' : ''}`, disabled: disabled || !!b,
    onclick: () => control('comfyui', action),
  }, label);
  const running = c.state === 'running';
  return el('div', { class: 'svc' },
    el('div', { class: 'svc-head' }, el('h3', {}, 'ComfyUI 引擎'), badge(c.state),
      el('div', { class: 'svc-btns' },
        btn('启动', 'start', ['running', 'starting'].includes(c.state)),
        btn('停止', 'stop', c.state === 'stopped'),
        btn('重启', 'restart', c.state === 'stopped'),
        el('button', { class: `btn small ghost${b === 'free' ? ' busy' : ''}`, disabled: !running || !!b, onclick: freeVram }, '释放显存'))),
    el('div', { class: 'kv' },
      item('进程', 'c-pid'), item('运行时长', 'c-up'), item('版本', 'c-ver'), item('PyTorch', 'c-torch'),
      meterItem('c-gpu', '显存', 'c-vram'), meterItem('c-ram-label', '内存', 'c-ram')),
    c.error ? el('div', { class: 'hint warn' }, c.error) : null,
    c.log_tail ? el('pre', { class: 'log' }, c.log_tail) : null);
}

function painterCard(p) {
  const b = busy.painter;
  const btn = (label, action, disabled) => el('button', {
    class: `btn small${b === action ? ' busy' : ''}`, disabled: disabled || !!b,
    onclick: () => control('painter', action),
  }, label);
  return el('div', { class: 'svc' },
    el('div', { class: 'svc-head' }, el('h3', {}, '绘画模块'), badge(p.state),
      el('div', { class: 'svc-btns' },
        btn('启动', 'start', ['running', 'starting'].includes(p.state)),
        btn('停止', 'stop', p.state === 'stopped'),
        btn('重启', 'restart', p.state === 'stopped'))),
    el('div', { class: 'kv' },
      item('进程', 'p-pid'), item('运行时长', 'p-up'), item('排队任务', 'p-queue'), item('正在执行', 'p-run')),
    p.error ? el('div', { class: 'hint warn' }, p.error) : null,
    p.log_tail ? el('pre', { class: 'log' }, p.log_tail) : null);
}

async function control(svc, action, force = false) {
  busy[svc] = action;
  renderEngine();
  try {
    await api.post(`/api/system/${svc}/${action}${force ? '?force=1' : ''}`);
    const name = svc === 'painter' ? '绘画模块' : 'ComfyUI';
    toast({ start: `正在启动${name}…`, stop: `${name}已停止`, restart: `正在重启${name}…` }[action], 'ok');
  } catch (e) {
    if (e.code === 'BUSY' && !force) {
      busy[svc] = null;
      const ok = await confirmDialog('有任务正在生成', `${action === 'stop' ? '停止' : '重启'}会取消正在生成和排队的任务，是否继续？`, '继续', true);
      if (ok) return control(svc, action, true);
    } else toastError(e);
  } finally {
    busy[svc] = null;
    emit('refresh-status');
  }
}

async function freeVram() {
  busy.comfyui = 'free';
  renderEngine();
  try {
    await api.post('/api/engine/free-vram');
    toast('已释放显存', 'ok');
  } catch (e) { toastError(e); }
  finally { busy.comfyui = null; emit('refresh-status'); }
}

// ═══════════════════════════ LLM ═══════════════════════════
const COLLAPSE_KEY = 'anima.llm.collapsed';
const llm = { providers: [], open: {}, search: {}, onlyOn: {}, testing: {}, fetching: {}, editing: null, collapsed: {} };
try { llm.collapsed = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '{}') || {}; } catch { /* 忽略 */ }
const DEFAULT_BASE = { openai: 'https://api.openai.com/v1', anthropic: 'https://api.anthropic.com' };
const FOLD_DEFAULT_OVER = 30;   // 没手动折叠 / 展开过时：模型多于这个数的服务商默认折叠

const isCollapsed = (p) => llm.collapsed[p.id] ?? p.models.length > FOLD_DEFAULT_OVER;
function setCollapsed(ids, v) {
  for (const id of ids) llm.collapsed[id] = v;
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(llm.collapsed)); } catch { /* 忽略 */ }
  drawLlm();
}

async function renderLlm() {
  const root = $('#panel-llm');
  try {
    llm.providers = await api.get('/api/llm/providers');
  } catch (e) { toastError(e); }
  drawLlm();
}

function drawLlm() {
  const root = $('#panel-llm');
  fill(root, 
    el('div', { class: 'panel-title' }, el('h2', {}, 'LLM'), el('span', { class: 'grow' }),
      llm.providers.length > 1 ? el('button', {
        class: 'btn ghost small', onclick: () => {
          const ids = llm.providers.map(p => p.id);
          setCollapsed(ids, !llm.providers.every(isCollapsed));
        },
      }, llm.providers.every(isCollapsed) ? '全部展开' : '全部折叠') : null,
      el('button', { class: 'btn accent small', onclick: () => { llm.editing = 'new'; drawLlm(); } }, '＋ 添加服务商')),
    el('div', { class: 'hint' },
      '用于「AI 填写」：根据描述自动填写提示词字段。支持 OpenAI 兼容格式与 Anthropic 格式。勾选的模型才会出现在 AI 填写中。',
      el('br'), 'Token 保存在本机 frontend/data/settings.json（明文），所有请求由本机前端服务发出。'),
    llm.editing === 'new' ? providerCard(null) : null,
    ...llm.providers.map(providerCard),
    !llm.providers.length && llm.editing !== 'new' ? el('div', { class: 'muted', style: { padding: '30px 0', textAlign: 'center' } }, '还没有服务商，点击右上角添加。') : null);
}

function providerCard(p) {
  const isNew = !p;
  const editing = isNew || llm.editing === p.id;
  const fmtSel = el('select', {},
    el('option', { value: 'openai' }, 'OpenAI 兼容'), el('option', { value: 'anthropic' }, 'Anthropic'));
  fmtSel.value = p?.format || 'openai';
  const name = el('input', { type: 'text', value: p?.name || '', placeholder: '名称，如 OpenAI 官方 / 某中转' });
  const base = el('input', { type: 'text', value: p?.base_url || DEFAULT_BASE[fmtSel.value], placeholder: 'API 地址' });
  const auth = el('select', {}, el('option', { value: 'x-api-key' }, '认证：x-api-key'), el('option', { value: 'bearer' }, '认证：Bearer'));
  auth.value = p?.auth || 'x-api-key';
  const key = el('input', { type: 'password', placeholder: p?.has_key ? `已保存 ${p.api_key_masked}（留空不修改）` : 'API Token', autocomplete: 'off' });
  const authWrap = el('label', { class: 'mini' }, el('span', {}, 'Anthropic 认证方式'), auth);
  const style = el('select', {}, ...Object.entries(STYLE_TEXT).map(([k, v]) => el('option', { value: k }, v)));
  style.value = p?.thinking_style || detectStyle(base.value);
  let styleTouched = !!p?.thinking_style;
  style.addEventListener('change', () => { styleTouched = true; });
  base.addEventListener('input', () => { if (!styleTouched) style.value = detectStyle(base.value); });
  const styleWrap = el('label', { class: 'mini wide' }, el('span', {}, '思考参数方式（各家 OpenAI 兼容接口控制思考的参数不同）'), style);
  const syncFmt = () => { authWrap.hidden = fmtSel.value !== 'anthropic'; styleWrap.hidden = fmtSel.value !== 'openai'; };
  fmtSel.addEventListener('change', () => {
    if (Object.values(DEFAULT_BASE).includes(base.value.trim()) || !base.value.trim()) base.value = DEFAULT_BASE[fmtSel.value];
    syncFmt();
  });
  syncFmt();

  const save = async () => {
    const data = { name: name.value, format: fmtSel.value, base_url: base.value, auth: auth.value, thinking_style: style.value };
    if (key.value) data.api_key = key.value;
    try {
      if (isNew) await api.post('/api/llm/providers', data);
      else await api.put(`/api/llm/providers/${p.id}`, data);
      llm.editing = null;
      toast('已保存', 'ok');
      renderLlm();
    } catch (e) { toastError(e); }
  };

  const form = el('div', { class: 'prov-form' },
    el('label', { class: 'mini' }, el('span', {}, '名称'), name),
    el('label', { class: 'mini' }, el('span', {}, '格式'), fmtSel),
    el('label', { class: 'mini' }, el('span', {}, 'API 地址'), base),
    el('label', { class: 'mini wide' }, el('span', {}, 'Token'), key),
    authWrap, styleWrap);

  if (editing) {
    return el('div', { class: 'prov' },
      el('div', { class: 'prov-head' }, el('h3', {}, isNew ? '添加服务商' : `编辑：${p.name}`)),
      el('div', { class: 'prov-body' }, form,
        el('div', { class: 'prov-btns' },
          el('button', { class: 'btn accent small', onclick: save }, '保存'),
          el('button', { class: 'btn ghost small', onclick: () => { llm.editing = null; drawLlm(); } }, '取消'))));
  }

  const enabledN = p.models.filter(m => m.enabled).length;
  const folded = isCollapsed(p);
  const toggle = () => setCollapsed([p.id], !folded);
  return el('div', { class: `prov${folded ? ' folded' : ''}` },
    el('div', { class: 'prov-head' },
      el('button', { class: 'btn ghost small fold', title: folded ? '展开' : '折叠', onclick: toggle }, folded ? '▸' : '▾'),
      el('h3', { class: 'fold-title', title: folded ? '展开' : '折叠', onclick: toggle }, p.name),
      folded ? el('span', { class: 'muted small' }, p.models.length ? `${p.models.length} 个模型 · 已勾选 ${enabledN} 个` : '未获取模型') : null,
      el('span', { class: 'badge' }, p.format === 'anthropic' ? 'Anthropic' : 'OpenAI 兼容'),
      p.format === 'openai' ? el('span', { class: 'badge', title: STYLE_TEXT[p.thinking_style] }, `思考参数：${(STYLE_TEXT[p.thinking_style] || '').split('：')[0]}`) : null,
      el('span', { class: 'muted small' }, p.base_url),
      el('span', { class: 'grow', style: { flex: 1 } }),
      el('span', { class: 'muted small' }, p.has_key ? `Token ${p.api_key_masked}` : '未填写 Token'),
      el('button', { class: 'btn ghost small', onclick: () => { llm.editing = p.id; drawLlm(); } }, '编辑'),
      el('button', {
        class: 'btn ghost small danger', onclick: async () => {
          if (!await confirmDialog('删除服务商？', `将删除「${p.name}」及其模型设置。`, '删除', true)) return;
          await api.del(`/api/llm/providers/${p.id}`).catch(toastError);
          emit('llm-models-changed');
          renderLlm();
        },
      }, '删除')),
    folded ? null : el('div', { class: 'prov-body' },
      el('div', { class: 'prov-btns' },
        el('button', { class: `btn small${llm.fetching[p.id] ? ' busy' : ''}`, disabled: !!llm.fetching[p.id], onclick: () => fetchModels(p) }, '获取模型列表'),
        el('span', { class: 'muted small' }, p.models.length ? `共 ${p.models.length} 个模型，已勾选 ${enabledN} 个` : '还没有获取模型列表')),
      p.models.length ? modelsTable(p) : null));
}

async function fetchModels(p) {
  llm.fetching[p.id] = true;
  drawLlm();
  try {
    const np = await api.post(`/api/llm/providers/${p.id}/fetch-models`);
    Object.assign(p, np);
    toast(`获取到 ${np.models.length} 个模型`, 'ok');
    emit('llm-models-changed');
  } catch (e) { toastError(e); }
  finally { llm.fetching[p.id] = false; drawLlm(); }
}

function modelsTable(p) {
  const search = el('input', { type: 'text', placeholder: '搜索模型', value: llm.search[p.id] || '' });
  const onlyOn = el('input', { type: 'checkbox', checked: !!llm.onlyOn[p.id] });
  const box = el('div', { class: 'models' });
  const drawRows = () => {
    const q = (llm.search[p.id] || '').toLowerCase();
    const list = p.models.filter(m => (!q || m.id.toLowerCase().includes(q) || (m.name || '').toLowerCase().includes(q))
      && (!llm.onlyOn[p.id] || m.enabled));
    fill(box, 
      el('div', { class: 'mrow head' }, el('span'), el('span', {}, '模型'), el('span', {}, '图片支持'), el('span', {}, '思考强度'), el('span')),
      ...list.slice(0, 300).map(m => modelRow(p, m, drawRows)),
      list.length > 300 ? el('div', { class: 'mrow' }, el('span'), el('span', { class: 'muted small' }, `还有 ${list.length - 300} 个，请用搜索缩小范围`)) : null);
  };
  search.addEventListener('input', () => { llm.search[p.id] = search.value; drawRows(); });
  onlyOn.addEventListener('change', () => { llm.onlyOn[p.id] = onlyOn.checked; drawRows(); });
  drawRows();
  return el('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } },
    el('div', { class: 'models-tools' }, search, el('label', {}, onlyOn, '只看已勾选')), box);
}

function modelRow(p, m, redraw) {
  const patch = async (data) => {
    try {
      const nm = await api.post(`/api/llm/providers/${p.id}/model`, { model_id: m.id, ...data });
      Object.assign(m, nm);
      emit('llm-models-changed');
    } catch (e) { toastError(e); }
    redraw();
  };
  const cb = el('input', { type: 'checkbox', checked: !!m.enabled, onchange: (e) => patch({ enabled: e.target.checked }) });
  const vSel = el('select', { onchange: (e) => patch({ vision: e.target.value === '' ? null : e.target.value === '1' }) },
    el('option', { value: '' }, '未测试'), el('option', { value: '1' }, '支持'), el('option', { value: '0' }, '不支持'));
  vSel.value = m.vision == null ? '' : (m.vision ? '1' : '0');
  const src = { api: '接口提供', test: '实测', manual: '手动' }[m.vision_source] || '';
  const levels = m.thinking?.levels || ['off'];
  const lSel = el('select', { onchange: (e) => patch({ level: e.target.value }) },
    ...levels.map(l => el('option', { value: l }, levelText(l))));
  lSel.value = m.level || levels[0];
  const testing = llm.testing[`${p.id}::${m.id}`];
  const testBtn = el('button', {
    class: `btn ghost small${testing ? ' busy' : ''}`, disabled: !!testing, title: '发送一次很短的请求：验证连通性与识图（会消耗少量额度）',
    onclick: async () => {
      llm.testing[`${p.id}::${m.id}`] = true;
      redraw();
      try {
        const r = await api.post('/api/llm/test', { provider_id: p.id, model_id: m.id, level: m.level });
        if (r.level_note) toast(r.level_note, 'warn', 6000);
        emit('llm-models-changed');
      } catch (e) { toastError(e); }
      finally { llm.testing[`${p.id}::${m.id}`] = false; renderLlm(); }   // 重新读取：测试可能修正了思考档位
    },
  }, '测试');
  const lt = m.last_test;
  let res = null;
  if (lt) {
    const ok = lt.text_ok;
    const parts = [ok ? '连通 ✓' : '连通 ✗', lt.vision === true ? '识图 ✓' : lt.vision === false ? '识图 ✗' : '',
      lt.elapsed != null ? `${lt.elapsed}s` : '', lt.answer ? `回答「${lt.answer}」` : '', lt.expected ? `正确答案 ${lt.expected}` : ''].filter(Boolean);
    res = el('div', { class: `test-res ${ok ? 'good' : 'bad'}` }, `上次测试（${levelText(lt.level || m.level)}）：${parts.join(' · ')}`,
      lt.error && !ok ? el('div', {}, lt.error) : null);
  }
  const canDisable = m.thinking?.can_disable === false ? '（该模型不能关闭思考）' : '';
  return el('div', { class: `mrow${m.enabled ? ' on' : ''}` },
    cb,
    el('div', { class: 'mid', title: m.id }, m.id, m.name && m.name !== m.id ? el('small', {}, m.name) : null, canDisable ? el('small', {}, canDisable) : null),
    el('div', {}, vSel, src ? el('div', { class: 'vsrc' }, src) : null),
    lSel, testBtn, res);
}

export { errorText };
