// 人设页：卡片列表、详情编辑（右栏）、填入绘图页；绘图页的「从人设选择」。左栏见 personaside.js，AI 见 personaai.js。
import { api } from './api.js';
import { $, el, fill, toast, toastError, confirmDialog, modal, fmtTime } from './ui.js';
import { app, on, emit, saveForm, syncAll } from './store.js';
import { createSide } from './personaside.js';
import { aiSection, openDanbooru, resetAi } from './personaai.js';

export const KIND = { original: '原创', existing: '已有角色' };
const EDIT_KEYS = ['name', 'aliases', 'kind', 'character_tag', 'series', 'count', 'artist', 'groups', 'base_tags',
  'appearance_desc', 'outfits', 'source_tag'];
const st = {
  view: 'list', q: '', kind: '', group: '', list: { items: [], groups: [], total: 0 },
  edit: null, orig: '', unassigned: [], target: 'base', timer: null,
};
let side;

export const rid = () => Math.random().toString(16).slice(2, 10);
export const joinTags = (...xs) => xs.map(x => (x || '').trim().replace(/^,|,$/g, '').trim()).filter(Boolean).join(', ');
const splitList = (s) => (s || '').split(/[,，、\n]/).map(x => x.trim()).filter(Boolean);
export const atArtist = (s) => splitList(s).map(t => (t.startsWith('@') ? t : '@' + t)).join(', ');
export const coverUrl = (p, thumb = true) => `/api/files/persona/${p.id}/cover?${thumb ? 'thumb=1&' : ''}v=${p.cover}`;

// 详情页的公共接口：左栏与 AI 模块通过它读写当前编辑中的人设
const ctx = {
  get p() { return st.edit; },
  groups: () => st.list.groups.map(g => g.name),
  dirty: () => isDirty(),
  save: () => save(),
  rerender: () => { drawHeader(); drawRight(); side.update(); },
  changed: () => { drawHeader(); side.update(); },
  setUnassigned: (list) => { st.unassigned = list || []; drawRight(); },
  coverChanged: (p) => { Object.assign(st.edit, { cover: p.cover, cover_gid: p.cover_gid }); st.list.stale = true; },
};

export function initPersona() {
  side = createSide(ctx);
  on('tab', (t) => {
    if (t !== 'personas') return;
    if (st.view === 'list') loadList();
    else side.update();
  });
  $('#persona-pick').addEventListener('click', openPicker);
  on('persona-open', async (id) => {      // AI 填写参考资料里的「打开人设」
    if (st.view === 'detail' && st.edit?.id === id) return;
    if (st.view === 'detail' && isDirty()
        && !await confirmDialog('放弃未保存的修改？', '当前人设有修改还没有保存。', '放弃修改', true)) return;
    openDetail(id);
  });
  window.addEventListener('beforeunload', (e) => { if (st.view === 'detail' && isDirty()) { e.preventDefault(); e.returnValue = ''; } });
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && st.view === 'detail'
        && $('#page-personas').classList.contains('active')) {
      e.preventDefault();
      save();
    }
  });
  drawList();
}

// ═══════════════════════════ 列表 ═══════════════════════════
async function loadList() {
  const qs = new URLSearchParams({ q: st.q, kind: st.kind, group: st.group });
  try { st.list = await api.get(`/api/personas?${qs}`); } catch (e) { toastError(e); }
  if (st.view === 'list') renderGrid();
}

