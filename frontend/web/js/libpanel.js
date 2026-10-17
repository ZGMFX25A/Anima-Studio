// 菜单 → 角色库 / 画师库 / 服装库：搜索、查看与手动纠正联网检索到的资料；手动新增。三个库共用这一套界面。
import { api } from './api.js';
import { $, el, fill, toast, toastError, confirmDialog, modal, fmtTime } from './ui.js';
import { app, on, emit } from './store.js';
import { clearArtistHints } from './artistpick.js';

const FIELD_ZH = { name: '名称', tag: '标准标签', series_tag: '作品', wiki: '简介', appearance: '外貌标签', notes: '补充说明' };
const sp = (t) => (t || '').replace(/_/g, ' ');

// full：有作品 / 外貌标签 / 简介；variants：造型变体挂在本体下；fuzzy：「从 Danbooru 添加」有模糊搜索；members：常穿的角色
export const LIB_CONFIG = {
  characters: {
    root: '#panel-chars', title: '角色库', noun: '角色', unit: '个', openEvent: 'chars-open', full: true, variants: true, fuzzy: true, manual: false,
    url: (tag) => `https://danbooru.donmai.us/wiki_pages/${encodeURIComponent(tag)}`,
    namePh: '显示名称，如 天童爱丽丝', tagPh: 'aris (blue archive)', tagLabel: '标准角色标签', tagHint: '写进 character',
    fetchPh: '角色名或标签，如 天童爱丽丝 / aris (blue archive)', searchPh: '在角色库中搜索：名字、标签或作品',
    fetchHint: '输入角色名或标签，直接添加最匹配的一个',
    notesPh: '原样发给 LLM，优先级最高。例如：默认服装为女仆装；瞳色是蓝色不是紫色',
    hint: '联网检索、在这里添加、人设导入用到的 Danbooru 角色都会存进来，下次直接用、不再联网；造型变体归在本体下面。资料有误点开改就行，改过的内容不会被自动覆盖。原创角色请在「人设」页新建。',
    empty: '角色库还是空的。可以在上面从 Danbooru 添加，或在 AI 填写中打开「联网检索」。',
    fuzzyHint: '名字可以只写一部分，加上作品更准；选中一个导入，它的造型变体会一起存进来',
    fuzzyNamePh: '名字，可以只写一部分，如 晶', appHint: '百分比 = 该角色图片中出现的比例',
  },
  outfits: {
    root: '#panel-outfits', title: '服装库', noun: '服装', unit: '套', openEvent: 'outfits-open', full: true, variants: false, fuzzy: true, members: true, manual: false,
    url: (tag) => `https://danbooru.donmai.us/wiki_pages/${encodeURIComponent(tag)}`,
    namePh: '显示名称，可以改成好认的中文名', tagPh: 'mischievous straight uniform (blue archive)', tagLabel: '服装标签', tagHint: '写进 appearance',
    fetchPh: '服装名或标签，如 イタズラ☆ストレート / mischievous straight uniform', searchPh: '在服装库中搜索：名字、标签或作品',
    fetchHint: '输入服装名（日文 / 英文）或标签，直接添加最匹配的一个',
    notesPh: '原样发给 LLM，优先级最高。例如：上衣是露脐短款，袖子是分离袖',
    hint: 'Danbooru 上单独建了标签、几个角色共用的服装（活动服装、偶像团体 / 学校制服等）。和造型变体的区别：变体是角色标签，写进 character；这里是普通标签，写进 appearance。这里只存用到的那套：描述里写了名字的、AI 从可选项里选用的、在这里添加的。描述里写了服装的名字就直接用；对不上名字时把这个作品的服装附给 AI 参考，只有明确指那一套（如「优香的偶像服」「活动里那套」）才用，只说「偶像服」按通用的写。',
    empty: '服装库还是空的。AI 填写用到某套作品服装时会存进来；也可以在上面添加，或用「模糊搜索」列出某个作品的全部服装挑一套。',
    fuzzyHint: '服装名（中日英文都行，可以只写一部分）和作品至少填一个；只填作品，或写的是说法（如 偶像服）时列出这个作品的全部服装；选一个导入',
    fuzzyNamePh: '服装名，如 初星学園 / イタズラ / cheerleader', appHint: '百分比 = 穿这套服装的图片中出现的比例，混有常穿它的角色本身的外貌',
  },
  artists: {
    root: '#panel-artists', title: '画师库', noun: '画师', unit: '个', openEvent: 'artists-open', full: false, manual: true,
    url: (tag) => `https://danbooru.donmai.us/artists/show_or_new?name=${encodeURIComponent(tag)}`,
    namePh: '显示名称，如 米山舞', tagPh: 'yoneyama mai', tagLabel: '标准画师标签', tagHint: '写进 artist（自动加 @）',
    fetchPh: '画师名或标签，如 米山舞 / yoneyama mai', searchPh: '在画师库中搜索：名字或标签',
    fetchHint: '输入画师名或标签，直接添加最匹配的一个',
    notesPh: '原样发给 LLM，优先级最高。例如：只用于厚涂风格时',
    hint: '描述中提到的画师会在联网检索时自动存进这里并永久保存。标签有误时点开修改即可；手动改过的内容不会被自动覆盖。',
    empty: '画师库还是空的。在描述里提到画师（如「米山舞的画风」）并打开「联网检索」，或在上方输入画师名从 Danbooru 添加。',
  },
};

const MIN_POSTS = 20;    // 与 server/web/variant_cache.py 一致：图片太少的变体不自动缓存
const VAR_VIEW_KEY = 'anima.library.variantView';
const FAMILY_ALL_KEY = 'anima.library.familyAll';

