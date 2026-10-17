// 绘图页：模型与采样、提示词字段、参考、高级、生成与预览。
import { api, errorText } from './api.js';
import { $, $$, el, fill, toast, toastError, dropzone, lightbox, confirmDialog, fmtDuration, dragScroll } from './ui.js';
import { app, bindAll, syncAll, setField, saveForm, on, emit, modelInfo, refHashesInUse } from './store.js';
import { showPromptModal, reuseDialog } from './records.js';
import { attachArtistPicker, artistHints, renderArtistHints } from './artistpick.js';
import { attachOutfitSearch } from './outfitpick.js';

const STAGE_TEXT = { queued: '排队中', preparing: '准备中', base: '绘制中', refine: '精细重绘', upscale: '超分', saving: '保存中', done: '完成' };
const TERMINAL = ['succeeded', 'failed', 'cancelled'];
const session = { jobs: [], selected: null, records: {} };
let pollTimer = null;
let crZone, prZone;

// ───────────────────────────── 初始化 ─────────────────────────────
export function initDraw() {
  const d = app.boot.defaults;
  attachArtistPicker($('input[data-field="artist"]'));     // 输入时从画师库给候选
  attachOutfitSearch($('#outfit-search'));                 // AI 填写描述框下的服装检索栏：复制服装库的标签贴进描述
  fill($('#aspect'), 
    ...Object.keys(d.aspect_presets).map(k => el('option', { value: k }, k)),
    el('option', { value: 'custom' }, '自定义'));
  fill($('#count-list'), ...d.count_suggestions.map(c => el('option', { value: c })));
  fillModelOptions();
  fillSamplerOptions();

  bindAll($('#page-draw'));
  dragScroll($('#strip'));
  segmented($('#seed-mode'), () => app.form.seed_mode, (v) => {
    setField('seed_mode', v);
    if (v === 'locked' && app.form.seed == null && currentSeed() != null) setField('seed', currentSeed());
  });
  segmented($('#pr-type'), () => app.form.pose_ref.input_type, (v) => setField('pose_ref.input_type', v));

  crZone = dropzone($('#cr-images'), {
    max: 3, label: '添加', get: () => app.form.character_ref.images,
    set: (v) => setField('character_ref.images', v),
  });
  prZone = dropzone($('#pr-image'), {
    max: 1, label: '上传', get: () => (app.form.pose_ref.image ? [app.form.pose_ref.image] : []),
    set: (v) => setField('pose_ref.image', v[0] || null),
  });

  $('#aspect').addEventListener('change', () => {
    if (app.form.aspect_ratio === 'custom') return;
    const [w, h] = app.boot.defaults.aspect_presets[app.form.aspect_ratio];
    app.form.width = w; app.form.height = h;
    saveForm();
  });
  $('#generate').addEventListener('click', submit);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && $('#page-draw').classList.contains('active')) {
      e.preventDefault();
      submit();
    }
  });
  $('#seed-last').addEventListener('click', () => {
    const s = currentSeed();
    if (s == null) return toast('还没有生成过图片', 'warn');
    app.form.seed_mode = 'locked';
    setField('seed', s);
    toast(`已锁定种子 ${s}`, 'ok');
  });
  $('#stage-cancel').addEventListener('click', cancelSelected);
  $('#stage-error-close').addEventListener('click', () => {
    const j = selectedJob();
    if (j) dismissJob(j);
  });
  $('#stage-img').addEventListener('error', () => {   // 文件已不存在：直接从本次结果里去掉
    const j = selectedJob();
    if (j) removeJob(j.job_id);
  });
  $('#stage-img').addEventListener('click', () => {   // 大图浏览，可左右切换本次生成的所有图
    const done = doneJobs();
    const idx = done.findIndex(j => j.job_id === session.selected);
    if (idx >= 0) lightbox(done.map(j => ({ src: `/api/files/image/${j.gallery_id}`, caption: `seed ${j.params?.seed ?? ''}` })), idx);
  });
  $('#res-reuse').addEventListener('click', async () => {
    const j = selectedJob();
    if (j?.gallery_id) reuseDialog(await getRecord(j.gallery_id));
  });
  $('#res-delete').addEventListener('click', deleteSelected);
  $('#res-prompt').addEventListener('click', async () => {
    const j = selectedJob();
    if (j?.gallery_id) showPromptModal(await getRecord(j.gallery_id));
  });
  $('#res-fav').addEventListener('click', toggleFavSelected);
  $('#res-reveal').addEventListener('click', async () => {
    const j = selectedJob();
    if (j?.gallery_id) api.post(`/api/gallery/${j.gallery_id}/reveal`).catch(toastError);
  });

  on('change', (path) => {
    if (path === 'model') onModelChange();
    refresh();
  });
  on('form-replaced', () => { fillSamplerOptions(); syncAll(); refresh(); });
  on('boot-updated', () => { fillModelOptions(); fillSamplerOptions(); syncAll(); refresh(); });
  on('gallery-changed', ({ id, favorite, deleted }) => {
    if (session.records[id]) {
      if (deleted) delete session.records[id];
      else session.records[id].favorite = favorite;
    }
    if (deleted) session.jobs.filter(j => j.gallery_id === id).forEach(j => removeJob(j.job_id, false));
    renderPreview();
    renderStrip();
  });

  syncAll();
  refresh();
  restoreJobs();
}