function drawList() {
  const search = el('input', { type: 'text', placeholder: '搜索名称、别名、角色标签、作品、分组', value: st.q });
  search.addEventListener('input', () => {
    clearTimeout(st.timer);
    st.timer = setTimeout(() => { st.q = search.value; loadList(); }, 250);
  });
  const seg = el('span', { class: 'seg', id: 'pl-kind' },
    [['', '全部'], ['original', '原创'], ['existing', '已有角色']].map(([v, t]) =>
      el('button', { 'data-v': v, class: st.kind === v ? 'on' : '', onclick: () => { st.kind = v; loadList(); } }, t)));
  fill($('#persona-list'),
    el('div', { class: 'pl-head' },
      el('h2', {}, '人设'), el('span', { class: 'muted small', id: 'pl-count' }),
      el('span', { class: 'grow' }),
      el('button', { class: 'btn accent', title: '默认是原创人物；已有作品的角色把「原创人物」关掉，再用名称旁的「🔍 Danbooru」导入',
        onclick: () => openDetail('new') }, '＋ 新建人设')),
    el('div', { class: 'pl-tools' }, search, seg),
    el('div', { class: 'chips pl-groups', id: 'pl-groups' }),
    el('div', { class: 'pgrid', id: 'pl-grid' }));
  renderGrid();
}

function renderGrid() {
  const { items, groups, total } = st.list;
  $('#pl-count').textContent = total ? `共 ${total} 个` : '';
  for (const b of $('#pl-kind').children) b.classList.toggle('on', b.dataset.v === st.kind);
  if (st.group && !groups.some(g => g.name === st.group)) st.group = '';
  fill($('#pl-groups'), groups.length ? [
    el('button', { class: `chip pick${st.group ? '' : ' on'}`, onclick: () => { st.group = ''; loadList(); } }, '全部分组'),
    ...groups.map(g => el('button', { class: `chip pick${st.group === g.name ? ' on' : ''}`,
      onclick: () => { st.group = st.group === g.name ? '' : g.name; loadList(); } }, g.name, el('em', {}, ` ${g.count}`)))] : []);
  const grid = $('#pl-grid');
  if (!items.length) {
    fill(grid, el('div', { class: 'empty-state' }, total ? '没有匹配的人设'
      : '还没有人设。点「＋ 新建人设」：默认是原创人物；已有作品的角色关掉「原创人物」，再用名称旁的「🔍 Danbooru」导入。'));
    return;
  }
  fill(grid, items.map(p => el('div', { class: 'pcard', onclick: () => openDetail(p.id) },
    el('div', { class: 'pcover' }, p.cover ? el('img', { src: coverUrl(p), alt: '', loading: 'lazy' })
      : el('span', { class: 'pinitial' }, [...p.name][0] || '?')),
    el('div', { class: 'pinfo' },
      el('div', { class: 'pname' }, p.name),
      el('div', { class: 'pmeta' },
        el('span', { class: `badge${p.kind === 'original' ? ' ok' : ''}` }, KIND[p.kind]),
        el('span', {}, p.series || (p.kind === 'original' ? '' : '—')),
        el('span', {}, `${p.outfits.length} 套造型`)),
      p.groups.length ? el('div', { class: 'pgroups' }, p.groups.map(g => el('span', { class: 'chip' }, g))) : null))));
}

// ═══════════════════════════ 详情 ═══════════════════════════
function blank(kind) {
  return {
    id: null, name: '', aliases: [], kind, character_tag: '', series: '',
    count: '1girl', artist: '', groups: [], base_tags: '', appearance_desc: '', source_tag: '',
    outfits: [{ id: rid(), name: '默认', tags: '', desc: '', character_tag: '', default: true }],
    cover: '', cover_gid: null, image_count: 0,
  };
}

const key = (p) => JSON.stringify(EDIT_KEYS.map(k => p[k]));
function isDirty() { return !!st.edit && key(st.edit) !== st.orig; }

export async function openDetail(id) {
  if (id === 'new') st.edit = blank('original');      // 默认原创人物，非原创自己关掉开关
  else {
    try { st.edit = await api.get(`/api/personas/${id}`); } catch (e) { return toastError(e); }
  }
  if (!st.list.groups.length) loadList();
  st.orig = id === 'new' ? '' : key(st.edit);
  st.unassigned = [];
  st.view = 'detail';
  resetAi();
  $('#persona-list').hidden = true;
  const root = $('#persona-detail');
  root.hidden = false;
  fill(root,
    el('div', { class: 'pd-head', id: 'pd-head' }),
    el('div', { class: 'pd-body' },
      el('div', { class: 'pd-left', id: 'pd-left' }),
      el('div', { class: 'pd-right', id: 'pd-right' })));
  drawHeader();
  drawRight();
  side.mount($('#pd-left'));
}

