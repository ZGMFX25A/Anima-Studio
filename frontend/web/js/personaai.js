// 人设的 AI 功能：Danbooru 检索导入（AI 拆分成 基础外观 / 造型）、AI 整理（按补充描述补全 / 修正）、撤销。
import { api, errorText } from './api.js';
import { el, fill, toast, toastError, modal, dropzone, fmtTime } from './ui.js';
import { app, emit } from './store.js';

const LLM_KEY = 'anima.persona.llm';
const ai = { models: null, key: '', desc: '', images: [], tags: false, undo: null, busy: null, status: '' };

// 右栏会整体重绘，状态文字记在这里，再写到当前页面上的元素
function setStatus(t) {
  ai.status = t;
  const n = document.querySelector('#pd-right .ai-status');
  if (n) n.textContent = t;
}

// 后端出错会自动重试（最多 2 次，共 3 次请求）；中止时同时通知后端，在下一次重试前停下，不再消耗额度
function cancellable() {
  const ctl = new AbortController();
  ctl.rid = Math.random().toString(36).slice(2) + Date.now().toString(36);
  const abort = ctl.abort.bind(ctl);
  ctl.abort = () => { abort(); api.post('/api/llm/cancel', { request_id: ctl.rid }).catch(() => {}); };
  return ctl;
}

export function resetAi() {
  ai.desc = '';
  ai.images = [];
  ai.undo = null;
  ai.status = '';
  ai.busy?.abort();
  ai.busy = null;
}

async function loadModels() {
  if (ai.models) return ai.models;
  try { ai.models = await api.get('/api/llm/enabled'); } catch { ai.models = []; }
  let saved = '';
  try { saved = localStorage.getItem(LLM_KEY) || ''; } catch { /* 忽略 */ }
  const pick = [saved, app.form.llm.model_key].find(k => ai.models.some(m => m.key === k));
  ai.key = pick || ai.models[0]?.key || '';
  return ai.models;
}
const model = () => ai.models?.find(m => m.key === ai.key) || null;

function llmSelect() {
  const sel = el('select', { onchange: (e) => { ai.key = e.target.value; try { localStorage.setItem(LLM_KEY, ai.key); } catch { /* 忽略 */ } } });
  const render = () => {
    const ms = ai.models || [];
    if (!ms.length) { fill(sel, el('option', { value: '' }, '未配置（菜单 → LLM）')); sel.disabled = true; return; }
    sel.disabled = false;
    fill(sel, ms.map(m => el('option', { value: m.key, selected: m.key === ai.key }, `${m.provider} · ${m.label}`)));
  };
  render();
  if (!ai.models) loadModels().then(render);
  return sel;
}

const llmBody = () => { const m = model(); return m ? { provider_id: m.provider_id, model_id: m.model_id } : {}; };

// ───────── 撤销 ─────────
const FIELDS = ['name', 'aliases', 'kind', 'character_tag', 'series', 'count', 'base_tags', 'appearance_desc', 'outfits', 'source_tag'];
function snapshot(ctx, label) {
  ai.undo = { label, data: structuredClone(Object.fromEntries(FIELDS.map(k => [k, ctx.p[k]]))) };
}
function undo(ctx) {
  if (!ai.undo) return;
  Object.assign(ctx.p, ai.undo.data);
  toast(`已撤销${ai.undo.label}`, 'ok', 1500);
  ai.undo = null;
  ctx.setUnassigned([]);
  ctx.rerender();
}