function segmented(root, get, set) {
  const render = () => $$('button', root).forEach(b => b.classList.toggle('on', b.dataset.v === get()));
  root.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    set(b.dataset.v);
    render();
  });
  on('change', render);
  on('form-replaced', render);
  render();
}

function fillModelOptions() {
  const caps = app.boot.capabilities;
  fill($('#model'), ...caps.models.map(m =>
    el('option', { value: m.id, disabled: m.installed === false }, m.label + (m.installed === false ? '（未安装）' : ''))));
  $('#model').value = app.form.model;
}

function fillSamplerOptions() {
  const caps = app.boot.capabilities;
  const m = modelInfo();
  const mk = (sel, list, def, cur) => {
    const opts = [el('option', { value: '' }, `模型默认（${def || '—'}）`), ...list.map(s => el('option', { value: s }, s))];
    if (cur && !list.includes(cur)) opts.push(el('option', { value: cur }, cur));
    fill(sel, ...opts);
    sel.value = cur || '';
  };
  mk($('#sampler'), caps.samplers || [], m?.defaults.sampler_name, app.form.sampler);
  mk($('#scheduler'), caps.schedulers || [], m?.defaults.scheduler, app.form.scheduler);
}

function onModelChange() {
  const m = modelInfo();
  if (!m) return;
  app.form.steps = m.defaults.steps;
  app.form.cfg = m.defaults.cfg;
  app.form.sampler = '';
  app.form.scheduler = '';
  const off = [];
  if (!m.features.character_ref && app.form.character_ref.enabled) { app.form.character_ref.enabled = false; off.push('人物参考'); }
  if (!m.features.pose_ref && app.form.pose_ref.enabled) { app.form.pose_ref.enabled = false; off.push('动作参考'); }
  if (off.length) toast(`${m.label} 不支持${off.join('、')}，已自动关闭`, 'warn');
  saveForm();
  fillSamplerOptions();
  syncAll();
}

// ───────────────────────────── 派生显示 ─────────────────────────────
function rangeHint(input, hint, range, fmt) {
  const v = input.value === '' ? null : Number(input.value);
  if (!range) { hint.textContent = ''; input.classList.remove('out'); return; }
  const [a, b] = range;
  hint.textContent = a === b ? `推荐 ${fmt(a)}` : `推荐 ${fmt(a)}–${fmt(b)}`;
  const out = v != null && (v < a || v > b);
  hint.classList.toggle('out', out);
  input.classList.toggle('out', out);
  if (out) hint.textContent += '（当前超出推荐范围）';
}

