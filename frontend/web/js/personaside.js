// 人设详情左栏：封面与预览图、生成预览（只在人设页管理，不进历史 / 收藏）。
import { api, errorText, uploadRef } from './api.js';
import { $, el, fill, toast, toastError, confirmDialog, modal, lightbox, fmtTime, dragScroll } from './ui.js';
import { app, modelInfo, refHashesInUse, FIELD_NAMES } from './store.js';
import { reuseDialog } from './records.js';
import { joinTags, coverUrl, atArtist } from './persona.js';

const TERMINAL = ['succeeded', 'failed', 'cancelled'];
const STAGE_TEXT = { queued: '排队中', preparing: '准备中', base: '绘制中', refine: '精细重绘', upscale: '超分', saving: '保存中', done: '完成' };
const SAFETY = ['safe', 'sensitive', 'nsfw', 'explicit'];
const PREF_KEY = 'anima.persona.preview';
const jobs = [];          // 本次打开页面以来提交的人设预览任务（所有人设）
let pollTimer = null;

function loadPrefs() {
  let p = {};
  try { p = JSON.parse(localStorage.getItem(PREF_KEY) || '{}') || {}; } catch { /* 忽略 */ }
  return p;
}
function savePrefs(p) { try { localStorage.setItem(PREF_KEY, JSON.stringify(p)); } catch { /* 忽略 */ } }