async function backToList() {
  if (isDirty() && !await confirmDialog('放弃未保存的修改？', '这个人设有修改还没有保存。', '放弃修改', true)) return;
  st.view = 'list';
  st.edit = null;
  side.unmount();
  $('#persona-detail').hidden = true;
  $('#persona-list').hidden = false;
  loadList();
}

function drawHeader() {
  const p = st.edit;
  fill($('#pd-head'),
    el('button', { class: 'btn ghost', onclick: backToList }, '← 返回'),
    el('h2', {}, p.name || (p.id ? '' : '新建人设')),
    el('span', { class: `badge${p.kind === 'original' ? ' ok' : ''}` }, KIND[p.kind]),
    isDirty() ? el('span', { class: 'badge warn' }, p.id ? '未保存' : '尚未保存') : null,
    el('span', { class: 'grow' }),
    el('button', { class: 'btn ghost', title: '用左侧「生成预览」中选中的造型填写绘图页的 count / character / series / appearance',
      onclick: () => fillDraw(p, side.outfitId()) }, '填入绘图页'),
    p.id ? el('button', { class: 'btn ghost danger', onclick: del }, '删除') : null,
    el('button', { class: 'btn accent', onclick: save, title: 'Ctrl+S' }, '保存'));
}

function payload() {
  const p = st.edit;
  return Object.fromEntries(EDIT_KEYS.map(k => [k, p[k]]));
}

async function save() {
  const p = st.edit;
  if (!p.name.trim()) { toast('请填写名称', 'warn'); $('#pf-name')?.focus(); return false; }
  try {
    const saved = p.id ? await api.put(`/api/personas/${p.id}`, payload()) : await api.post('/api/personas', payload());
    st.edit = saved;
    st.orig = key(saved);
    st.list.stale = true;
    toast('已保存', 'ok', 1500);
    ctx.rerender();
    return true;
  } catch (e) {
    toastError(e);
    return false;
  }
}

async function del() {
  const p = st.edit;
  const n = p.image_count || 0;
  const ok = await confirmDialog(`删除人设「${p.name}」？`,
    `资料与封面会被删除${n ? `，${n} 张预览图会从磁盘永久删除` : ''}，无法恢复。`, '永久删除', true);
  if (!ok) return;
  try {
    await api.del(`/api/personas/${p.id}`);
    toast('已删除', 'ok', 1500);
    st.orig = key(st.edit);    // 不再提示未保存
    backToList();
  } catch (e) { toastError(e); }
}