export function refresh() {
  const f = app.form;
  const m = modelInfo();
  const ui = app.boot.models_ui[f.model] || {};

  // 模型说明
  const notes = m?.notes || {};
  const label = { character_ref: '人物参考', pose_ref: '动作参考' };
  fill($('#model-note'), 
    ...Object.entries(notes).map(([k, v]) => el('p', {}, el('span', { class: 'tag' }, label[k] || k), v)),
    ui.source ? el('p', { class: 'muted' }, `推荐范围来源：${ui.source}`) : null);

  rangeHint($('#steps'), $('#steps-hint'), ui.steps, (x) => x);
  rangeHint($('#cfg'), $('#cfg-hint'), ui.cfg, (x) => x);

  // cfg = 1 → neg 不生效
  const cfgOne = Number(f.cfg) === 1;
  $('#neg-field').classList.toggle('off', cfgOne);
  $('#neg-field textarea').disabled = cfgOne;
  $('#neg-note').hidden = !cfgOne;

  // 画幅
  const custom = f.aspect_ratio === 'custom' || !app.boot.defaults.aspect_presets[f.aspect_ratio];
  $('#custom-size').hidden = !custom;
  const [w, h] = custom ? [f.width, f.height] : app.boot.defaults.aspect_presets[f.aspect_ratio];
  const mp = (w * h / 1048576).toFixed(2);
  $('#size-chip').textContent = `${w} × ${h} · ${mp} MP`;

  // 画师
  const toks = (f.fields.artist || '').split(',').map(s => s.trim()).filter(Boolean);
  const bad = toks.filter(t => !t.startsWith('@'));
  const aw = $('#artist-warn');
  fill(aw);
  if (bad.length) {
    aw.append(`缺少 @：${bad.join(', ')}`, el('button', {
      onclick: () => { setField('fields.artist', toks.map(t => t.startsWith('@') ? t : '@' + t).join(', ')); syncAll(); },
    }, '补上 @'));
  } else if (toks.length > 1) {
    aw.append(el('span', { class: 'muted' }, '多画师混合时稳定性会下降'));
  }
  aw.hidden = !aw.childNodes.length;
  // 画师库里能对上、但不是标准标签的写法（@k子）：提示替换成标准标签
  const ah = $('#artist-hint'), key = toks.join('|');
  if (ah.dataset.for !== key) {
    ah.dataset.for = key;
    if (!toks.length) renderArtistHints(ah, null, []);
    else artistHints(toks).then(h => { if (ah.dataset.for === key) renderArtistHints(ah, $('input[data-field="artist"]'), h); });
  }

  // 参考
  const feat = m?.features || {};
  for (const [key, row, why] of [['character_ref', '#cr-toggle-row', '#cr-why'], ['pose_ref', '#pr-toggle-row', '#pr-why']]) {
    const ok = !!feat[key];
    $(row).classList.toggle('disabled', !ok);
    $(row + ' input').disabled = !ok;
    $(why).textContent = ok ? '' : '（该模型不支持）';
    $(row).title = ok ? '' : (notes[key] || '该模型不支持');
  }
  const cr = f.character_ref.enabled, pr = f.pose_ref.enabled;
  $('#cr-zone').hidden = !cr;
  $('#pr-zone').hidden = !pr;
  $('#ref-zones').classList.toggle('two', cr && pr);
  $('#ref-adv').hidden = !cr && !pr;
  $('#cr-adv').hidden = !cr;
  $('#pr-adv').hidden = !pr;
  crZone.render();
  prZone.render();
  const hints = [];
  if (cr) hints.push('人物参考：提示词写 2~5 个核心特征即可，不要写与参考图冲突的特征；换装时在 neg 写原服装的标志部件。');
  if (pr) hints.push('动作参考：要写与骨架一致的动作词和朝向（如 facing viewer），构图与骨架范围一致（full body / upper body）。');
  if (cr && pr) hints.push('两者同开：人物参考图尽量用中性站姿，极端动作可能被参考图的姿势拉回。');
  fill($('#ref-hints'), ...hints.map(t => el('div', { class: 'hint' }, t)));

  // 种子
  $('#seed').disabled = f.seed_mode !== 'locked';
  $('#seed').placeholder = f.seed_mode === 'locked' ? '输入种子' : '随机';
  emit('refreshed');
}

// ───────────────────────────── 生成 ─────────────────────────────
async function submit() {
  const f = app.form;
  if (f.character_ref.enabled && !f.character_ref.images.length) return toast('已开启人物参考，请先上传人物参考图', 'warn');
  if (f.pose_ref.enabled && !f.pose_ref.image) return toast('已开启动作参考，请先上传动作参考图', 'warn');
  if (f.seed_mode === 'locked' && (f.seed == null || f.seed === '')) return toast('已锁定种子，请填写种子', 'warn');
  const btn = $('#generate');
  btn.disabled = true;
  try {
    const job = await api.post('/api/jobs', { form: structuredClone(f) });
    session.jobs.push(job);
    session.selected = job.job_id;
    renderPreview();
    renderStrip();
    startPolling();
    emit('jobs-changed');
  } catch (e) {
    toastError(e);
  } finally {
    btn.disabled = false;
  }
}

async function restoreJobs() {
  try {
    const jobs = (await api.get('/api/jobs')).filter(j => j.status !== 'cancelled');   // 已取消的不再显示
    if (!jobs.length) return;
    session.jobs = jobs;
    const running = jobs.filter(j => !TERMINAL.includes(j.status));
    session.selected = (running.at(-1) || jobs.at(-1)).job_id;
    const lastOk = [...jobs].reverse().find(j => j.status === 'succeeded');
    if (lastOk) app.lastSeed = lastOk.params?.seed ?? null;
    renderPreview();
    renderStrip();
    if (running.length) startPolling();
  } catch { /* 忽略 */ }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(poll, 1000);
}