// ═══════════════════════════ 右栏：AI 助手 ═══════════════════════════
export function aiSection(ctx) {
  const desc = el('textarea', { rows: 3, placeholder: '如：黑色超长发、一侧扎起，蓝色眼睛，头顶方形光环；再加一套女仆装' }, ai.desc);
  desc.addEventListener('input', () => { ai.desc = desc.value; });
  const imgs = el('div', { class: 'dz-wrap' });
  const zone = dropzone(imgs, {
    max: 3, label: '参考图', get: () => ai.images, set: (v) => { ai.images = v; },
    isDisabled: () => model()?.vision !== true,
  });
  const sel = llmSelect();
  sel.addEventListener('change', () => zone.render());
  loadModels().then(() => zone.render());
  const tags = el('label', { class: 'switch-row', title: '用 DanbooruSearch 按补充描述检索标准标签作为参考（消耗额度，结果会缓存）' },
    el('input', { type: 'checkbox', checked: ai.tags, onchange: (e) => { ai.tags = e.target.checked; } }),
    el('span', { class: 'switch' }), el('span', {}, '标签参考'));
  const status = el('span', { class: 'muted small ai-status' }, ai.status);
  const run = el('button', { class: `btn accent${ai.busy ? ' busy' : ''}`, disabled: !!ai.busy, title: '按补充描述（和参考图）补全、修正基础外观与造型；结果可撤销，需要点「保存」才会保存' }, '✦ AI 整理');
  run.addEventListener('click', () => refine(ctx, run));
  return el('section', { class: 'psec' },
    el('div', { class: 'psec-title' }, el('h3', {}, 'AI 助手'),
      el('span', { class: 'muted small' }, ctx.p.kind === 'existing'
        ? '已有角色建议先用名称旁的「🔍 Danbooru」导入；这里按补充描述继续调整'
        : '用中文写外貌与服装，AI 转写成标签并整理进基础外观与造型')),
    el('div', { class: 'field' },
      el('label', { class: 'fl' }, el('b', {}, '补充描述'), el('i', {}, '可附参考图（多模态模型才会发送）')),
      el('div', { class: 'ai-desc' }, imgs, desc)),
    el('div', { class: 'ai-bar' },
      el('label', { class: 'pv-field grow' }, el('span', {}, 'LLM'), sel), tags,
      ai.undo ? el('button', { class: 'btn ghost', onclick: () => undo(ctx) }, `撤销${ai.undo.label}`) : null,
      run),
    status);
}

async function refine(ctx, btn) {
  await loadModels();
  if (!model()) return toast('请先在 菜单 → LLM 中勾选模型', 'warn');
  const p = ctx.p;
  if (!ai.desc.trim() && !ai.images.length && !p.base_tags.trim() && !p.appearance_desc.trim()) {
    return toast('请先写补充描述', 'warn');
  }
  const ctl = ai.busy = cancellable();
  btn.classList.add('busy');
  btn.disabled = true;
  const t0 = Date.now();
  const tick = setInterval(() => setStatus(`AI 整理中… ${Math.round((Date.now() - t0) / 1000)}s`), 500);
  try {
    const res = await api.post('/api/personas/refine', {
      request_id: ctl.rid,
      ...llmBody(), persona: structuredClone(p), description: ai.desc,
      images: model()?.vision === true ? ai.images : [], tag_reference: ai.tags,
    }, { signal: ctl.signal });
    snapshot(ctx, ' AI 整理');
    const r = res.result;
    Object.assign(p, { base_tags: r.base_tags, appearance_desc: r.appearance_desc, outfits: r.outfits });
    if (r.count) p.count = r.count;
    ctx.setUnassigned(res.unassigned);
    ctx.rerender();
    for (const w of res.warnings || []) toast(w, 'warn', 6000);
    emit('llm-models-changed');     // 思考档位可能被自动改掉
    clearInterval(tick);
    setStatus(`完成（${res.elapsed.toFixed(1)}s${res.tags ? ` · 标签参考 ${res.tags.count} 个${res.tags.cached ? '（缓存）' : ''}` : ''}），检查后点「保存」`);
  } catch (e) {
    if (e.name === 'AbortError') return;
    clearInterval(tick);
    setStatus('失败');
    toast(`AI 整理失败：${errorText(e)}`, 'error', 8000);
  } finally {
    clearInterval(tick);
    if (ai.busy === ctl) ai.busy = null;
    for (const b of [btn, document.querySelector('#pd-right .ai-bar .btn.accent')]) {
      b?.classList.remove('busy');
      if (b) b.disabled = false;
    }
  }
}