// ───────── 右栏 ─────────
function drawRight() {
  const root = $('#pd-right');
  if (!root) return;
  const scroll = root.scrollTop;
  const p = st.edit;
  const bind = (node, k, parse = (v) => v) => {
    node.addEventListener('input', () => { p[k] = parse(node.value); ctx.changed(); });
    return node;
  };
  const input = (k, ph, attrs = {}) => bind(el('input', { type: 'text', value: p[k] ?? '', placeholder: ph, ...attrs }), k);
  const area = (k, ph, rows = 3) => bind(el('textarea', { rows, placeholder: ph }, p[k] || ''), k);
  const field = (label, hint, ...nodes) => el('div', { class: 'field' },
    el('label', { class: 'fl' }, el('b', {}, label), hint ? el('i', {}, hint) : null), ...nodes);

  const aliases = bind(el('input', { type: 'text', value: p.aliases.join(', '), placeholder: '如：天童アリス, Tendou Aris' }), 'aliases', splitList);
  const groups = bind(el('input', { type: 'text', value: p.groups.join(', '), placeholder: '如：碧蓝档案, 游戏开发部' }), 'groups', splitList);
  const existing = ctx.groups().filter(g => g);
  const groupChips = existing.length ? el('div', { class: 'chips' }, existing.map(g => el('button', {
    class: `chip pick${p.groups.includes(g) ? ' on' : ''}`, type: 'button',
    onclick: () => { p.groups = p.groups.includes(g) ? p.groups.filter(x => x !== g) : [...p.groups, g]; ctx.rerender(); },
  }, g))) : null;
  // 原创人物：名称 / 别名不是 Danbooru 角色标签，AI 填写时会从 character 去掉
  const kindSwitch = el('label', { class: 'switch-row kind-switch',
    title: '开：原创人物，AI 填写时名称 / 别名不会写进 character；关：已有作品的角色（Danbooru 上有角色标签）' },
    el('input', { type: 'checkbox', checked: p.kind === 'original', onchange: (e) => {
      p.kind = e.target.checked ? 'original' : 'existing';
      if (p.kind === 'original') p.series = '';          // 原创人物不填作品（输入框置灰）
      ctx.rerender();
    } }),
    el('span', { class: 'switch' }), el('span', {}, '原创人物'));

  const nameRow = el('div', { class: 'name-row' },
    input('name', '如：天童爱丽丝', { id: 'pf-name' }),
    el('button', { class: 'btn', type: 'button', title: '在 Danbooru 检索这个角色，并由 AI 拆分成基础外观与造型',
      onclick: () => openDanbooru(ctx) }, '🔍 Danbooru'));

  const sections = [
    el('section', { class: 'psec' },
      el('div', { class: 'psec-title' }, el('h3', {}, '基本信息'), kindSwitch),
      el('div', { class: 'grid2' },
        field('名称 *', '全库唯一，AI 填写时描述里出现即匹配', nameRow),
        field('别名', '逗号分隔，同样参与匹配', aliases)),
      el('div', { class: 'grid2' },
        field('角色标签', p.kind === 'original' ? '写进 character，原创一般留空' : 'Danbooru 角色标签，写进 character',
          input('character_tag', p.kind === 'original' ? '' : 'aris (blue archive)')),
        field('作品', p.kind === 'original' ? '原创人物不填作品' : '写进 series',
          input('series', p.kind === 'original' ? '' : 'blue archive', { disabled: p.kind === 'original' }))),
      el('div', { class: 'grid2' },
        field('主体', '写进 count', input('count', '1girl', { list: 'count-list' })),
        field('画师偏好', '带 @，未指定画师时使用', input('artist', '@画师标签'))),
      field('分组', '逗号分隔，点下方已有分组快速添加', groups, groupChips)),

    el('section', { class: 'psec' },
      el('div', { class: 'psec-title' }, el('h3', {}, '基础外观'),
        el('span', { class: 'muted small' }, '只写本体特征（发型 / 瞳色 / 兽耳 / 体型 / 常驻饰品），服装写进下面的造型')),
      field('基础标签', 'Danbooru 风格，逗号分隔', area('base_tags', 'black hair, very long hair, one side up, hair between eyes, blue eyes, halo, rectangular halo', 3)),
      field('外观描述', '中文自然语言，AI 填写时随人设资料发给 LLM', area('appearance_desc', '黑色超长发，一侧扎起，刘海垂在两眼之间；蓝色眼睛；头顶方形光环……', 3)),
      unassignedBox()),

    el('section', { class: 'psec' },
      el('div', { class: 'psec-title' }, el('h3', {}, '服装差分'),
        el('span', { class: 'muted small' }, '每套造型 = 服装标签；预览与绘图 = 基础标签 + 选中造型')),
      ...p.outfits.map((o, i) => outfitBlock(o, i)),
      el('button', { class: 'btn ghost', type: 'button', onclick: () => {
        p.outfits.push({ id: rid(), name: '', tags: '', desc: '', character_tag: '', default: !p.outfits.length });
        ctx.rerender();
        $('#pd-right .outfit:last-of-type input')?.focus();
      } }, '＋ 添加造型')),

    aiSection(ctx),

    el('div', { class: 'pd-foot muted small' },
      p.id ? `id ${p.id}` : '尚未保存',
      p.source_tag ? [' · 来源 ', el('a', { href: `https://danbooru.donmai.us/wiki_pages/${encodeURIComponent(p.source_tag)}`,
        target: '_blank', rel: 'noreferrer' }, p.source_tag.replace(/_/g, ' '))] : '',
      p.created_at ? ` · 创建 ${fmtTime(p.created_at)}` : '', p.updated_at ? ` · 更新 ${fmtTime(p.updated_at)}` : ''),
  ];
  fill(root, sections);
  root.scrollTop = scroll;
}