// 已有最终结果：失败 / 取消 / 成功且已写入图库
const settled = (j) => j.status === 'failed' || j.status === 'cancelled' || (j.status === 'succeeded' && !!j.gallery_id);

let polling = false;
async function poll() {
  if (polling) return;   // 上一轮请求还没回来（服务卡顿）时不叠加
  const active = session.jobs.filter(j => !settled(j));
  if (!active.length) { clearInterval(pollTimer); pollTimer = null; return; }
  polling = true;
  try {
    for (const j of active) {
      try {
        Object.assign(j, await api.get(`/api/jobs/${j.job_id}`));
      } catch (e) {
        if (e.status !== 404) continue;   // 连不上：下次再试
        markFailed(j, '任务记录已丢失（前端服务重启过），请重新生成');
      }
      // 成功但迟迟没写入图库：按失败处理，不让卡片一直停在「没有图也没有提示」
      if (j.status === 'succeeded' && !j.gallery_id && (j._waits = (j._waits || 0) + 1) > 10) {
        markFailed(j, '图片已生成，但没有写入图库（详见 logs/frontend.log）');
      }
      if (settled(j) && session.jobs.includes(j)) onFinished(j);
    }
  } finally { polling = false; }
  renderPreview();
  renderStrip();
}

function markFailed(j, message) {
  Object.assign(j, { status: 'failed', error: { code: 'LOST', message } });
}

function onFinished(j) {
  if (j._notified) return;
  j._notified = true;
  if (j.status === 'succeeded' && j.gallery_id) {
    app.lastSeed = j.params?.seed ?? app.lastSeed;
    emit('gallery-new', j.gallery_id);
    if (session.selected !== j.job_id) toast('有一张新图片已完成', 'ok');
  } else if (j.status === 'failed') {
    toast(`生成失败：${errorText(j.error)}`, 'error', 7000);
  } else if (j.status === 'cancelled') {
    removeJob(j.job_id);   // 取消的任务直接从结果条移除，预览切到最近的另一张
  }
  emit('jobs-changed');
}

async function cancelSelected() {
  const j = selectedJob();
  if (!j) return;
  try {
    const r = await api.post(`/api/jobs/${j.job_id}/cancel`);
    if (r?.status === 'cancelled') {   // 排队中的任务立即取消；执行中的要等引擎中断，由轮询移除
      j.status = 'cancelled';
      onFinished(j);
    } else toast('已请求取消', 'info');
  } catch (e) { toastError(e); }
}

// ───────────────────────────── 预览 ─────────────────────────────
function selectedJob() {
  return session.jobs.find(j => j.job_id === session.selected) || null;
}

function currentSeed() {
  const j = selectedJob();
  return j?.status === 'succeeded' ? (j.params?.seed ?? app.lastSeed) : app.lastSeed;
}

async function getRecord(id) {
  if (!session.records[id]) session.records[id] = await api.get(`/api/gallery/${id}`);
  return session.records[id];
}

function renderPreview() {
  const j = selectedJob();
  const img = $('#stage-img');
  const prog = $('#stage-progress');
  const errBox = $('#stage-error');
  $('#stage-empty').hidden = !!j;
  errBox.hidden = true;
  if (!j) { img.hidden = true; prog.hidden = true; $('#result-bar').hidden = true; return; }

  const running = !TERMINAL.includes(j.status);
  prog.hidden = !running;
  if (running) {
    const p = j.progress || {};
    const pct = Math.round(p.percent || 0);
    $('#ring-pct').textContent = `${pct}%`;
    $('#ring-bar').style.strokeDashoffset = String(326.7 * (1 - pct / 100));
    $('#stage-stage').textContent = STAGE_TEXT[p.stage] || (j.status === 'queued' ? '排队中' : '处理中');
    $('#stage-sub').textContent = p.queue_position ? `前面还有 ${p.queue_position} 个任务`
      : (p.total_steps ? `第 ${p.step} / ${p.total_steps} 步` : '');
  }
  if (j.status === 'succeeded' && j.gallery_id) {
    const src = `/api/files/image/${j.gallery_id}`;
    if (img.getAttribute('src') !== src) img.src = src;
    img.hidden = false;
    $('#result-bar').hidden = false;
    const pr = j.params || {};
    $('#result-meta').textContent = `seed ${pr.seed}  ·  ${pr.width}×${pr.height}  ·  ${pr.model}  ·  ${pr.steps} 步  ·  cfg ${pr.cfg}  ·  ${pr.sampler_name}/${pr.scheduler}`;
    getRecord(j.gallery_id).then((r) => {
      $('#res-fav').textContent = r.favorite ? '★ 已收藏' : '☆ 收藏';
    }).catch(() => {});
  } else if (!running) {
    img.hidden = true;
    $('#result-bar').hidden = true;
    if (j.status === 'failed') { errBox.hidden = false; $('#stage-error-text').textContent = `生成失败：${errorText(j.error)}`; }
    else if (j.status === 'cancelled') { errBox.hidden = false; $('#stage-error-text').textContent = '任务已取消'; }
  } else {
    $('#result-bar').hidden = true;
  }
}