// aris_(maid)_(blue_archive) 相对 aris_(blue_archive) → maid（与 server/web/danbooru.py 的 qualifier 一致）
function qualifier(vtag, btag) {
  const m = btag.match(/^(.+?)_(\([^()]+\))$/);
  const stem = m ? m[1] : btag;
  const rest = vtag.startsWith(stem) ? vtag.slice(stem.length) : vtag;
  let qs = [...rest.matchAll(/\(([^()]+)\)/g)].map(x => x[1]);
  if (m && qs.length > 1) qs = qs.slice(0, -1);
  return (qs.join(' ') || vtag).replace(/_/g, ' ');
}

// 变体在切换标签上的短名：名字是「本体名(女仆)」时取括号里的，否则取标签里多出的括号
function variantLabel(v, b) {
  const m = v.name.match(/^(.*?)\s*[（(]([^()（）]+)[)）]$/);
  return m && b.name && m[1].trim() === b.name.trim() ? m[2] : qualifier(v.tag, b.tag);
}

// 编辑页内容快照：判断有没有没保存的修改（标签输入框显示空格写法，比较时统一成下划线）
const snapOf = (c) => JSON.stringify([c.name, (c.tag || '').replace(/ /g, '_'), (c.series_tag || '').replace(/ /g, '_'),
  c.names, c.appearance, c.notes || '', c.wiki || '']);