function outfitBlock(o, i) {
  const p = st.edit;
  const bind = (node, k) => { node.addEventListener('input', () => { o[k] = node.value; ctx.changed(); }); return node; };
  const move = (d) => {
    const j = i + d;
    if (j < 0 || j >= p.outfits.length) return;
    [p.outfits[i], p.outfits[j]] = [p.outfits[j], p.outfits[i]];
    ctx.rerender();
  };
  return el('div', { class: `outfit${o.default ? ' default' : ''}` },
    el('div', { class: 'outfit-head' },
      bind(el('input', { type: 'text', value: o.name, placeholder: `造型名称，如 制服 / 女仆装` }), 'name'),
      el('label', { class: 'radio', title: '默认造型：没有指定造型时使用' },
        el('input', { type: 'radio', name: 'outfit-default', checked: o.default,
          onchange: () => { p.outfits.forEach(x => { x.default = x === o; }); ctx.rerender(); } }), '默认'),
      el('button', { class: 'btn ghost small', type: 'button', title: '上移', disabled: i === 0, onclick: () => move(-1) }, '↑'),
      el('button', { class: 'btn ghost small', type: 'button', title: '下移', disabled: i === p.outfits.length - 1, onclick: () => move(1) }, '↓'),
      el('button', { class: 'btn ghost small danger', type: 'button', title: '删除这套造型', onclick: () => {
        p.outfits.splice(i, 1);
        if (o.default && p.outfits.length) p.outfits[0].default = true;
        ctx.rerender();
      } }, '×')),
    bind(el('textarea', { rows: 2, placeholder: '服装标签：white shirt, blue necktie, white jacket, open jacket, black skirt, pleated skirt' }, o.tags), 'tags'),
    bind(el('input', { type: 'text', value: o.desc, placeholder: '中文描述（可选），如：白衬衫配蓝色领带，敞开的白色外套，黑色百褶裙' }), 'desc'),
    p.kind === 'existing' || o.character_tag ? el('label', { class: 'mini ct' }, el('span', {}, '专属角色标签（可选：Danbooru 的造型变体标签，选用这套造型时替换 character）'),
      bind(el('input', { type: 'text', value: o.character_tag, placeholder: 'aris (maid) (blue archive)' }), 'character_tag')) : null);
}

// AI 没有归类的标签：点击加入基础标签或某套造型
function unassignedBox() {
  const p = st.edit;
  if (!st.unassigned.length) return null;
  const targets = [['base', '基础标签'], ...p.outfits.map(o => [o.id, `造型：${o.name || '未命名'}`])];
  if (!targets.some(([v]) => v === st.target)) st.target = 'base';
  const sel = el('select', { onchange: (e) => { st.target = e.target.value; } },
    targets.map(([v, t]) => el('option', { value: v, selected: v === st.target }, t)));
  const add = (t) => {
    if (st.target === 'base') p.base_tags = joinTags(p.base_tags, t);
    else { const o = p.outfits.find(x => x.id === st.target); if (o) o.tags = joinTags(o.tags, t); }
    st.unassigned = st.unassigned.filter(x => x !== t);
    ctx.rerender();
  };
  return el('div', { class: 'unassigned' },
    el('div', { class: 'ua-head' }, el('span', { class: 'muted small' }, `未归类标签 ${st.unassigned.length} 个（AI 没有归入外观或造型，点击加入 →）`),
      sel, el('button', { class: 'btn ghost small', type: 'button', onclick: () => ctx.setUnassigned([]) }, '全部丢弃')),
    el('div', { class: 'chips' }, st.unassigned.map(t => el('button', { class: 'chip pick', type: 'button', onclick: () => add(t) }, t))));
}