function renderStrip() {
  const strip = $('#strip');
  fill(strip, ...[...session.jobs].reverse().map((j) => {
    const running = !TERMINAL.includes(j.status);
    const cls = ['strip-item', j.job_id === session.selected ? 'sel' : '', running ? 'running' : '',
      j.status === 'failed' ? 'failed' : ''].join(' ');
    const item = el('div', {
      class: cls, title: j.status === 'failed' ? `生成失败：${errorText(j.error)}` : j.status,
      onclick: () => { session.selected = j.job_id; renderPreview(); renderStrip(); },
    });
    if (j.status === 'succeeded' && j.gallery_id) {
      const img = el('img', { src: `/api/files/thumb/${j.gallery_id}`, alt: '' });
      img.onerror = () => removeJob(j.job_id);
      item.append(img);
    } else if (running) item.textContent = `${Math.round(j.progress?.percent || 0)}%`;
    else if (j.status === 'failed' || j.status === 'cancelled') {
      item.append(j.status === 'failed' ? '失败' : '取消',
        el('button', { class: 'x', title: '关闭', onclick: (e) => { e.stopPropagation(); dismissJob(j); } }, '×'));
    } else item.textContent = '保存中';   // 已完成，正在写入图库
    return item;
  }));
}

function doneJobs() {
  return session.jobs.filter(j => j.status === 'succeeded' && j.gallery_id);
}

// 从本次结果中移除一个任务；若正在显示它，改为显示最近的另一张（没有就回到空白）
function removeJob(jobId, redraw = true) {
  const idx = session.jobs.findIndex(j => j.job_id === jobId);
  if (idx < 0) return;
  session.jobs.splice(idx, 1);
  if (session.selected === jobId) {
    const next = [...session.jobs].reverse().find(j => j.status === 'succeeded' || !TERMINAL.includes(j.status));
    session.selected = next ? next.job_id : null;
  }
  if (redraw) { renderPreview(); renderStrip(); }
}

// 关掉失败 / 取消的任务：服务端也一并忘掉，刷新页面后不再出现
function dismissJob(j) {
  removeJob(j.job_id);
  api.post(`/api/jobs/${j.job_id}/dismiss`).catch(() => {});
}

async function deleteSelected() {
  const j = selectedJob();
  if (!j?.gallery_id) return;
  const rec = await getRecord(j.gallery_id).catch(() => null);
  const ok = await confirmDialog('永久删除这张图片？', `原文件${rec ? ` ${rec.path.split(/[\\/]/).pop()}` : ''} 将从磁盘删除，无法恢复。`, '永久删除', true);
  if (!ok) return;
  try {
    await api.post(`/api/gallery/${j.gallery_id}/delete`, { keep: refHashesInUse() });
    emit('gallery-changed', { id: j.gallery_id, deleted: true });
    toast('已删除', 'ok', 1500);
  } catch (e) { toastError(e); }
}

async function toggleFavSelected() {
  const j = selectedJob();
  if (!j?.gallery_id) return;
  try {
    const r = await api.post(`/api/gallery/${j.gallery_id}/favorite`);
    if (session.records[j.gallery_id]) session.records[j.gallery_id].favorite = r.favorite;
    $('#res-fav').textContent = r.favorite ? '★ 已收藏' : '☆ 收藏';
    emit('gallery-changed', { id: j.gallery_id, favorite: r.favorite });
  } catch (e) { toastError(e); }
}

export function activeJobCount() {
  return session.jobs.filter(j => !TERMINAL.includes(j.status)).length;
}

export { fmtDuration };
