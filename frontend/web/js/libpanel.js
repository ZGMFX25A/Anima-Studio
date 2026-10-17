// 菜单 → 角色库 / 画师库：搜索、查看与手动纠正联网检索到的资料；手动新增。两个库共用这一套界面。
import { api } from './api.js';
import { $, el, fill, toast, toastError, confirmDialog, modal, fmtTime } from './ui.js';
import { on } from './store.js';

const FIELD_ZH = { name: '名称', tag: '标准标签', series_tag: '作品', wiki: '简介', appearance: '外貌标签', notes: '补充说明' };
const sp = (t) => (t || '').replace(/_/g, ' ');

export const LIB_CONFIG = {
  characters: {
    root: '#panel-chars', title: '角色库', noun: '角色', openEvent: 'chars-open', full: true, manual: false,
    url: (tag) => `https://danbooru.donmai.us/wiki_pages/${encodeURIComponent(tag)}`,
    namePh: '显示名称，如 天童爱丽丝', tagPh: 'aris (blue archive)', tagLabel: '标准角色标签', tagHint: '写进 character',
    fetchPh: '角色名或标签，如 天童爱丽丝 / aris (blue archive)', searchPh: '在角色库中搜索：名字、标签或作品',
    fetchHint: '输入角色名或标签，直接添加最匹配的一个',
    notesPh: '原样发给 LLM，优先级最高。例如：默认服装为女仆装；瞳色是蓝色不是紫色',
    hint: '联网检索、在这里添加、人设导入用到的 Danbooru 角色都会存进来，下次直接用、不再联网；造型变体归在本体下面。资料有误点开改就行，改过的内容不会被自动覆盖。原创角色请在「人设」页新建。',
    empty: '角色库还是空的。可以在上面从 Danbooru 添加，或在 AI 填写中打开「联网检索」。',
  },
  artists: {
    root: '#panel-artists', title: '画师库', noun: '画师', openEvent: 'artists-open', full: false, manual: true,
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

export function createLibrary(kind) {
  const cfg = LIB_CONFIG[kind];
  const base = `/api/library/${kind}`;
  // view: list / edit；addMode：direct（按名字直接添加）/ fuzzy（名字 + 作品模糊搜索，选中后导入），只用于角色库
  // varView：列表里变体的展开方式 menu（下拉菜单）/ inline（在本体下面逐行列出）；open：inline 时已展开的本体，默认全部折叠
  const st = { q: '', items: [], total: 0, variantTotal: 0, view: 'list', edit: null, timer: null,
    addMode: 'direct', fz: { name: '', series: '', res: null, busy: false },
    varView: 'menu', open: new Set(), menu: null };
  try { if (localStorage.getItem(VAR_VIEW_KEY) === 'inline') st.varView = 'inline'; } catch { /* 忽略 */ }
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
      const f = cfg.full && c.parent_tag && byTag.get(c.parent_tag);
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
        toast(`已加入：${c.name} → ${sp(c.tag)}`, 'ok');
        openEditor(c.id);
      } catch (e) { toastError(e); }
      finally { fetchBtn.classList.remove('busy'); fetchBtn.disabled = false; }
    };
    fetchBtn.addEventListener('click', doFetch);
    fetchName.addEventListener('keydown', (e) => { if (e.key === 'Enter') doFetch(); });

    // 从 Danbooru 添加：角色库有「直接添加 / 模糊搜索」两个选项
    const fuzzy = cfg.full && st.addMode === 'fuzzy';
    const ADD_MODES = [['direct', '直接添加', cfg.fetchHint],
      ['fuzzy', '模糊搜索', '名字可以只写一部分，加上作品更准；选中一个导入，它的造型变体会一起存进来']];
    const modeSeg = cfg.full ? el('div', { class: 'seg small' }, ADD_MODES.map(([k, label]) =>
      el('button', { class: st.addMode === k ? 'on' : '', onclick: () => { st.addMode = k; drawList(); } }, label))) : null;
    const fzName = el('input', { type: 'text', placeholder: '名字，可以只写一部分，如 晶', value: st.fz.name });
    const fzSeries = el('input', { type: 'text', placeholder: '作品（可选），如 碧蓝档案', value: st.fz.series });
    const fzBtn = el('button', { class: `btn small accent${st.fz.busy ? ' busy' : ''}`, disabled: st.fz.busy }, '搜索');
    const doSearch = async () => {
      st.fz.name = fzName.value.trim();
      st.fz.series = fzSeries.value.trim();
      if (!st.fz.name) return;
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
        toast(`已加入：${r.name} → ${sp(r.tag)}${r.parent_tag || !r.variants_at ? '，本体和造型变体正在后台补齐' : ''}`, 'ok', 5000);
        openEditor(r.id);
      } catch (e) { toastError(e); row.classList.remove('busy'); }
    };

    const row = (c, fam, isVar) => {
      const n = fam?.vars.length || 0;
      const open = st.varView === 'inline' ? st.open.has(c.tag) : st.menu?.tag === c.tag;
      const hitInside = st.q && fam?.vars.some(v => v.hit);
      // 本体名字旁的折叠按钮：下拉菜单方式弹出菜单，列表方式在下面展开 / 收起
      const fold = n ? el('button', {
        class: `vfold${open ? ' open' : ''}${hitInside ? ' hit' : ''}`, title: hitInside ? '有变体命中搜索' : '',
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
    // 变体默认折叠；展开方式：下拉菜单 / 在本体下面逐行展开（可一键全部展开）
    const varTools = cfg.full && withVars.length ? [
      el('span', { class: 'muted small' }, '变体展开方式'),
      el('div', { class: 'seg small' }, [['menu', '下拉菜单', '点「N 个变体」弹出菜单'], ['inline', '逐行展开', '点「N 个变体」在本体下面逐行列出']]
        .map(([k, label, title]) => el('button', { class: st.varView === k ? 'on' : '', title, onclick: () => setVarView(k) }, label))),
      st.varView === 'inline' ? el('button', { class: 'btn small ghost', onclick: () => {
        st.open = allOpen ? new Set() : new Set(withVars.map(f => f.base.tag));
        drawList();
      } }, allOpen ? '全部折叠' : '全部展开') : null] : [];
    const count = st.total ? `${st.total} 个${cfg.noun}${st.variantTotal ? ` · ${st.variantTotal} 个造型变体` : ''}` : '';

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
    if (fz.busy) return el('div', { class: 'fz-box muted small' }, '搜索中…（约 3–6 秒）');
    if (!fz.res) return null;
    const { items, series_tags: keys } = fz.res;
    const narrowed = keys.length && items.some(c => c.series_match);
    const nMatch = items.filter(c => c.series_match).length;
    return el('div', { class: 'fz-box' },
      el('div', { class: 'muted small' },
        !items.length ? `Danbooru 上没有找到「${fz.name}」，少写几个字或换个写法试试`
          : fz.series && !keys.length ? `没认出作品「${fz.series}」，下面的结果没有按作品筛选；换个写法试试（英文名、日文名）`
            : narrowed ? `${items.length} 个结果，${nMatch} 个属于 ${sp(keys[keys.length - 1])}（排在前面），点一个导入`
              : keys.length ? `${items.length} 个结果，没有属于 ${sp(keys[keys.length - 1])} 的；点一个导入` : `${items.length} 个结果，点一个导入`),
      items.length ? el('div', { class: 'db-list' }, items.map(c => {
        const row = el('button', { class: `db-row${narrowed && c.series_match === false ? ' dim' : ''}`, onclick: () => onPick(c, row) },
          el('b', { class: 'mono' }, sp(c.tag)),
          c.matched && c.matched !== c.tag && c.matched !== sp(c.tag) ? el('span', { class: 'muted small' }, `← ${c.matched}`) : null,
          el('span', { class: 'grow' }),
          /^.+?_\(.+\)_\([^()]+\)$/.test(c.tag) ? el('span', { class: 'muted small' }, '造型变体') : null,
          narrowed && c.series_match === false ? el('span', { class: 'muted small' }, '其它作品') : null,
          c.in_library ? el('span', { class: 'badge ok' }, '已在角色库') : null,
          el('span', { class: 'muted small mono' }, `${c.post_count} 张`));
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
            : c.variants_at ? `${all.length} 个变体，已缓存 ${nCached} 个 · 列表获取于 ${fmtTime(c.variants_at)}` : '还没获取变体列表',
          c.variant_busy ? ' · 后台缓存中…' : '',
          el('span', { class: 'grow' }),
          c._listBusy ? null : el('button', { class: 'btn small ghost', onclick: fetchList, title: '从 Danbooru 重新获取变体列表，新出的变体会补进来' },
            c.variants_at ? '刷新列表' : '获取列表')),
        all.length ? all.map(v => {
          const r = rows.get(v.tag);
          const why = v.removed ? '删除过，不会自动取回' : v.post_count < MIN_POSTS ? `不到 ${MIN_POSTS} 张，未自动缓存` : c.variant_busy ? '后台缓存中…' : '未缓存';
          return el('div', { class: 'vrow' },
            r ? el('a', { class: 'vrow-name', href: '#', onclick: (e) => { e.preventDefault(); openEditor(r.id); } }, r.name,
              el('small', { class: 'mono' }, sp(v.tag)))
              : el('span', { class: 'vrow-name mono' }, sp(v.tag)),
            el('span', { class: 'muted mono' }, `${v.post_count} 张`),
            r ? el('span', {}, el('span', { class: 'badge ok small-badge' }, '已缓存'))
              : el('span', { class: 'vrow-state' }, el('span', { class: 'muted' }, why),
                el('button', { class: 'btn small ghost', onclick: (e) => fetchOne(v.tag, e.currentTarget) }, '获取')));
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
      btn.textContent = '获取中…';
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
    if (id === 'new') {
      st.edit = { id: null, name: '', tag: '', series_tag: '', names: [], appearance: [], notes: '', wiki: '', other_names: [],
        source: 'manual', edited: [], post_count: 0 };
    } else {
      try { st.edit = await api.get(`${base}/${id}`); } catch (e) { toastError(e); st.view = 'list'; return; }
    }
    drawEditor();
  }

  function drawEditor() {
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
        st.edit = isNew ? await api.post(base, body) : await api.put(`${base}/${c.id}`, body);
        toast('已保存', 'ok');
        drawEditor();
      } catch (e) { toastError(e); }
    };
    const doRefresh = async (keep) => {
      try { st.edit = await api.post(`${base}/${c.id}/refresh`, { keep_edited: keep }); toast('已更新', 'ok'); drawEditor(); }
      catch (e) { toastError(e); }
    };
    const refresh = () => {
      if (!cfg.full) return doRefresh(true);   // 画师只更新作品数与别名，没有可被覆盖的字段
      modal({
        title: '从 Danbooru 重新获取', stack: true,
        body: c.edited.length ? `手动改过的项：${c.edited.map(k => FIELD_ZH[k] || k).join('、')}` : '会用 Danbooru 的最新资料更新简介、作品与外貌标签。',
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
        ? `「${c.name}」及其名称映射会从${cfg.title}删除；后台缓存不会再自动取回它，需要时在本体的「造型变体」里点「获取」。`
        : `「${c.name}」${nVar ? `和它的 ${nVar} 个造型变体` : ''}及名称映射会从${cfg.title}删除；下次检索会重新从 Danbooru 获取。`;
      if (!await confirmDialog(`删除这个${c.parent_tag ? '变体' : cfg.noun}？`, msg, '删除', true)) return;
      try { await api.del(`${base}/${c.id}`); toast('已删除', 'ok'); st.view = 'list'; load(); }
      catch (e) { toastError(e); }
    };
    const editedMark = (k) => c.edited.includes(k) && c.source !== 'manual' ? el('span', { class: 'badge warn small-badge' }, '已编辑') : null;
    const field = (label, k, node, hint) => el('div', { class: 'field' },
      el('label', { class: 'fl' }, el('b', {}, label), hint ? el('i', {}, hint) : null, editedMark(k)), node);

    fill(root,
      el('div', { class: 'panel-title' },
        el('button', { class: 'btn ghost small', onclick: () => { st.view = 'list'; load(); } }, '← 返回列表'),
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
        c.fetched_at ? ` · 获取于 ${fmtTime(c.fetched_at)}` : '') : null,
      el('div', { class: 'svc' },
        el('div', { class: cfg.full ? 'grid3 eq' : 'grid2' },
          field('名称', 'name', name, '显示用'),
          field(cfg.tagLabel, 'tag', tag, cfg.tagHint),
          cfg.full ? field('作品标签', 'series_tag', series, '写进 series') : null),
        cfg.full && !isNew && !c.parent_tag ? field('造型变体', null, variantBox(c), `Danbooru 为这个角色的服装 / 形态单独建的标签；${MIN_POSTS} 张以上的自动缓存`) : null,
        field('名称映射', null, namesBox, `这些名字都会对应到这个${cfg.noun}`),
        cfg.full ? field('外貌标签', 'appearance', appBox, '百分比 = 该角色图片中出现的比例') : null,
        field('补充说明', 'notes', notes, cfg.full ? '发给 LLM，优先于外貌标签' : '发给 LLM'),
        cfg.full ? field('简介', 'wiki', wiki, '来自 Danbooru wiki') : null));
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