// ═══════════════════════════ 填入绘图页 ═══════════════════════════
// outfitId：undefined = 默认造型；'' = 只用基础外观
export function fillDraw(p, outfitId) {
  const o = outfitId === '' ? null : (p.outfits.find(x => x.id === outfitId) || p.outfits.find(x => x.default) || null);
  const f = app.form.fields;
  const next = {
    count: p.count || f.count, character: o?.character_tag || p.character_tag, series: p.series,
    appearance: joinTags(p.base_tags, o?.tags),
  };
  if (!(f.artist || '').trim() && p.artist) next.artist = atArtist(p.artist);
  const changed = Object.keys(next).filter(k => (f[k] || '') !== next[k]);
  Object.assign(f, next);
  app.form.llm.persona = p.id ? { id: p.id, outfit_id: o?.id || '', name: p.name, outfit_name: o?.name || '' } : null;
  saveForm();
  syncAll();
  emit('change', 'fields');
  emit('goto', 'draw');
  for (const k of changed) {
    const node = document.querySelector(`[data-field="${k}"]`);
    if (node) { node.classList.remove('flash'); void node.offsetWidth; node.classList.add('flash'); }
  }
  toast(`已填入：${p.name || '人设'}${o ? ` · ${o.name}` : ' · 仅基础外观'}`, 'ok');
}

// ═══════════════════════════ 绘图页：从人设选择 ═══════════════════════════
async function openPicker() {
  let data;
  try { data = await api.get('/api/personas'); } catch (e) { return toastError(e); }
  if (!data.total) {
    toast('还没有人设，先到「人设」页新建', 'info');
    return emit('goto', 'personas');
  }
  const search = el('input', { type: 'text', placeholder: '搜索名称、别名、作品' });
  const box = el('div', { class: 'pick-grid' });
  const body = el('div', { class: 'picker' }, search, box);
  const m = modal({ title: '从人设选择', wide: true, body, actions: [{ label: '取消', kind: 'ghost' }] });
  const showList = (items) => fill(box, items.length ? items.map(p => el('div', { class: 'pick-card', onclick: () => choose(p.id) },
    el('div', { class: 'pcover small' }, p.cover ? el('img', { src: coverUrl(p), alt: '' }) : el('span', { class: 'pinitial' }, [...p.name][0])),
    el('div', { class: 'pname' }, p.name), el('div', { class: 'muted small' }, p.series || KIND[p.kind])))
    : el('div', { class: 'muted' }, '没有匹配的人设'));
  let timer;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try { showList((await api.get(`/api/personas?q=${encodeURIComponent(search.value)}`)).items); } catch (e) { toastError(e); }
    }, 200);
  });
  showList(data.items);
  setTimeout(() => search.focus(), 30);

  async function choose(id) {
    let p;
    try { p = await api.get(`/api/personas/${id}`); } catch (e) { return toastError(e); }
    const pickOutfit = (oid) => { m.close(); fillDraw(p, oid); };
    if (p.outfits.length <= 1) return pickOutfit(p.outfits[0]?.id ?? '');
    fill(body,
      el('div', { class: 'pick-head' }, el('button', { class: 'btn ghost small', onclick: () => fill(body, search, box) }, '← 返回'),
        el('b', {}, p.name), el('span', { class: 'muted small' }, '选择造型')),
      el('div', { class: 'pick-outfits' },
        p.outfits.map(o => el('button', { class: `pick-outfit${o.default ? ' default' : ''}`, onclick: () => pickOutfit(o.id) },
          el('b', {}, o.name, o.default ? el('em', {}, ' 默认') : null), el('span', {}, o.tags || '（无标签）'))),
        el('button', { class: 'pick-outfit', onclick: () => pickOutfit('') }, el('b', {}, '仅基础外观'), el('span', {}, '不加任何造型标签'))));
  }
}
