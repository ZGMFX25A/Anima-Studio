// 绘图页「AI 填写」卡片。
import { api, errorText } from './api.js';
import { $, el, fill, toast, toastError, dropzone } from './ui.js';
import { app, on, emit, setField, saveForm, syncAll, FIELD_NAMES } from './store.js';
import { levelText } from './menu.js';

let models = [];
let imgZone;
let undoSnapshot = null;
let controller = null;
let lastResult = '';      // 上次调用的结果说明，下次调用前一直显示

export function initLlm() {
  const card = $('#ai-card');
  $('[data-collapse="ai-card"]').addEventListener('click', () => {
    card.classList.toggle('collapsed');
    try { localStorage.setItem('anima.aicard.collapsed', card.classList.contains('collapsed') ? '1' : ''); } catch { /* 忽略 */ }
  });
  try { if (localStorage.getItem('anima.aicard.collapsed')) card.classList.add('collapsed'); } catch { /* 忽略 */ }

  imgZone = dropzone($('#llm-images'), {
    max: 3, label: '灵感图', get: () => app.form.llm.images, set: (v) => setField('llm.images', v),
    isDisabled: () => visionModel()?.vision !== true,
  });

  $('#llm-model').addEventListener('change', (e) => {
    app.form.llm.model_key = e.target.value;
    const m = current();
    app.form.llm.level = m ? (m.level || m.levels[0]) : '';
    saveForm();
    renderLevel();
    renderVisionModels();
    refresh();
  });
  $('#llm-vision-model').addEventListener('change', (e) => { app.form.llm.vision_key = e.target.value; saveForm(); refresh(); });
  $('#llm-level').addEventListener('change', (e) => { app.form.llm.level = e.target.value; saveForm(); });
  $('#llm-generate').addEventListener('click', () => run('generate'));
  $('#llm-modify').addEventListener('click', () => run('modify'));
  $('#llm-undo').addEventListener('click', undo);

  on('llm-models-changed', load);
  on('change', refresh);
  on('form-replaced', () => { renderModels(); refresh(); });
  load();
}

async function load() {
  try { models = await api.get('/api/llm/enabled'); } catch { models = []; }
  renderModels();
  refresh();
}

function current() {
  return models.find(m => m.key === app.form.llm.model_key) || null;
}

// 识图模型：'' = 同上（用 AI 填写的模型）；下拉框只列出能看图的模型
function visionModel() {
  const k = app.form.llm.vision_key;
  return (k && models.find(m => m.key === k && m.vision === true)) || current();
}

function renderVisionModels() {
  const sel = $('#llm-vision-model');
  const m = current();
  const list = models.filter(x => x.vision === true);
  if (models.length && app.form.llm.vision_key && !list.some(x => x.key === app.form.llm.vision_key)) {
    app.form.llm.vision_key = '';    // 选过的模型被取消勾选或测出不能看图：回到「同上」
    saveForm();
  }
  const groups = {};
  for (const x of list) (groups[x.provider] ||= []).push(x);
  fill(sel, el('option', { value: '' }, m ? `同上（${m.label}）` : '同上'),
    ...Object.entries(groups).map(([prov, l]) =>
      el('optgroup', { label: prov }, ...l.map(x => el('option', { value: x.key }, x.label)))));
  sel.value = app.form.llm.vision_key;
  sel.disabled = !models.length;
}

function renderModels() {
  const sel = $('#llm-model');
  if (!models.length) {
    fill(sel, el('option', { value: '' }, '未配置（菜单 → LLM）'));
    sel.disabled = true;
  } else {
    sel.disabled = false;
    const groups = {};
    for (const m of models) (groups[m.provider] ||= []).push(m);
    fill(sel, ...Object.entries(groups).map(([prov, list]) =>
      el('optgroup', { label: prov }, ...list.map(m => el('option', { value: m.key }, m.label)))));
    if (!current()) {
      app.form.llm.model_key = models[0].key;
      app.form.llm.level = models[0].level || models[0].levels[0];
      saveForm();
    }
    sel.value = app.form.llm.model_key;
  }
  renderLevel();
  renderVisionModels();
}

function renderLevel() {
  const sel = $('#llm-level');
  const m = current();
  const levels = m?.levels || ['off'];
  fill(sel, ...levels.map(l => el('option', { value: l }, levelText(l))));
  if (!levels.includes(app.form.llm.level)) app.form.llm.level = m?.level && levels.includes(m.level) ? m.level : levels[0];
  sel.value = app.form.llm.level;
  sel.disabled = !m;
  sel.title = m && !levels.includes('off') ? '该模型不能关闭思考，最低只能到最低档' : '';
}