// ═══════════════════════════ Danbooru 检索导入 ═══════════════════════════
export function openDanbooru(ctx) {
  const p = ctx.p;
  const input = el('input', { type: 'text', value: p.character_tag || p.name || '', placeholder: '角色名（中文 / 日文 / 英文）或 Danbooru 标签' });
  const seriesIn = el('input', { type: 'text', value: p.series || '', placeholder: '作品（可选），如 碧蓝档案 / blue archive', style: { display: 'none' } });
  const searchBtn = el('button', { class: 'btn' }, '搜索');
  const results = el('div', { class: 'db-list' });
  const varBox = el('div', { class: 'db-vars' });
  const status = el('span', { class: 'muted small' });
  const sel = llmSelect();
  const split = el('input', { type: 'checkbox', checked: true });
  const importBtn = el('button', { class: 'btn accent', disabled: true }, '导入');
  const st = { picked: null, vars: [], varsAt: null, checked: new Set(), ctl: null, fuzzy: false };
  // 两种查法：直接搜索（按名字 / 标签补全，原来的方式）；模糊搜索（名字可只写一部分 + 作品）
  const modeBtns = [['直接搜索', false], ['模糊搜索', true]].map(([label, fz]) =>
    el('button', { class: fz === st.fuzzy ? 'on' : '', onclick: () => setMode(fz) }, label));
  const setMode = (fz) => {
    st.fuzzy = fz;
    modeBtns.forEach((b, i) => b.classList.toggle('on', (i === 1) === fz));
    seriesIn.style.display = fz ? '' : 'none';
    input.placeholder = fz ? '名字，可以只写一部分，如 晶 / 爱丽丝 / akira' : '角色名（中文 / 日文 / 英文）或 Danbooru 标签';
  };

  const m = modal({
    title: '从 Danbooru 导入已有角色', wide: true, actions: [],
    onClose: () => st.ctl?.abort(),
    body: el('div', { class: 'db-modal' },
      el('div', { class: 'db-search' }, el('div', { class: 'seg' }, modeBtns), input, seriesIn, searchBtn),
      results, varBox,
      el('div', { class: 'db-foot' },
        el('label', { class: 'pv-field grow' }, el('span', {}, 'LLM'), sel),
        el('label', { class: 'checkline', title: '关闭后只导入角色标签、作品、别名与变体造型，共现标签全部放进「未归类」由你手动分配' }, split, 'AI 拆分'),
        status,
        el('button', { class: 'btn ghost', onclick: () => m.close() }, '取消'),
        importBtn)),
  });
  loadModels().then(() => { if (!ai.models.length) { split.checked = false; split.disabled = true; } });

  async function search() {
    const q = input.value.trim();
    if (!q) return;
    const fuzzy = st.fuzzy, series = fuzzy ? seriesIn.value.trim() : '';
    st.picked = null;
    importBtn.disabled = true;
    fill(varBox);
    fill(results, el('div', { class: 'muted small' }, fuzzy ? '搜索中…（约 3–6 秒）' : '检索中…'));
    try {
      const list = await api.post('/api/personas/danbooru/search', { q, series, fuzzy });
      const other = series && list.some(c => c.series_match) ? list.filter(c => c.series_match === false).length : 0;
      fill(results, list.length ? [
        ...list.map(c => el('button', { class: `db-row${c.series_match === false && other ? ' dim' : ''}`, onclick: (e) => pick(c, e.currentTarget) },
          el('b', { class: 'mono' }, c.text), c.matched && c.matched !== c.tag && c.matched !== c.text ? el('span', { class: 'muted small' }, `← ${c.matched}`) : null,
          el('span', { class: 'grow' }),
          c.series_match === false && other ? el('span', { class: 'muted small' }, '其它作品') : null,
          c.in_library ? el('span', { class: 'badge ok' }, '已在角色库') : null,
          el('span', { class: 'muted small mono' }, `${c.post_count} 张`))),
        series && !list.some(c => c.series_match !== null) ? el('div', { class: 'muted small' }, `没认出作品「${series}」，上面的结果没有按作品筛选；换个写法试试（英文名、日文名）`) : null]
        : el('div', { class: 'muted small' }, `Danbooru 上没有找到「${q}」${fuzzy ? '，少写几个字或换个写法试试' : '，试试英文名、罗马音，或切到「模糊搜索」'}`));
    } catch (e) { fill(results); toastError(e); }
  }

  async function loadVariants(c, refresh = false) {
    fill(varBox, el('div', { class: 'muted small' }, refresh ? '正在更新变体列表…' : '查找造型变体…'));
    let r = { items: [], cached_at: null };
    try { r = await api.post('/api/personas/danbooru/variants', { tag: c.tag, refresh }); } catch (e) { toastError(e); }
    if (st.picked !== c) return;
    st.vars = r.items;
    st.checked = new Set([...st.checked].filter(t => st.vars.some(v => v.tag === t)));
    const head = el('div', { class: 'db-vars-head' },
      el('b', {}, '造型变体'),
      el('span', { class: 'muted' }, '勾选的各成一套造型，变体标签写进它的「专属角色标签」，最多 6 个'),
      el('span', { class: 'grow' }),
      r.cached_at ? el('span', { class: 'muted' }, `列表来自角色库，更新于 ${fmtTime(r.cached_at)}`) : null,
      r.cached_at ? el('button', { class: 'btn small ghost', onclick: () => loadVariants(c, true) }, '更新列表') : null);
    fill(varBox, st.vars.length ? [head,
      el('div', { class: 'db-varlist' }, st.vars.map(v => el('label', { class: 'checkline' },
        el('input', { type: 'checkbox', checked: st.checked.has(v.tag), onchange: (e) => {
          if (e.target.checked) {
            if (st.checked.size >= 6) { e.target.checked = false; return toast('最多勾选 6 个', 'warn'); }
            st.checked.add(v.tag);
          } else st.checked.delete(v.tag);
        } }),
        el('span', { class: 'mono' }, v.text), el('span', { class: 'muted small' }, `${v.post_count} 张`),
        v.in_library ? el('span', { class: 'badge ok small-badge' }, '已在角色库') : null)))]
      : [head, el('div', { class: 'muted small' }, '这个角色在 Danbooru 上没有造型变体')]);
  }

  async function pick(c, row) {
    st.picked = c;
    st.vars = [];
    st.checked = new Set();
    for (const r of results.children) r.classList.toggle('on', r === row);
    importBtn.disabled = false;
    await loadVariants(c);
  }

  async function doImport() {
    if (!st.picked) return;
    if (split.checked && !model()) return toast('请先在 菜单 → LLM 中勾选模型，或取消「AI 拆分」', 'warn');
    st.ctl = cancellable();
    importBtn.classList.add('busy');
    importBtn.disabled = true;
    const t0 = Date.now();
    const tick = setInterval(() => {
      status.textContent = `${split.checked ? '取资料 → AI 拆分' : '取资料'}中… ${Math.round((Date.now() - t0) / 1000)}s`;
    }, 500);
    try {
      const res = await api.post('/api/personas/import', {
        request_id: st.ctl.rid,
        tag: st.picked.tag, variants: [...st.checked], split: split.checked, name: ctx.p.name, description: ai.desc,
        variant_counts: st.vars.filter(v => st.checked.has(v.tag)), ...(split.checked ? llmBody() : {}),
      }, { signal: st.ctl.signal });
      clearInterval(tick);
      m.close();
      await applyImport(ctx, res);
    } catch (e) {
      if (e.name !== 'AbortError') { status.textContent = '失败'; toast(`导入失败：${errorText(e)}`, 'error', 8000); }
    } finally {
      clearInterval(tick);
      importBtn.classList.remove('busy');
      importBtn.disabled = !st.picked;
    }
  }

  searchBtn.addEventListener('click', search);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') search(); });
  seriesIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') search(); });
  importBtn.addEventListener('click', doImport);
  setTimeout(() => input.focus(), 30);
  if (input.value.trim()) search();
}