export function createLibrary(kind) {
  const cfg = LIB_CONFIG[kind];
  const base = `/api/library/${kind}`;
  // view: list / edit；addMode：direct（按名字直接添加）/ fuzzy（名字 + 作品模糊搜索，选中后导入），只用于角色库
  // varView：列表里变体的展开方式 menu（下拉菜单）/ inline（在本体下面逐行列出）；open：inline 时已展开的本体，默认全部折叠
  const st = { q: '', items: [], total: 0, variantTotal: 0, view: 'list', edit: null, timer: null,
    addMode: 'direct', fz: { name: '', series: '', res: null, busy: false },
    varView: 'menu', open: new Set(), menu: null,
    // 详情页：family = 本体 + 已缓存的变体（顶部切换标签）；familyAll = 显示全部（本体与变体从上到下排开）；snap / syncEdit 判断未保存的修改
    family: null, familyAll: false, snap: '', syncEdit: null, famToken: null };
  const ai = { models: null, key: '', busy: null, pending: null };     // 服装库「用 AI 起中文名」
  const AI_KEY = 'anima.outfits.llm';
  try { if (localStorage.getItem(VAR_VIEW_KEY) === 'inline') st.varView = 'inline'; } catch { /* 忽略 */ }
  try { st.familyAll = localStorage.getItem(FAMILY_ALL_KEY) === '1'; } catch { /* 忽略 */ }
  on(cfg.openEvent, (id) => openEditor(id));

  async function render() {
    if (st.view === 'edit' && st.edit) return drawEditor();
    await load();
  }

  async function load() {
    try {
      const d = await api.get(`${base}?q=${encodeURIComponent(st.q)}&limit=200`);
      st.items = d.items;
      st.total = d.total;
      st.variantTotal = d.variant_total || 0;
      // 搜索命中的是变体（而不是本体）时，列表展开方式下自动展开那一组
      st.open = new Set(st.q ? st.items.filter(c => c.hit && c.parent_tag).map(c => c.parent_tag) : []);
    } catch (e) { toastError(e); }
    if (st.view === 'list') drawList();   // 加载期间若已打开编辑页，不覆盖
  }

  // 本体 + 变体 成组（后端已按组排好：本体在前，变体紧跟其后）
  function families() {
    const out = [], byTag = new Map();
    for (const c of st.items) {
      const f = cfg.variants && c.parent_tag && byTag.get(c.parent_tag);
      if (f) f.vars.push(c);
      else { const nf = { base: c, vars: [] }; out.push(nf); byTag.set(c.tag, nf); }
    }
    return out;
  }

  // ───────── 变体下拉菜单 ─────────
  function closeMenu() {
    const m = st.menu;
    if (!m) return;
    st.menu = null;
    m.el.remove();
    m.btn.classList.remove('open');
    for (const [t, fn, opt] of m.off) t.removeEventListener(fn[0], fn[1], opt);
  }

  function openMenu(btn, fam) {
    const same = st.menu?.tag === fam.base.tag;
    closeMenu();
    if (same) return;     // 再点一次同一个按钮 = 收起
    const menu = el('div', { class: 'vmenu', role: 'menu' },
      el('div', { class: 'vmenu-head' }, el('b', {}, fam.base.name), el('span', { class: 'muted' }, ` · ${fam.vars.length} 个造型变体，点一个打开`)),
      el('div', { class: 'vmenu-list' }, fam.vars.map(v => el('button', {
        class: `vmenu-item${v.hit ? ' hit' : ''}`, role: 'menuitem', title: `${v.name}\n${sp(v.tag)}`,
        onclick: () => { closeMenu(); openEditor(v.id); } },
        el('span', { class: 'vm-name' }, v.name,
          v.edited.length ? el('span', { class: 'badge warn small-badge' }, '已编辑') : null),
        el('span', { class: 'vm-tag mono muted' }, sp(v.tag)),
        el('span', { class: 'vm-count mono muted' }, `${v.post_count} 张`)))));
    document.body.append(menu);
    // 贴在按钮下方（放不下就放上方），不超出窗口左右
    const r = btn.getBoundingClientRect(), h = menu.offsetHeight, w = menu.offsetWidth;
    const below = r.bottom + 4 + h <= window.innerHeight - 8 || r.top - 4 - h < 8;
    menu.style.top = `${below ? r.bottom + 4 : r.top - 4 - h}px`;
    menu.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - w - 8))}px`;
    btn.classList.add('open');
    const outside = (e) => { if (!menu.contains(e.target) && !btn.contains(e.target)) closeMenu(); };
    const key = (e) => { if (e.key === 'Escape') closeMenu(); };
    const off = [[document, ['mousedown', outside], true], [document, ['keydown', key], false],
      [window, ['resize', closeMenu], false], [window, ['scroll', (e) => { if (!menu.contains(e.target)) closeMenu(); }], true]];
    for (const [t, fn, opt] of off) t.addEventListener(fn[0], fn[1], opt);
    st.menu = { el: menu, btn, tag: fam.base.tag, off };
  }

  function setVarView(v) {
    st.varView = v;
    try { localStorage.setItem(VAR_VIEW_KEY, v); } catch { /* 忽略 */ }
    drawList();
  }

  // ───────── 列表 ─────────
  function drawList() {
    closeMenu();
    const root = $(cfg.root);
    const search = el('input', { type: 'text', class: 'lib-search', placeholder: cfg.searchPh, value: st.q });
    search.addEventListener('input', () => {
      clearTimeout(st.timer);
      st.timer = setTimeout(() => {
        st.q = search.value;
        load().then(() => { const s = $(`${cfg.root} .lib-search`); s.focus(); s.setSelectionRange(s.value.length, s.value.length); });
      }, 300);
    });
    const fetchName = el('input', { type: 'text', placeholder: cfg.fetchPh });
    const fetchBtn = el('button', { class: 'btn small accent' }, '添加');
    const doFetch = async () => {
      const name = fetchName.value.trim();
      if (!name) return;
      fetchBtn.classList.add('busy');
      fetchBtn.disabled = true;
      try {
        const c = await api.post(`${base}/fetch`, { name });
        clearArtistHints();
        toast(`已加入：${c.name} → ${sp(c.tag)}`, 'ok');
        openEditor(c.id);
      } catch (e) { toastError(e); }
      finally { fetchBtn.classList.remove('busy'); fetchBtn.disabled = false; }
    };
    fetchBtn.addEventListener('click', doFetch);
    fetchName.addEventListener('keydown', (e) => { if (e.key === 'Enter') doFetch(); });

    // 从 Danbooru 添加：角色库有「直接添加 / 模糊搜索」两个选项
    const fuzzy = cfg.fuzzy && st.addMode === 'fuzzy';
    const ADD_MODES = [['direct', '直接添加', cfg.fetchHint], ['fuzzy', '模糊搜索', cfg.fuzzyHint]];
    const modeSeg = cfg.fuzzy ? el('div', { class: 'seg small' }, ADD_MODES.map(([k, label]) =>
      el('button', { class: st.addMode === k ? 'on' : '', onclick: () => { st.addMode = k; drawList(); } }, label))) : null;
    const fzName = el('input', { type: 'text', placeholder: cfg.fuzzyNamePh, value: st.fz.name });
    const fzSeries = el('input', { type: 'text', placeholder: '作品（可选），如 碧蓝档案', value: st.fz.series });
    const fzBtn = el('button', { class: `btn small accent${st.fz.busy ? ' busy' : ''}`, disabled: st.fz.busy }, '搜索');
    const doSearch = async () => {
      st.fz.name = fzName.value.trim();
      st.fz.series = fzSeries.value.trim();
      if (cfg.members ? !st.fz.name && !st.fz.series : !st.fz.name) return toast(cfg.members ? '请输入服装名或作品' : '请输入名字', 'warn');
      st.fz.busy = true;
      st.fz.res = null;
      drawList();
      try { st.fz.res = await api.post(`${base}/search`, { name: st.fz.name, series: st.fz.series }); }
      catch (e) { toastError(e); }
      finally { st.fz.busy = false; if (st.view === 'list') drawList(); }
    };
    fzBtn.addEventListener('click', doSearch);
    for (const i of [fzName, fzSeries]) i.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
    const doImport = async (c, row) => {
      row.classList.add('busy');
      try {
        const r = await api.post(`${base}/import`, { tag: c.tag, name: st.fz.name });
        toast(`已加入：${r.name} → ${sp(r.tag)}${cfg.variants && (r.parent_tag || !r.variants_at) ? '，本体和造型变体正在后台存入' : ''}`, 'ok', 5000);
        openEditor(r.id);
      } catch (e) { toastError(e); row.classList.remove('busy'); }
    };

    const row = (c, fam, isVar) => {
      const n = fam?.vars.length || 0;
      const open = st.varView === 'inline' ? st.open.has(c.tag) : st.menu?.tag === c.tag;
      const hitInside = st.q && fam?.vars.some(v => v.hit);
      // 本体名字旁的折叠按钮：下拉菜单方式弹出菜单，列表方式在下面展开 / 收起
      const fold = n ? el('button', {
        class: `vfold${open ? ' open' : ''}${hitInside ? ' hit' : ''}`, title: hitInside ? '有变体符合搜索' : '',
        onclick: (e) => {
          e.stopPropagation();
          if (st.varView === 'menu') return openMenu(e.currentTarget, fam);
          if (st.open.has(c.tag)) st.open.delete(c.tag); else st.open.add(c.tag);
          drawList();
        } }, el('span', { class: 'caret' }, '▸'), `${n} 个变体`) : null;
      return el('div', { class: `crow${cfg.full ? '' : ' four'}${isVar ? ' variant' : ''}${isVar && c.hit ? ' hit' : ''}`, onclick: () => openEditor(c.id) },
        el('div', { class: 'cname' },
          el('div', { class: 'cname-line' }, isVar ? el('span', { class: 'vmark' }, '└') : null, el('span', { class: 'cname-text' }, c.name), fold),
          el('small', {}, c.names.filter(x => x !== c.name).slice(0, 4).join(' · '))),
        el('span', { class: 'mono' }, (cfg.full ? '' : '@') + sp(c.tag)),
        cfg.full ? el('span', { class: 'muted' }, sp(c.series_tag) || '—') : null,
        el('span', { class: 'muted mono' }, c.post_count ? `${c.post_count} 张` : '—'),
        el('span', {},
          c.parent_tag ? el('span', { class: 'badge' }, '变体') : el('span', { class: 'badge' }, c.source === 'manual' ? '手动新增' : 'Danbooru'),
          c.edited.length && c.source !== 'manual' ? el('span', { class: 'badge warn', title: c.edited.map(k => FIELD_ZH[k] || k).join('、') }, '已编辑') : null));
    };
    const fams = families();
    const rows = fams.flatMap(f => [row(f.base, f),
      ...(st.varView === 'inline' && st.open.has(f.base.tag) ? f.vars.map(v => row(v, null, true)) : [])]);
    const withVars = fams.filter(f => f.vars.length);
    const allOpen = withVars.length && withVars.every(f => st.open.has(f.base.tag));
    // 变体默认折叠；显示方式：下拉菜单 / 在列表中展开（可一键全部展开）
    const varTools = cfg.variants && withVars.length ? [
      el('span', { class: 'muted small' }, '变体显示方式'),
      el('div', { class: 'seg small' }, [['menu', '下拉菜单', '点「N 个变体」弹出下拉菜单'], ['inline', '在列表中展开', '点「N 个变体」在本体下面展开']]
        .map(([k, label, title]) => el('button', { class: st.varView === k ? 'on' : '', title, onclick: () => setVarView(k) }, label))),
      st.varView === 'inline' ? el('button', { class: 'btn small ghost', onclick: () => {
        st.open = allOpen ? new Set() : new Set(withVars.map(f => f.base.tag));
        drawList();
      } }, allOpen ? '全部折叠' : '全部展开') : null] : [];
    const count = st.total ? `${st.total} ${cfg.unit}${cfg.noun}${st.variantTotal ? ` · ${st.variantTotal} 个造型变体` : ''}` : '';

    fill(root,
      el('div', { class: 'panel-title' }, el('h2', {}, cfg.title), el('span', { class: 'muted small' }, count),
        el('span', { class: 'grow' }),
        cfg.manual ? el('button', { class: 'btn small', onclick: () => openEditor('new') }, '＋ 手动新增') : null),
      el('div', { class: 'hint' }, cfg.hint),
      el('section', { class: 'lib-add' },
        el('div', { class: 'lib-add-head' }, el('b', {}, '从 Danbooru 添加'), modeSeg,
          el('span', { class: 'muted small' }, ADD_MODES.find(m => m[0] === (fuzzy ? 'fuzzy' : 'direct'))[2])),
        el('div', { class: 'lib-add-row' }, ...(fuzzy ? [fzName, fzSeries, fzBtn] : [fetchName, fetchBtn])),
        fuzzy ? fuzzyResults(doImport) : null),
      cfg.members ? aiNameBar() : null,
      el('div', { class: 'lib-bar' }, search, el('span', { class: 'grow' }), ...varTools),
      el('div', { class: 'clist' },
        el('div', { class: `crow head${cfg.full ? '' : ' four'}` }, el('span', {}, '名称'), el('span', {}, '标准标签'),
          cfg.full ? el('span', {}, '作品') : null, el('span', {}, '图片数'), el('span', {}, '来源')),
        rows.length ? rows : el('div', { class: 'muted', style: { padding: '30px', textAlign: 'center' } },
          st.q ? `没有匹配「${st.q}」的${cfg.noun}` : cfg.empty)));
  }

  // 模糊搜索的候选：点一个导入（本体与变体在后台补齐）
  function fuzzyResults(onPick) {
    const fz = st.fz;
    if (fz.busy) return el('div', { class: 'fz-box muted small' }, cfg.members ? '搜索中…（约 2–5 秒）' : '搜索中…（约 3–6 秒）');
    if (!fz.res) return null;
    const { items, series_tags: keys } = fz.res;
    const narrowed = keys.length && items.some(c => c.series_match);
    const nMatch = items.filter(c => c.series_match).length;
    const sname = sp(keys[keys.length - 1] || '');
    const nHit = items.filter(c => c.exact).length;
    const noSeries = fz.series && !keys.length ? `没认出作品「${fz.series}」（换个写法试试：英文名、日文名）；` : '';
    const outfitSum = () => !items.length ? `${noSeries}没有找到${fz.name ? `「${fz.name}」，少写几个字或换个写法试试（日文名、英文名）` : ''}`
      : !fz.name ? `${noSeries}${sname} 的 ${items.length} 套服装，点一个导入`
        : !keys.length ? `${noSeries}${items.length} 个结果（服装库里的在前），点一个导入`
          : nHit ? `${nHit} 个名字对得上（排在前面），其余是 ${sname} 的其它服装；点一个导入`
            : `没有名字对得上「${fz.name}」的，下面是 ${sname} 的全部服装，看简介挑一个导入`;
    return el('div', { class: 'fz-box' },
      fz.res.offline ? el('div', { class: 'small', style: { color: 'var(--warn)' } }, `Danbooru 连不上（${fz.res.offline}），下面只有${cfg.title}里的结果`) : null,
      el('div', { class: 'muted small' },
        cfg.members ? outfitSum()
          : !items.length ? `Danbooru 上没有找到「${fz.name}」，少写几个字或换个写法试试`
          : fz.series && !keys.length ? `没认出作品「${fz.series}」，下面的结果没有按作品筛选；换个写法试试（英文名、日文名）`
            : narrowed ? `${items.length} 个结果，${nMatch} 个属于 ${sp(keys[keys.length - 1])}（排在前面），点一个导入`
              : keys.length ? `${items.length} 个结果，没有属于 ${sp(keys[keys.length - 1])} 的；点一个导入` : `${items.length} 个结果，点一个导入`),
      items.length ? el('div', { class: 'db-list' }, items.map(c => {
        const row = el('button', { class: `db-row${narrowed && c.series_match === false ? ' dim' : ''}${c.desc ? ' with-desc' : ''}`, onclick: () => onPick(c, row) },
          el('b', { class: 'mono' }, sp(c.tag)),
          c.matched && c.matched !== c.tag && c.matched !== sp(c.tag) ? el('span', { class: 'muted small' }, `← ${c.matched}`) : null,
          el('span', { class: 'grow' }),
          cfg.members && fz.name && keys.length && c.exact ? el('span', { class: 'badge small-badge' }, '名字对上') : null,
          cfg.variants && /^.+?_\(.+\)_\([^()]+\)$/.test(c.tag) ? el('span', { class: 'muted small' }, '造型变体') : null,
          narrowed && c.series_match === false ? el('span', { class: 'muted small' }, '其它作品') : null,
          c.in_library ? el('span', { class: 'badge ok' }, `已在${cfg.title}`) : null,
          el('span', { class: 'muted small mono' }, `${c.post_count} 张`),
          c.desc ? el('span', { class: 'db-desc muted small' }, c.desc) : null);
        return row;
      })) : null);
  }

  // 本体详情里的「造型变体」：列表缓存在本体上；图片数够的在后台缓存，其余可点「获取」。只重绘这一块，不影响没保存的输入
  function variantBox(c) {
    const box = el('div', { class: 'vlist' });
    const merge = (d) => { for (const k of ['variants', 'variants_at', 'variant_rows', 'variant_busy']) c[k] = d[k]; };
    const draw = () => {
      if (st.edit !== c) return;
      const rows = new Map((c.variant_rows || []).map(r => [r.tag, r]));
      const listed = c.variants || [];
      const all = [...listed, ...(c.variant_rows || []).filter(r => !listed.some(v => v.tag === r.tag))];
      const nCached = all.filter(v => rows.has(v.tag)).length;
      fill(box,
        el('div', { class: 'vlist-head muted small' },
          c._listBusy ? '正在获取变体列表…'
            : c.variants_at ? `${all.length} 个变体，${nCached} 个已存入角色库 · 列表更新于 ${fmtTime(c.variants_at)}` : '还没获取变体列表',
          c.variant_busy ? ' · 正在后台存入…' : '',
          el('span', { class: 'grow' }),
          c._listBusy ? null : el('button', { class: 'btn small ghost', onclick: fetchList, title: '从 Danbooru 重新获取变体列表，新出的变体会补进来' },
            c.variants_at ? '更新列表' : '获取变体列表')),
        all.length ? all.map(v => {
          const r = rows.get(v.tag);
          const [why, tip] = v.removed ? ['删除过', '单独删除过，不会自动存回来；需要时点「存入」']
            : v.post_count < MIN_POSTS ? [`图片少于 ${MIN_POSTS} 张`, `图片少于 ${MIN_POSTS} 张的变体共现标签不准，不自动存入；需要时点「存入」`]
              : c.variant_busy ? ['正在后台存入…', ''] : ['还没存入', ''];
          return el('div', { class: 'vrow' },
            r ? el('a', { class: 'vrow-name', href: '#', onclick: (e) => { e.preventDefault(); openEditor(r.id); } }, r.name,
              el('small', { class: 'mono' }, sp(v.tag)))
              : el('span', { class: 'vrow-name mono' }, sp(v.tag)),
            el('span', { class: 'muted mono' }, `${v.post_count} 张`),
            r ? el('span', {}, el('span', { class: 'badge ok small-badge' }, '已存入'))
              : el('span', { class: 'vrow-state', title: tip }, el('span', { class: 'muted' }, why),
                el('button', { class: 'btn small ghost', onclick: (e) => fetchOne(v.tag, e.currentTarget) }, '存入')));
        }) : c.variants_at ? el('div', { class: 'muted small' }, '这个角色在 Danbooru 上没有造型变体') : null);
      clearTimeout(c._poll);
      if (c.variant_busy) c._poll = setTimeout(poll, 2500);
    };
    const poll = async () => {
      if (st.view !== 'edit' || st.edit !== c) return;
      try { merge(await api.get(`${base}/${c.id}`)); } catch { return; }
      draw();
    };
    const fetchList = async () => {
      c._listBusy = true;
      draw();
      try { merge(await api.post(`${base}/${c.id}/variants`)); } catch (e) { toastError(e); }
      c._listBusy = false;
      draw();
    };
    const fetchOne = async (tag, btn) => {
      btn.textContent = '存入中…';
      btn.disabled = true;
      try { merge(await api.post(`${base}/${c.id}/variants/fetch`, { tag })); toast(`已存入角色库：${sp(tag)}`, 'ok'); }
      catch (e) { toastError(e); }
      draw();
    };
    // 本体还没取过变体列表（旧数据）：打开时自动取一次
    if (!c.variants_at && !c._autoFetched) { c._autoFetched = true; setTimeout(fetchList); } else setTimeout(draw);
    return box;
  }

  // ───────── 编辑 ─────────
  async function openEditor(id) {
    closeMenu();
    st.view = 'edit';
    st.family = null;
    if (id === 'new') {
      st.edit = { id: null, name: '', tag: '', series_tag: '', names: [], appearance: [], notes: '', wiki: '', other_names: [],
        source: 'manual', edited: [], post_count: 0 };
    } else {
      try { st.edit = await api.get(`${base}/${id}`); } catch (e) { toastError(e); st.view = 'list'; return; }
      if (cfg.variants) st.family = await loadFamily(st.edit);
    }
    st.snap = snapOf(st.edit);
    drawEditor();
  }

  // 本体 + 已缓存的变体（详情页顶部的切换标签、「显示全部」用）；没有变体时为 null
  async function loadFamily(c) {
    let b = c;
    if (c.parent?.id) { try { b = await api.get(`${base}/${c.parent.id}`); } catch { return null; } }
    else if (c.parent_tag) return null;     // 本体还没存进角色库
    const vars = b.variant_rows || [];
    if (!vars.length) return null;
    const head = { id: b.id, name: b.name, tag: b.tag, post_count: b.post_count };
    return { base: head, members: [{ ...head, isBase: true }, ...vars.map(v => ({ ...v, isBase: false }))] };
  }

  const isDirty = () => { st.syncEdit?.(); return !!st.edit && st.snap !== snapOf(st.edit); };
  const leaveEdit = async () => !isDirty() || confirmDialog('有未保存的修改', '切换后，刚才的修改会丢失。要继续吗？', '继续', true);
  const backToList = () => { st.view = 'list'; load(); };

  function setFamilyAll(v) {
    st.familyAll = v;
    try { localStorage.setItem(FAMILY_ALL_KEY, v ? '1' : '0'); } catch { /* 忽略 */ }
  }

  // 顶部：本体 / 各变体的切换标签 +「显示全部」开关
  function familyBar() {
    const f = st.family, c = st.edit;
    if (!f) return null;
    const root = $(cfg.root);
    const pick = async (m) => {
      if (st.familyAll) return root.querySelector(`.fam-card[data-id="${m.id}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (m.id !== c.id && await leaveEdit()) openEditor(m.id);
    };
    return el('div', { class: 'fam-bar' },
      el('div', { class: 'fam-tabs' }, f.members.map(m => el('button', {
        class: `fam-tab${m.isBase ? ' base' : ''}${!st.familyAll && m.id === c.id ? ' on' : ''}`,
        title: `${m.name}\n${sp(m.tag)} · ${m.post_count} 张${st.familyAll ? '\n点击跳到这一项' : ''}`, onclick: () => pick(m) },
      m.isBase ? `本体 · ${m.name}` : variantLabel(m, f.base)))),
      el('label', { class: 'switch-row fam-all', title: '本体和所有已存入的变体从上到下一起显示，方便对比' },
        el('input', { type: 'checkbox', checked: st.familyAll, onchange: (e) => {
          st.syncEdit?.();     // 没保存的输入先记下，切回单个查看时还在
          setFamilyAll(e.target.checked);
          drawEditor();
        } }),
        el('span', { class: 'switch' }), el('span', {}, '显示全部')));
  }

  // 显示全部：本体与各变体的资料从上到下排开（只读，「编辑」回到单个查看）
  async function drawFamilyAll() {
    const f = st.family;
    st.syncEdit = null;
    const root = $(cfg.root);
    const list = el('div', { class: 'fam-cards' }, el('div', { class: 'muted small' }, '读取中…'));
    fill(root,
      el('div', { class: 'panel-title' },
        el('button', { class: 'btn ghost small', onclick: backToList }, '← 返回列表'),
        el('h2', {}, f.base.name), el('span', { class: 'muted small' }, `本体与 ${f.members.length - 1} 个造型变体`)),
      familyBar(),
      el('div', { class: 'muted small' }, '变体里颜色淡的外貌标签本体也有（发色、光环这类），正常颜色的是这个变体特有的，多半是服装。'),
      list);
    const token = st.famToken = {};
    const ds = await Promise.all(f.members.map(m => api.get(`${base}/${m.id}`).catch(() => null)));
    if (st.famToken !== token || st.view !== 'edit' || !st.familyAll) return;
    const baseTags = new Set((ds[0]?.appearance || []).map(a => a.tag));
    fill(list, f.members.map((m, i) => familyCard(m, ds[i], baseTags)));
    if (st.edit.id !== f.base.id) list.querySelector(`.fam-card[data-id="${st.edit.id}"]`)?.scrollIntoView({ block: 'start' });
  }

  function familyCard(m, d, baseTags) {
    if (!d) return el('div', { class: 'fam-card muted' }, `${m.name}：读取失败`);
    const edit = async () => {
      setFamilyAll(false);
      if (m.id === st.edit.id) return drawEditor();
      if (await leaveEdit()) openEditor(m.id);
      else { setFamilyAll(true); drawEditor(); }
    };
    const wiki = d.wiki ? el('div', { class: 'fam-wiki', title: '点击展开 / 收起', onclick: () => wiki.classList.toggle('open') }, d.wiki) : null;
    return el('section', { class: `fam-card${m.id === st.edit.id ? ' current' : ''}`, 'data-id': m.id },
      el('div', { class: 'fam-card-head' },
        el('span', { class: `badge small-badge${m.isBase ? ' ok' : ''}` }, m.isBase ? '本体' : '变体'),
        el('b', {}, d.name), el('span', { class: 'mono muted' }, sp(d.tag)), el('span', { class: 'muted small' }, `${d.post_count} 张`),
        d.edited.length ? el('span', { class: 'badge warn small-badge', title: d.edited.map(k => FIELD_ZH[k] || k).join('、') }, '已编辑') : null,
        el('span', { class: 'grow' }),
        el('button', { class: 'btn small ghost', onclick: edit }, '编辑')),
      el('div', { class: 'fam-row' }, el('span', { class: 'fam-k' }, '外貌标签'),
        d.appearance.length ? el('div', { class: 'chips' }, d.appearance.map(a => el('span', {
          class: `chip${!m.isBase && baseTags.has(a.tag) ? ' same' : ''}` },
        sp(a.tag), el('em', {}, a.freq != null ? ` ${Math.round(a.freq * 100)}%` : ' 手动')))) : el('span', { class: 'muted' }, '—')),
      d.notes ? el('div', { class: 'fam-row' }, el('span', { class: 'fam-k' }, '补充说明'), el('div', {}, d.notes)) : null,
      wiki ? el('div', { class: 'fam-row' }, el('span', { class: 'fam-k' }, '简介'), wiki) : null);
  }

  function drawEditor() {
    if (st.familyAll && st.family) return drawFamilyAll();
    st.famToken = null;
    const c = st.edit;
    const root = $(cfg.root);
    const isNew = !c.id;
    const name = el('input', { type: 'text', value: c.name, placeholder: cfg.namePh });
    const tag = el('input', { type: 'text', value: sp(c.tag), placeholder: cfg.tagPh });
    const series = el('input', { type: 'text', value: sp(c.series_tag), placeholder: 'blue archive' });
    const notes = el('textarea', { rows: 3, placeholder: cfg.notesPh }, c.notes);
    const wiki = el('textarea', { rows: 5 }, c.wiki || '');
    const sync = () => {
      c.name = name.value; c.tag = tag.value; c.notes = notes.value;
      if (cfg.full) { c.series_tag = series.value; c.wiki = wiki.value; }
    };
    st.syncEdit = sync;
    const saved = (d) => {     // 保存 / 重新获取后：以服务器返回的为准，切换标签上的名字跟着改
      clearArtistHints();      // 画师的名称映射可能变了，artist 框的替换提示重新查
      st.edit = d;
      st.snap = snapOf(d);
      const m = st.family?.members.find(x => x.id === d.id);
      if (m) { m.name = d.name; if (m.isBase) st.family.base.name = d.name; }
    };

    const namesBox = chipEditor(c.names, n => n, (i) => { sync(); c.names.splice(i, 1); drawEditor(); },
      '添加名字后回车（昵称、其它语言的写法）', (v) => {
        sync();
        v.split(/[,，]/).map(x => x.trim()).filter(Boolean).forEach(x => { if (!c.names.includes(x)) c.names.push(x); });
        drawEditor();
      });
    const appBox = cfg.full ? chipEditor(c.appearance,
      a => [sp(a.tag), a.freq != null ? el('em', {}, ` ${Math.round(a.freq * 100)}%`) : el('em', {}, ' 手动')],
      (i) => { sync(); c.appearance.splice(i, 1); drawEditor(); },
      '添加外貌标签后回车，可用逗号分隔多个', (v) => {
        sync();
        v.split(/[,，]/).map(x => x.trim().replace(/ /g, '_')).filter(Boolean)
          .forEach(t => { if (!c.appearance.some(a => a.tag === t)) c.appearance.push({ tag: t, freq: null }); });
        drawEditor();
      }) : null;

    const save = async () => {
      sync();
      const body = { name: c.name, tag: c.tag, names: c.names, notes: c.notes };
      if (cfg.full) Object.assign(body, { series_tag: c.series_tag, appearance: c.appearance, wiki: c.wiki });
      try {
        saved(isNew ? await api.post(base, body) : await api.put(`${base}/${c.id}`, body));
        toast('已保存', 'ok');
        drawEditor();
      } catch (e) { toastError(e); }
    };
    const doRefresh = async (keep) => {
      try { saved(await api.post(`${base}/${c.id}/refresh`, { keep_edited: keep })); toast('已更新', 'ok'); drawEditor(); }
      catch (e) { toastError(e); }
    };
    const refresh = () => {
      if (!cfg.full) return doRefresh(true);   // 画师只更新作品数与别名，没有可被覆盖的字段
      modal({
        title: '从 Danbooru 重新获取', stack: true,
        body: c.edited.length ? `手动改过的项：${c.edited.map(k => FIELD_ZH[k] || k).join('、')}`
          : cfg.members ? '会用 Danbooru 的最新资料更新简介、常见标签与常穿的角色。' : '会用 Danbooru 的最新资料更新简介、作品与外貌标签。',
        actions: [
          { label: '保留手动修改', sub: '只更新没改过的项', kind: 'accent', onClick: () => doRefresh(true) },
          { label: '全部覆盖', sub: '丢弃手动修改（补充说明与名称映射不受影响）', kind: '', onClick: () => doRefresh(false) },
          { label: '取消', kind: 'ghost' },
        ],
      });
    };
    const nVar = (c.variant_rows || []).length;
    const del = async () => {
      const msg = c.parent_tag
        ? `「${c.name}」及其名称映射会从${cfg.title}删除；以后不会再自动存回来；需要时在本体的「造型变体」里点「存入」。`
        : cfg.members ? `「${c.name}」及其名称映射会从${cfg.title}删除；不影响 AI 填写认服装，下次用到时会重新存进来。`
        : `「${c.name}」${nVar ? `和它的 ${nVar} 个造型变体` : ''}及名称映射会从${cfg.title}删除；下次检索会重新从 Danbooru 获取。`;
      if (!await confirmDialog(`删除这个${c.parent_tag ? '变体' : cfg.noun}？`, msg, '删除', true)) return;
      try { await api.del(`${base}/${c.id}`); clearArtistHints(); toast('已删除', 'ok'); st.view = 'list'; load(); }
      catch (e) { toastError(e); }
    };
    const editedMark = (k) => c.edited.includes(k) && c.source !== 'manual' ? el('span', { class: 'badge warn small-badge' }, '已编辑') : null;
    const field = (label, k, node, hint) => el('div', { class: 'field' },
      el('label', { class: 'fl' }, el('b', {}, label), hint ? el('i', {}, hint) : null, editedMark(k)), node);

    fill(root,
      el('div', { class: 'panel-title' },
        el('button', { class: 'btn ghost small', onclick: backToList }, '← 返回列表'),
        el('h2', {}, isNew ? `手动新增${cfg.noun}` : c.name), el('span', { class: 'grow' }),
        !isNew && c.source !== 'manual' ? el('a', { class: 'small', href: cfg.url(c.tag), target: '_blank', rel: 'noreferrer' }, '在 Danbooru 查看') : null,
        !isNew ? el('button', { class: 'btn small ghost', onclick: refresh }, '从 Danbooru 重新获取') : null,
        !isNew ? el('button', { class: 'btn small ghost danger', onclick: del }, '删除') : null,
        el('button', { class: 'btn small accent', onclick: save }, isNew ? '添加' : '保存')),
      !isNew ? el('div', { class: 'muted small' },
        c.parent ? [el('span', { class: 'badge small-badge' }, '造型变体'), ' 本体：',
          c.parent.id ? el('a', { href: '#', onclick: (e) => { e.preventDefault(); openEditor(c.parent.id); } }, c.parent.name || sp(c.parent.tag))
            : `${sp(c.parent.tag)}（正在后台存入角色库）`, ' · '] : null,
        `来源：${c.source === 'manual' ? '手动新增' : 'Danbooru'}`, c.post_count ? ` · ${c.post_count} 张图` : '',
        c.fetched_at ? ` · 获取于 ${fmtTime(c.fetched_at)}`
          : cfg.members && c.source === 'danbooru' ? ' · 常见标签与常穿的角色还没取到（后台补齐中；也可以点「从 Danbooru 重新获取」）' : '') : null,
      familyBar(),
      el('div', { class: 'svc' },
        el('div', { class: cfg.full ? 'grid3 eq' : 'grid2' },
          field('名称', 'name', name, '显示用'),
          field(cfg.tagLabel, 'tag', tag, cfg.tagHint),
          cfg.full ? field('作品标签', 'series_tag', series, '写进 series') : null),
        cfg.variants && !isNew && !c.parent_tag ? field('造型变体', null, variantBox(c), `Danbooru 为这个角色的服装 / 形态单独建的标签；图片 ${MIN_POSTS} 张以上的会自动存入角色库`) : null,
        cfg.members && !isNew ? field('常穿的角色', null, membersBox(c), '百分比 = 这套服装的图片里出现这个角色的比例；「wiki」= Danbooru 简介里提到的') : null,
        field('名称映射', null, namesBox, cfg.members ? '描述里写了这些名字，就认成这套服装（只在同一作品的角色身上认）' : `这些名字都会对应到这个${cfg.noun}`),
        cfg.full ? field(cfg.members ? '常见标签' : '外貌标签', 'appearance', appBox, cfg.appHint) : null,
        field('补充说明', 'notes', notes, cfg.full ? '发给 LLM，优先于外貌标签' : '发给 LLM'),
        cfg.full ? field('简介', 'wiki', wiki, '来自 Danbooru wiki') : null));
  }

  // 服装库：用 AI 给还没有中文名的服装起中文名（顺带起作品的中文名与简称），绘图页的服装检索栏就能用中文搜到；手动改过名称的不动
  async function aiLoad() {
    if (!ai.models) {
      try { ai.models = await api.get('/api/llm/enabled'); } catch { ai.models = []; }
      let saved = '';
      try { saved = localStorage.getItem(AI_KEY) || ''; } catch { /* 忽略 */ }
      ai.key = [saved, app.form?.llm?.model_key].find(k => ai.models.some(m => m.key === k)) || ai.models[0]?.key || '';
    }
    try { ai.pending = await api.get(`${base}/cn-names`); } catch { ai.pending = null; }
  }

  function aiNameBar() {
    const box = el('section', { class: 'lib-add lib-ai' });
    const cancel = () => {
      ai.busy?.ctl.abort();
      api.post('/api/llm/cancel', { request_id: ai.busy?.rid }).catch(() => {});
    };
    const run = async () => {
      const m = ai.models?.find(x => x.key === ai.key);
      if (!m) return;
      ai.busy = { ctl: new AbortController(), rid: Math.random().toString(36).slice(2) + Date.now().toString(36) };
      draw();
      try {
        const r = await api.post(`${base}/cn-names`, { provider_id: m.provider_id, model_id: m.model_id, request_id: ai.busy.rid },
          { signal: ai.busy.ctl.signal });
        toast(`已起好中文名：${r.named} 套服装、${r.series} 个作品${r.partial ? '；中途出错只做了一部分，可以再点一次' : ''}`,
          r.partial ? 'warn' : 'ok', 5000);
      } catch (e) { if (e.name !== 'AbortError') toastError(e); }
      ai.busy = null;
      await aiLoad();
      if (st.view === 'list') load();
    };
    const draw = () => {
      const ms = ai.models || [];
      const p = ai.pending;
      const n = p ? p.outfits + p.series : 0;
      const sel = el('select', { disabled: !ms.length || !!ai.busy, onchange: (e) => {
        ai.key = e.target.value;
        try { localStorage.setItem(AI_KEY, ai.key); } catch { /* 忽略 */ }
      } }, ms.length ? ms.map(m => el('option', { value: m.key, selected: m.key === ai.key }, `${m.provider} · ${m.label}`))
        : el('option', { value: '' }, '未配置（菜单 → LLM）'));
      fill(box, el('div', { class: 'lib-add-head' },
        el('b', {}, '中文名'),
        el('span', { class: 'muted small' }, !p ? '' : n ? `${p.outfits} 套服装、${p.series} 个作品还没有中文名` : '都有中文名了'),
        el('span', { class: 'grow' }), sel,
        ai.busy ? [el('span', { class: 'muted small' }, '正在起名…'), el('button', { class: 'btn small ghost', onclick: cancel }, '取消')]
          : el('button', { class: 'btn small accent', disabled: !ms.length || !n, onclick: run,
            title: '给还没有中文名的服装起中文名（手动改过名称的不动），顺带给作品起中文名和常用简称；绘图页的服装检索栏就能用中文搜到' },
          '用 AI 起中文名')));
    };
    draw();
    if (!ai.busy) aiLoad().then(draw);
    return box;
  }

  // 服装详情的「常穿的角色」：在角色库里的可点开
  function membersBox(c) {
    const ms = c.members || [];
    if (!ms.length) return el('div', { class: 'muted small' }, c.fetched_at ? '没有特别常穿这套服装的角色' : '—');
    return el('div', { class: 'chips' }, ms.map(m => {
      const label = [m.name || sp(m.tag), el('em', {}, m.freq != null ? ` ${Math.round(m.freq * 100)}%` : ' wiki')];
      return m.id ? el('a', { class: 'chip link', href: '#', title: `${sp(m.tag)}\n在角色库中打开`,
        onclick: (e) => { e.preventDefault(); emit('goto-panel', 'chars'); emit('chars-open', m.id); } }, label)
        : el('span', { class: 'chip', title: `${sp(m.tag)}\n不在角色库` }, label);
    }));
  }

  return { render };
}

function chipEditor(list, render, onRemove, placeholder, onAdd) {
  const input = el('input', { type: 'text', placeholder });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && input.value.trim()) {
      e.preventDefault();
      onAdd(input.value);
      input.value = '';
    }
  });
  return el('div', { class: 'chip-editor' },
    list.map((x, i) => el('span', { class: 'chip' }, render(x), el('button', { title: '移除', onclick: () => onRemove(i) }, '×'))),
    input);
}