function refresh() {
  const m = current();
  const vm = visionModel();
  const f = app.form;
  const vision = vm?.vision;
  imgZone?.render();
  const pick = models.some(x => x.vision === true) ? '，可以在「识图模型」里另选一个能看图的模型' : '';
  $('#llm-img-note').textContent = !m ? '' : vision === true ? '可附带最多 3 张灵感图：先由识图模型逐张描述，再交给上面的模型生成（不参与绘画）'
    : vision === false ? `当前模型不支持图片${pick}` : `当前模型未测试图片支持，请先在 菜单 → LLM 中点「测试」${pick}`;
  const hints = [];
  const noSee = m && vision !== true;
  if (noSee && f.pose_ref.enabled && f.pose_ref.image) {
    hints.push('识图模型看不到动作参考图：请另选能看图的识图模型，或在描述里写明动作（含朝向、全身/半身），或者让 action 留空。');
  }
  if (noSee && f.character_ref.enabled && f.character_ref.images.length) {
    hints.push('识图模型看不到人物参考图：请另选能看图的识图模型，或在描述里写人物的核心特征，或者让 appearance 留空。');
  }
  if (!controller) {
    const refsSent = !noSee && m && ((f.pose_ref.enabled && f.pose_ref.image) || (f.character_ref.enabled && f.character_ref.images.length));
    $('#llm-status').textContent = lastResult || (refsSent ? '已开启的参考图会先识图，再作为参考交给生成' : '');
  }
  fill($('#llm-hints'), ...hints.map(t => el('div', { class: 'hint warn' }, t)));
  renderPersonaLink();
  const disabled = !m || !!controller;
  $('#llm-generate').disabled = disabled;
  $('#llm-modify').disabled = disabled;
}

function snapshot() {
  return { fields: structuredClone(app.form.fields), aspect_ratio: app.form.aspect_ratio, width: app.form.width, height: app.form.height };
}

async function run(mode) {
  const m = current();
  if (!m) return;
  const f = app.form;
  const vm = visionModel();
  const canSee = vm?.vision === true;
  const images = canSee ? f.llm.images : [];
  if (mode === 'generate' && !f.llm.description.trim() && !images.length) {
    return toast('请先填写描述', 'warn');
  }
  controller = new AbortController();
  lastResult = '';
  const btn = $(mode === 'generate' ? '#llm-generate' : '#llm-modify');
  btn.classList.add('busy');
  refresh();
  const t0 = Date.now();
  // 识图与识别角色同时进行
  const recognize = canSee && (images.length || (f.character_ref.enabled && f.character_ref.images.length)
    || (f.pose_ref.enabled && f.pose_ref.image));
  const first = [recognize && '识图', f.llm.web_character && '识别角色'].filter(Boolean).join(' + ');
  const stages = [first, (f.llm.web_character || f.llm.tag_reference) && '检索', '生成'].filter(Boolean);
  const tick = setInterval(() => { $('#llm-status').textContent = `${stages.join(' → ')}中… ${Math.round((Date.now() - t0) / 1000)}s`; }, 500);
  // 后端出错会自动重试（最多 3 次）；取消时也通知后端，在下一次重试前停下
  const requestId = Math.random().toString(36).slice(2) + Date.now().toString(36);
  const cancelBtn = el('button', { class: 'btn ghost small', onclick: () => {
    controller?.abort();
    api.post('/api/llm/cancel', { request_id: requestId }).catch(() => {});
  } }, '取消');
  $('#llm-undo').before(cancelBtn);
  try {
    const res = await api.post('/api/llm/fill', {
      mode, request_id: requestId, provider_id: m.provider_id, model_id: m.model_id, level: f.llm.level, safety: f.llm.safety,
      description: f.llm.description, user_images: images,
      ...(vm && vm !== m ? { vision_provider_id: vm.provider_id, vision_model_id: vm.model_id } : {}),
      current_fields: { ...f.fields, aspect_ratio: f.aspect_ratio },
      character: { enabled: f.character_ref.enabled, images: f.character_ref.images },
      pose: { enabled: f.pose_ref.enabled, image: f.pose_ref.image, input_type: f.pose_ref.input_type },
      web_character: !!f.llm.web_character, tag_reference: !!f.llm.tag_reference,
      persona_match: f.llm.persona_match !== false,
      persona_linked: f.llm.persona ? { id: f.llm.persona.id, outfit_id: f.llm.persona.outfit_id } : null,
    }, { signal: controller.signal });
    apply(res.fields);
    renderRecognition(res.recognition);
    renderResearch(res.research);
    lastResult = `完成（${res.elapsed.toFixed(1)}s）`;
    for (const w of res.warnings || []) toast(w, 'warn', 6000);
  } catch (e) {
    if (e.name === 'AbortError') lastResult = '已取消';
    else { lastResult = '失败'; toast(`AI 填写失败：${errorText(e)}`, 'error', 8000); }
  } finally {
    clearInterval(tick);
    cancelBtn.remove();
    emit('llm-models-changed');   // 思考档位可能被自动改掉，重读后下拉框随之更新
    btn.classList.remove('busy');
    controller = null;
    refresh();
  }
}