function chooseMode() {
  return new Promise((resolve) => {
    modal({
      title: '这个人设已有内容', stack: true, onClose: (v) => resolve(v || null),
      body: '导入的基础外观、外观描述与造型怎么处理？（之后都可以撤销）',
      actions: [
        { label: '覆盖', sub: '用导入结果替换基础标签、外观描述与造型', kind: 'accent', value: 'overwrite' },
        { label: '保留已有内容', sub: '已有内容保留；新的造型追加在后面', value: 'merge' },
        { label: '取消', kind: 'ghost', value: null },
      ],
    });
  });
}

async function applyImport(ctx, res) {
  const p = ctx.p, d = res.draft;
  const has = p.base_tags.trim() || p.appearance_desc.trim() || p.outfits.some(o => o.tags.trim());
  const mode = has ? await chooseMode() : 'overwrite';
  if (!mode) return;
  snapshot(ctx, '导入');
  const over = mode === 'overwrite';
  const set = (k) => { if (d[k] && (over || !String(p[k] || '').trim())) p[k] = d[k]; };
  p.kind = 'existing';
  p.source_tag = d.source_tag;
  if (!p.name.trim()) p.name = d.name || d.character_tag;
  set('character_tag');
  set('series');
  if (d.count && (over || !p.count.trim())) p.count = d.count;
  set('base_tags');
  set('appearance_desc');
  const seen = new Set([p.name, ...p.aliases].map(x => x.toLowerCase()));
  p.aliases = [...p.aliases, ...d.aliases.filter(a => !seen.has(a.toLowerCase()))];
  if (d.outfits.length) {
    if (over) p.outfits = d.outfits;
    else {
      const keep = p.outfits.filter(o => o.tags.trim() || o.desc.trim());
      const names = new Set(keep.map(o => o.name));
      const add = d.outfits.filter(o => !names.has(o.name)).map(o => ({ ...o, default: false }));
      p.outfits = [...keep, ...add];
      if (!p.outfits.some(o => o.default) && p.outfits.length) p.outfits[0].default = true;
    }
  }
  ctx.setUnassigned(res.unassigned);
  ctx.rerender();
  for (const w of res.warnings || []) toast(w, 'warn', 6000);
  emit('llm-models-changed');       // 思考档位可能被自动改掉
  const saved = res.variants_saved ? `，${res.variants_saved} 个勾选的变体也已存入` : '';
  toast(`已导入 ${d.character_tag}（${res.library === 'cached' ? '角色库已有资料' : '已存入角色库'}${saved}），检查后点「保存」`, 'ok', 5000);
}