export function createSide(ctx) {
  const ss = { root: null, pid: null, images: [], sel: null, outfit: undefined, artist: null, manual: false, text: '',
    prefs: loadPrefs(), dom: {} };

  // ───────── 预览参数与提示词 ─────────
  const pp = () => app.boot.defaults.persona_preview || {};
  const model = () => (ss.prefs.model && modelInfo(ss.prefs.model) ? ss.prefs.model : app.form.model);
  const aspect = () => (app.boot.defaults.aspect_presets[ss.prefs.aspect] ? ss.prefs.aspect : '2:3');
  const safety = () => (ss.prefs.safety ?? pp().safety ?? 'safe');
  const outfit = () => {
    const os = ctx.p.outfits;
    if (ss.outfit === '') return null;
    return os.find(o => o.id === ss.outfit) || os.find(o => o.default) || null;
  };
  const artist = () => atArtist(ss.artist ?? ctx.p.artist ?? '');

  // 预览构图（solo, cowboy shot, …）紧跟在主体后面：1girl, solo, cowboy shot, …, 角色, 作品, 画师, 外观, 造型
  function fields() {
    const p = ctx.p, o = outfit();
    return {
      quality_meta_year_safe: joinTags(pp().quality, safety()), count: joinTags(p.count, pp().composition),
      character: o?.character_tag || p.character_tag, series: p.series, artist: artist(),
      appearance: joinTags(p.base_tags, o?.tags), action: '', nltags: '',
      neg: app.boot.defaults.fields.neg,
    };
  }
  const autoText = () => { const f = fields(); return joinTags(f.count, f.character, f.series, f.artist, f.appearance, f.action); };

  // ───────── 挂载 ─────────
  function mount(root) {
    ss.root = root;
    ss.sig = ss.stripSig = null;
    ss.pid = ctx.p.id;
    ss.images = [];
    ss.sel = null;
    ss.outfit = undefined;
    ss.artist = null;
    ss.manual = false;
    const d = ss.dom = {};
    d.viewer = el('div', { class: 'pv-viewer', tabindex: 0, title: '点击查看大图；可把图片拖到这里或粘贴设为封面' });
    d.strip = el('div', { class: 'pv-strip' });
    dragScroll(d.strip);
    d.actions = el('div', { class: 'pv-actions' });
    d.meta = el('div', { class: 'pv-meta' });
    d.job = el('div', { class: 'pv-job' });
    d.outfit = el('select', { onchange: (e) => { ss.outfit = e.target.value; syncText(); } });
    d.model = el('select', { onchange: (e) => { ss.prefs.model = e.target.value; savePrefs(ss.prefs); } });
    d.aspect = el('select', { onchange: (e) => { ss.prefs.aspect = e.target.value; savePrefs(ss.prefs); } });
    d.safety = el('select', { onchange: (e) => { ss.prefs.safety = e.target.value; savePrefs(ss.prefs); syncText(); } },
      el('option', { value: '' }, '不写'), SAFETY.map(s => el('option', { value: s }, s)));
    d.artist = el('input', { type: 'text', placeholder: '@画师（默认用人设的画师偏好）' });
    d.artist.addEventListener('input', () => { ss.artist = d.artist.value; syncText(); });
    d.text = el('textarea', { rows: 6, class: 'mono' });
    d.text.addEventListener('input', () => { ss.manual = true; ss.text = d.text.value; renderTextNote(); });
    d.note = el('div', { class: 'pv-note muted small' });
    d.gen = el('button', { class: 'btn accent big', onclick: generate }, '按当前提示词生成预览');
    d.file = el('input', { type: 'file', accept: 'image/*', hidden: true, onchange: () => { if (d.file.files[0]) uploadCover(d.file.files[0]); d.file.value = ''; } });

    d.viewer.addEventListener('click', openLightbox);
    d.viewer.addEventListener('dragover', (e) => { e.preventDefault(); d.viewer.classList.add('drag'); });
    d.viewer.addEventListener('dragleave', () => d.viewer.classList.remove('drag'));
    d.viewer.addEventListener('drop', (e) => {
      e.preventDefault();
      d.viewer.classList.remove('drag');
      const f = [...e.dataTransfer.files].find(x => x.type.startsWith('image/'));
      if (f) uploadCover(f);
    });
    d.viewer.addEventListener('paste', (e) => {
      const f = [...(e.clipboardData?.items || [])].find(i => i.kind === 'file')?.getAsFile();
      if (f) { e.preventDefault(); uploadCover(f); }
    });

    const mini = (label, node) => el('label', { class: 'pv-field' }, el('span', {}, label), node);
    fill(root,
      d.viewer, d.strip, d.actions, d.meta,
      el('div', { class: 'pv-gen' },
        el('div', { class: 'pv-gen-head' }, el('h3', {}, '生成预览'), el('span', { class: 'grow' }),
          el('button', { class: 'btn ghost small', onclick: pickFromHistory }, '从历史选图'),
          el('button', { class: 'btn ghost small', onclick: async () => { if (await ensureSaved()) d.file.click(); } }, '上传封面')),
        el('div', { class: 'grid2' }, mini('造型', d.outfit), mini('模型', d.model)),
        el('div', { class: 'grid2' }, mini('比例', d.aspect), mini('分级', d.safety)),
        mini('画师', d.artist),
        d.text, d.note, d.job, d.gen, d.file));
    fillSelects();
    update();
    loadImages();
    restoreJobs();
  }

  function unmount() {
    ss.root = null;
    ss.pid = null;
  }

  function fillSelects() {
    const d = ss.dom;
    fill(d.model, app.boot.capabilities.models.map(m => el('option', { value: m.id, disabled: m.installed === false },
      m.label + (m.installed === false ? '（未安装）' : ''))));
    d.model.value = model();
    fill(d.aspect, Object.entries(app.boot.defaults.aspect_presets).map(([k, [w, h]]) => el('option', { value: k }, `${k}  ${w}×${h}`)));
    d.aspect.value = aspect();
    d.safety.value = safety();
  }

  // 右栏有修改时调用：同步造型选项、画师占位、自动提示词
  function update() {
    if (!ss.root) return;
    if (ss.pid !== ctx.p.id) ss.pid = ctx.p.id;   // 新建的人设刚保存
    const d = ss.dom;
    const os = ctx.p.outfits;
    if (ss.outfit && ss.outfit !== '' && !os.some(o => o.id === ss.outfit)) ss.outfit = undefined;
    fill(d.outfit, os.map(o => el('option', { value: o.id }, (o.name || '未命名') + (o.default ? '（默认）' : ''))),
      el('option', { value: '' }, '仅基础外观'));
    d.outfit.value = ss.outfit === '' ? '' : (outfit()?.id ?? '');
    if (ss.artist === null) d.artist.value = ctx.p.artist || '';
    if (document.activeElement !== d.model) d.model.value = model();
    syncText();
    renderImages();
  }

  function syncText() {
    if (!ss.root) return;
    if (!ss.manual) {
      ss.text = autoText();
      ss.dom.text.value = ss.text;
    }
    renderTextNote();
  }

  function renderTextNote() {
    const n = ss.dom.note;
    fill(n, ss.manual
      ? ['已手动修改（改人设字段不再同步） ', el('button', { class: 'linkbtn', onclick: () => { ss.manual = false; syncText(); } }, '恢复自动')]
      : `按人设字段自动组合（改字段会同步）；前面自动加质量词${safety() ? '与分级' : ''}，负向提示词用默认值`);
  }

  // ───────── 图片 ─────────
  async function loadImages(selectId) {
    const pid = ctx.p.id;
    if (!pid) { ss.images = []; renderImages(); return; }
    try {
      const list = await api.get(`/api/personas/${pid}/images`);
      if (pid !== ctx.p?.id) return;
      ss.images = list;
    } catch (e) { toastError(e); }
    if (selectId != null) ss.sel = `g${selectId}`;
    renderImages();
  }

  function items() {
    const p = ctx.p;
    const out = [];
    // 封面来源图的文件还在时，封面就是那张图；文件丢失了则单独显示封面副本（cover.png 不受影响）
    const coverInList = p.cover_gid != null && ss.images.some(r => r.id === p.cover_gid && r.exists);
    if (p.cover && !coverInList) out.push({ key: 'cover', cover: true, src: coverUrl(p, false), thumb: coverUrl(p) });
    for (const r of ss.images) {
      out.push({ key: `g${r.id}`, rec: r, cover: coverInList && r.id === p.cover_gid,
        src: `/api/files/image/${r.id}`, thumb: `/api/files/thumb/${r.id}`, lost: !r.exists });
    }
    return out;
  }

  function current(list = items()) {
    let it = list.find(x => x.key === ss.sel);
    if (!it) it = list.find(x => x.cover) || list[0] || null;
    ss.sel = it?.key ?? null;
    return it;
  }

  function renderImages() {
    if (!ss.root) return;
    const d = ss.dom;
    const list = items();
    const it = current(list);
    renderJob();
    // 右栏每次输入、任务每秒进度都会调用到这里；内容没变时不重建图片，避免闪烁
    const stripSig = JSON.stringify([list.map(x => x.key), ss.sel, running().map(j => [j.job_id, Math.round(j.progress?.percent || 0)])]);
    if (stripSig !== ss.stripSig) {
      ss.stripSig = stripSig;
      fill(d.strip, list.length > 1 || running().length ? [
        ...running().map(j => el('div', { class: 'strip-item running', title: '生成中' }, `${Math.round(j.progress?.percent || 0)}%`)),
        ...list.map(x => el('div', { class: `strip-item${x.key === ss.sel ? ' sel' : ''}`, onclick: () => { ss.sel = x.key; renderImages(); } },
          x.lost ? '丢失' : el('img', { src: x.thumb, alt: '' })))] : []);
    }
    const sig = JSON.stringify([list.map(x => x.key + x.cover), ss.sel, ctx.p.cover, ctx.p.id, ctx.p.outfits.map(o => o.id + o.name)]);
    if (sig === ss.sig) return;
    ss.sig = sig;
    fill(d.viewer, it
      ? (it.lost ? el('div', { class: 'pv-empty' }, '文件已丢失') : el('img', { src: it.src, alt: '' }))
      : el('div', { class: 'pv-empty' }, el('div', { class: 'stage-icon' }, '✦'),
        ctx.p.id ? '还没有图片：在下面生成预览，或拖入 / 粘贴一张图作为封面' : '保存后可以生成预览、设置封面'),
      list.length > 1 ? [
        el('button', { class: 'lb-nav lb-prev', onclick: (e) => { e.stopPropagation(); step(-1); } }, '‹'),
        el('button', { class: 'lb-nav lb-next', onclick: (e) => { e.stopPropagation(); step(1); } }, '›')] : null,
      it?.cover ? el('span', { class: 'pv-badge' }, '★ 封面') : null);
    // 操作
    const btn = (label, fn, cls = '') => el('button', { class: `btn ghost small ${cls}`, onclick: fn }, label);
    if (!it) fill(d.actions);
    else if (!it.rec) {
      fill(d.actions, el('span', { class: 'pv-cur' }, '★ 当前封面'), el('span', { class: 'grow' }),
        btn('移除封面', removeCover, 'danger'));
    } else {
      fill(d.actions,
        it.cover ? el('span', { class: 'pv-cur' }, '★ 当前封面') : btn('☆ 设为封面', () => setCover(it.rec.id)),
        btn('↗ 复用到绘图页', () => reuseDialog(it.rec)),
        btn('↓ 用它的提示词', () => usePrompt(it.rec)),
        el('span', { class: 'grow' }),
        btn('删除这张', () => removeImage(it.rec), 'danger'));
    }
    const r = it?.rec;
    const o = r && ctx.p.outfits.find(x => x.id === r.form?.persona?.outfit_id);
    d.meta.textContent = r ? [fmtTime(r.created_at), r.model, `seed ${r.seed}`, o ? `造型 ${o.name}` : null, '预览生成']
      .filter(Boolean).join('  ·  ') : (it ? '封面（上传或来自历史图片）' : '');
  }

  function step(dlt) {
    const list = items();
    const i = list.findIndex(x => x.key === ss.sel);
    ss.sel = list[(i + dlt + list.length) % list.length].key;
    renderImages();
  }

  function openLightbox() {
    const list = items().filter(x => !x.lost);
    if (!list.length) return;
    const i = Math.max(0, list.findIndex(x => x.key === ss.sel));
    lightbox(list.map(x => ({ src: x.src, caption: x.rec ? `${fmtTime(x.rec.created_at)}  ·  seed ${x.rec.seed}` : '封面' })), i);
  }

  async function ensureSaved() {
    if (ctx.p.id && !ctx.dirty()) return true;
    if (!ctx.p.id) toast('先保存人设', 'info', 1500);
    return ctx.save();
  }

  async function setCover(gid) {
    try {
      const p = await api.post(`/api/personas/${ctx.p.id}/cover`, { gallery_id: gid });
      ctx.coverChanged(p);
      toast('已设为封面', 'ok', 1500);
      renderImages();
    } catch (e) { toastError(e); }
  }

  async function uploadCover(file) {
    if (!await ensureSaved()) return;
    try {
      const h = await uploadRef(file);
      const p = await api.post(`/api/personas/${ctx.p.id}/cover`, { ref: h });
      ctx.coverChanged(p);
      ss.sel = 'cover';
      toast('已设为封面', 'ok', 1500);
      renderImages();
    } catch (e) { toastError(e); }
  }

  async function removeCover() {
    if (!await confirmDialog('移除封面？', '只移除封面，不影响预览图。', '移除')) return;
    try { ctx.coverChanged(await api.del(`/api/personas/${ctx.p.id}/cover`)); renderImages(); } catch (e) { toastError(e); }
  }

  async function removeImage(rec) {
    const ok = await confirmDialog('永久删除这张预览图？', `原文件 ${rec.filename} 将从磁盘删除，无法恢复。${rec.id === ctx.p.cover_gid ? '封面是单独保存的副本，不受影响。' : ''}`, '永久删除', true);
    if (!ok) return;
    try {
      await api.post(`/api/gallery/${rec.id}/delete`, { keep: refHashesInUse() });
      ctx.p.image_count = Math.max(0, (ctx.p.image_count || 1) - 1);
      ss.images = ss.images.filter(r => r.id !== rec.id);
      renderImages();
      toast('已删除', 'ok', 1500);
    } catch (e) { toastError(e); }
  }

  function usePrompt(rec) {
    const pr = rec.form?.persona || {};
    if (pr.outfit_id === '' || ctx.p.outfits.some(o => o.id === pr.outfit_id)) ss.outfit = pr.outfit_id;
    if (rec.form?.model && modelInfo(rec.form.model)) ss.prefs.model = rec.form.model;
    if (app.boot.defaults.aspect_presets[rec.form?.aspect_ratio]) ss.prefs.aspect = rec.form.aspect_ratio;
    if (pr.safety !== undefined) ss.prefs.safety = pr.safety;
    savePrefs(ss.prefs);
    ss.artist = pr.artist ?? null;
    fillSelects();
    update();
    ss.dom.artist.value = artist();
    if (pr.prompt_text && pr.prompt_text !== autoText()) {
      ss.manual = true;
      ss.text = pr.prompt_text;
      ss.dom.text.value = ss.text;
    } else { ss.manual = false; }
    syncText();
    toast('已载入这张图的提示词', 'ok', 1500);
  }

  async function pickFromHistory() {
    if (!await ensureSaved()) return;
    const grid = el('div', { class: 'hist-grid' });
    const more = el('button', { class: 'btn ghost small' }, '加载更多');
    let offset = 0;
    const load = async () => {
      try {
        const d = await api.get(`/api/gallery?offset=${offset}&limit=40`);
        offset += d.items.length;
        grid.append(...d.items.filter(x => x.exists).map(x => el('img', {
          src: `/api/files/thumb/${x.id}`, alt: '', loading: 'lazy', title: fmtTime(x.created_at),
          onclick: async () => { m.close(); await setCover(x.id); ss.sel = 'cover'; renderImages(); },
        })));
        more.hidden = offset >= d.total;
        if (!d.total) grid.append(el('div', { class: 'muted' }, '历史里还没有图片'));
      } catch (e) { toastError(e); }
    };
    more.addEventListener('click', load);
    const m = modal({ title: '从历史选一张作为封面', wide: true, body: el('div', {}, grid, el('div', { class: 'center' }, more)),
      actions: [{ label: '取消', kind: 'ghost' }] });
    load();
  }

  // ───────── 生成 ─────────
  async function generate() {
    if (!await ensureSaved()) return;
    const p = ctx.p;
    const text = ss.dom.text.value.trim();
    if (!text) return toast('提示词为空', 'warn');
    const m = modelInfo(model());
    const ar = aspect();
    const [w, h] = app.boot.defaults.aspect_presets[ar];
    const f = fields();
    const flds = Object.fromEntries(FIELD_NAMES.map(k => [k, '']));
    Object.assign(flds, ss.manual ? { quality_meta_year_safe: f.quality_meta_year_safe, appearance: text, neg: f.neg } : f);
    const o = outfit();
    const form = {
      model: model(), steps: m?.defaults.steps ?? null, cfg: m?.defaults.cfg ?? null, fields: flds,
      aspect_ratio: ar, width: w, height: h, seed_mode: 'random', seed: null, sampler: '', scheduler: '',
      character_ref: { enabled: false, images: [] }, pose_ref: { enabled: false, image: null },
      llm: { description: '', images: [] },
      persona: { id: p.id, outfit_id: o ? o.id : '', prompt_text: text, manual: ss.manual, safety: safety(), artist: artist() },
    };
    ss.dom.gen.disabled = true;
    try {
      const job = await api.post('/api/jobs', { form });
      jobs.push({ ...job, persona_id: p.id, persona_name: p.name });
      startPolling();
      renderImages();
    } catch (e) { toastError(e); }
    finally { ss.dom.gen.disabled = false; }
  }

  const running = () => jobs.filter(j => j.persona_id === ctx.p?.id && !TERMINAL.includes(j.status));

  function renderJob() {
    const d = ss.dom.job;
    const rs = running();
    if (!rs.length) { fill(d); d.hidden = true; return; }
    d.hidden = false;
    fill(d, rs.map(j => {
      const pr = j.progress || {};
      const pct = Math.round(pr.percent || 0);
      return el('div', { class: 'pv-jobrow' },
        el('div', { class: 'meter' }, el('i', { style: { width: `${pct}%` } })),
        el('span', { class: 'small' }, `${STAGE_TEXT[pr.stage] || (j.status === 'queued' ? '排队中' : '处理中')} ${pct}%`,
          pr.queue_position ? ` · 前面还有 ${pr.queue_position} 个` : pr.total_steps ? ` · ${pr.step}/${pr.total_steps} 步` : ''),
        el('button', { class: 'btn ghost small', onclick: () => api.post(`/api/jobs/${j.job_id}/cancel`).then(() => toast('已请求取消', 'info')).catch(toastError) }, '取消'));
    }));
  }

  async function restoreJobs() {
    const pid = ctx.p.id;
    if (!pid) return;
    try {
      for (const j of await api.get(`/api/jobs?persona_id=${pid}`)) {
        if (!jobs.some(x => x.job_id === j.job_id) && !TERMINAL.includes(j.status)) jobs.push({ ...j, persona_name: ctx.p.name });
      }
      if (running().length) { startPolling(); renderImages(); }
    } catch { /* 忽略 */ }
  }

  function startPolling() {
    if (!pollTimer) pollTimer = setInterval(poll, 1000);
  }

  async function poll() {
    const active = jobs.filter(j => !TERMINAL.includes(j.status) || (j.status === 'succeeded' && !j.gallery_id && (j._waits || 0) < 10));
    if (!active.length) { clearInterval(pollTimer); pollTimer = null; return; }
    for (const j of active) {
      let v;
      try {
        v = await api.get(`/api/jobs/${j.job_id}`);
      } catch (e) {
        if (e.status !== 404) continue;   // 连不上：下次再试
        v = { status: 'failed', error: { message: '任务记录已丢失（前端服务重启过），请重新生成' } };
      }
      const was = j.status;
      Object.assign(j, v);
      if (v.status === 'succeeded' && !v.gallery_id && (j._waits = (j._waits || 0) + 1) >= 10) {
        Object.assign(j, { status: 'failed', error: { message: '图片已生成，但没有写入图库（详见 logs/frontend.log）' } });
      }
      const done = j.status === 'succeeded' && j.gallery_id;
      if (done && !j._done) {
        j._done = true;
        if (ctx.p?.id === j.persona_id && ss.root) {
          ctx.p.image_count = (ctx.p.image_count || 0) + 1;
          loadImages(v.gallery_id);
        } else toast(`人设「${j.persona_name}」的预览已完成`, 'ok');
      } else if (was !== 'failed' && j.status === 'failed') {
        toast(`人设「${j.persona_name}」预览生成失败：${errorText(j.error)}`, 'error', 7000);
      }
    }
    if (ss.root) renderImages();
  }

  return { mount, unmount, update, outfitId: () => (ss.root ? (ss.outfit === '' ? '' : outfit()?.id) : undefined) };
}