// ───────── 绘图页选用的人设（「填入绘图页」/「从人设选择」时记下，AI 填写时一并附上资料） ─────────
function renderPersonaLink() {
  const box = $('#llm-persona-link');
  const lp = app.form.llm.persona;
  box.hidden = !lp;
  if (!lp) return;
  const on = app.form.llm.persona_match !== false;
  fill(box,
    el('span', { class: `chip${on ? '' : ' off'}`, title: on ? 'AI 填写时会附上这个人设的资料（含当前造型）' : '「人设匹配」已关闭，不会附上' },
      '已选用人设：', el('b', {}, lp.name), lp.outfit_name ? ` · ${lp.outfit_name}` : ' · 仅基础外观',
      el('button', { title: '不再关联这个人设', onclick: () => { setField('llm.persona', null); } }, '×')),
    on ? null : el('span', { class: 'muted small' }, '（人设匹配已关闭）'));
}

// ───────── 识图结果（识图模型对每张图的描述，原样交给了生成） ─────────
function renderRecognition(rec) {
  const box = $('#llm-vision');
  if (!rec) { box.hidden = true; return; }
  const ok = rec.items.filter(x => x.text).length;
  const failed = rec.items.length - ok;
  $('#llm-vision-sum').textContent = `识图结果：${ok} 张${failed ? `，${failed} 张失败` : ''}（${rec.model}）`;
  fill($('#llm-vision-body'), ...rec.items.map(x => el('div', { class: 'vr-item' },
    el('div', { class: 'rs-row' },
      el('span', { class: 'rs-name' }, x.label.replace(/^\[|\]$/g, '')),
      x.error ? el('span', { class: 'badge bad' }, '失败')
        : el('span', { class: 'muted small' }, x.cached ? '缓存（同一张图没有重新识图）' : `${x.elapsed.toFixed(1)}s`)),
    x.error ? el('div', { class: 'rs-alt small', style: { color: 'var(--danger)' } }, x.error)
      : el('div', { class: 'vr-text' }, x.text))));
  box.hidden = false;
}

// ───────── 联网检索的参考资料 ─────────
const STATUS = { library: ['角色库', 'ok'], danbooru: ['Danbooru 新检索', 'ok'], not_found: ['未找到', 'warn'], error: ['出错', 'bad'] };

function renderResearch(r) {
  const box = $('#llm-research');
  if (!r) { box.hidden = true; return; }
  const chars = r.characters || [];
  const arts = r.artists || [];
  const tags = r.tags;
  const pers = r.personas || [];
  const parts = [];
  if (pers.length) parts.push(`${pers.length} 个人设`);
  if (r.characters) parts.push(`${chars.filter(c => c.tag).length} 个角色`);
  if (r.artists && arts.length) parts.push(`${arts.filter(c => c.tag).length} 个画师`);
  if (tags) parts.push(tags.error ? '标签参考不可用' : `${tags.count} 个标签参考`);
  $('#llm-research-sum').textContent = `参考资料：${parts.join(' · ') || '无'}`;
  const row = (c, kind) => {
    const [st, cls] = STATUS[c.status] || [c.status, ''];
    const art = kind === 'artists';
    const url = art ? `https://danbooru.donmai.us/artists/show_or_new?name=${encodeURIComponent(c.tag || '')}`
      : `https://danbooru.donmai.us/wiki_pages/${encodeURIComponent(c.tag || '')}`;
    const [panel, evt, label] = art ? ['artists', 'artists-open', '在画师库中编辑'] : ['chars', 'chars-open', '在角色库中编辑'];
    return el('div', { class: 'rs-row' },
      el('span', { class: 'badge' }, art ? '画师' : '角色'),
      el('span', { class: `badge ${cls}` }, st),
      el('span', { class: 'rs-name' }, c.name),
      c.tag ? el('span', { class: 'rs-tag', title: '标准标签' }, '→ ', art ? '@' : '', c.tag.replace(/_/g, ' ')) : null,
      c.series_tag ? el('span', { class: 'muted small' }, c.series_tag.replace(/_/g, ' ')) : null,
      c.post_count ? el('span', { class: 'muted small' }, `${c.post_count} 张`) : null,
      c.edited ? el('span', { class: 'muted small' }, '已手动编辑') : null,
      el('span', { class: 'grow' }),
      c.tag && c.source !== 'manual' ? el('a', { class: 'small', href: url, target: '_blank', rel: 'noreferrer' }, 'Danbooru') : null,
      c.id ? el('button', { class: 'btn ghost small', onclick: () => { emit('goto', 'menu'); emit('goto-panel', panel); emit(evt, c.id); } }, label) : null,
      c.alternatives?.length ? el('div', { class: 'rs-alt muted small' }, '其他候选：', c.alternatives.map(t => t.replace(/_/g, ' ')).join('、')) : null,
      c.error ? el('div', { class: 'rs-alt small', style: { color: 'var(--danger)' } }, c.error) : null);
  };
  const personaRow = (x) => el('div', { class: 'rs-row' },
    el('span', { class: 'badge ok' }, '人设'),
    el('span', { class: 'rs-name' }, x.name),
    el('span', { class: 'muted small' }, x.matched ? `描述中的「${x.matched}」` : '绘图页选用'),
    x.outfit ? el('span', { class: 'muted small' }, `造型 ${x.outfit}`) : null,
    el('span', { class: 'grow' }),
    el('button', { class: 'btn ghost small', onclick: () => { emit('goto', 'personas'); emit('persona-open', x.id); } }, '打开人设'));
  const rows = [...pers.map(personaRow), ...chars.map(c => row(c, 'characters')), ...arts.map(c => row(c, 'artists'))];
  if (r.characters && !chars.length && !arts.length) rows.push(el('div', { class: 'muted small' }, '描述中没有识别到作品角色或画师'));
  let tagNode = null;
  if (tags) {
    const meta = tags.error ? tags.error
      : [tags.cached ? '来自缓存（未扣额度）' : null, tags.remaining != null ? `今日剩余 ${tags.remaining} 点` : null,
        tags.pool === 'personal' ? '个人额度' : '公共池'].filter(Boolean).join(' · ');
    tagNode = el('div', { class: 'rs-tags' },
      el('div', { class: 'muted small' }, `标签参考：${meta}`),
      tags.items?.length ? el('div', { class: 'chips' }, tags.items.map(t => el('span', { class: 'chip' }, t.replace(/_/g, ' ')))) : null);
  }
  fill($('#llm-research-body'), rows, tagNode);
  box.hidden = false;
}

function apply(out) {
  undoSnapshot = snapshot();
  const changed = [];
  for (const k of FIELD_NAMES) {
    const v = out[k] ?? '';
    if ((app.form.fields[k] || '') !== v) { app.form.fields[k] = v; changed.push(k); }
  }
  const presets = app.boot.defaults.aspect_presets;
  if (out.aspect_ratio && presets[out.aspect_ratio] && out.aspect_ratio !== app.form.aspect_ratio) {
    app.form.aspect_ratio = out.aspect_ratio;
    [app.form.width, app.form.height] = presets[out.aspect_ratio];
    changed.push('aspect_ratio');
  }
  saveForm();
  syncAll();
  emit('change', 'fields');
  for (const k of changed) {
    const node = document.querySelector(`[data-field="${k}"]`);
    if (node) { node.classList.remove('flash'); void node.offsetWidth; node.classList.add('flash'); }
  }
  $('#llm-undo').disabled = false;
  if (!changed.length) toast('LLM 没有修改任何字段', 'info');
}

function undo() {
  if (!undoSnapshot) return;
  Object.assign(app.form, { ...undoSnapshot, fields: undoSnapshot.fields });
  undoSnapshot = null;
  saveForm();
  syncAll();
  emit('change', 'fields');
  $('#llm-undo').disabled = true;
  toast('已撤销 AI 填写', 'ok', 1500);
}

export { toastError };
