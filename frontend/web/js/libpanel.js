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
    fetchPh: '输入角色名，从 Danbooru 添加', searchPh: '搜索名字（中文 / 日文 / 英文）、标签或作品',
    notesPh: '原样发给 LLM，优先级最高。例如：默认服装为女仆装；瞳色是蓝色不是紫色',
    hint: '联网检索到的角色（Danbooru 上的已有角色）会自动存进这里并永久保存，下次直接使用不再联网。资料有误时点开修改即可；手动改过的内容不会被自动覆盖。原创角色请在「人设」页新建。',
    empty: '角色库还是空的。在 AI 填写中打开「联网检索」，或在上方输入角色名从 Danbooru 添加。',
  },
  artists: {
    root: '#panel-artists', title: '画师库', noun: '画师', openEvent: 'artists-open', full: false, manual: true,
    url: (tag) => `https://danbooru.donmai.us/artists/show_or_new?name=${encodeURIComponent(tag)}`,
    namePh: '显示名称，如 米山舞', tagPh: 'yoneyama mai', tagLabel: '标准画师标签', tagHint: '写进 artist（自动加 @）',
    fetchPh: '输入画师名，从 Danbooru 添加', searchPh: '搜索名字（中文 / 日文 / 英文）或标签',
    notesPh: '原样发给 LLM，优先级最高。例如：只用于厚涂风格时',
    hint: '描述中提到的画师会在联网检索时自动存进这里并永久保存。标签有误时点开修改即可；手动改过的内容不会被自动覆盖。',
    empty: '画师库还是空的。在描述里提到画师（如「米山舞的画风」）并打开「联网检索」，或在上方输入画师名从 Danbooru 添加。',
  },
};

export function createLibrary(kind) {
  const cfg = LIB_CONFIG[kind];
  const base = `/api/library/${kind}`;
  const st = { q: '', items: [], total: 0, view: 'list', edit: null, timer: null };   // view: list / edit
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
    } catch (e) { toastError(e); }
    if (st.view === 'list') drawList();   // 加载期间若已打开编辑页，不覆盖
  }

  // ───────── 列表 ─────────
  function drawList() {
    const root = $(cfg.root);
    const search = el('input', { type: 'text', placeholder: cfg.searchPh, value: st.q });
    search.addEventListener('input', () => {
      clearTimeout(st.timer);
      st.timer = setTimeout(() => {
        st.q = search.value;
        load().then(() => { const s = $(`${cfg.root} input`); s.focus(); s.setSelectionRange(s.value.length, s.value.length); });
      }, 300);
    });
    const fetchName = el('input', { type: 'text', placeholder: cfg.fetchPh });
    const fetchBtn = el('button', { class: 'btn small' }, '从 Danbooru 添加');
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

    const rows = st.items.map(c => el('div', { class: `crow${cfg.full ? '' : ' four'}`, onclick: () => openEditor(c.id) },
      el('div', { class: 'cname' }, c.name, el('small', {}, c.names.filter(n => n !== c.name).slice(0, 4).join(' · '))),
      el('span', { class: 'mono' }, (cfg.full ? '' : '@') + sp(c.tag)),
      cfg.full ? el('span', { class: 'muted' }, sp(c.series_tag) || '—') : null,
      el('span', { class: 'muted mono' }, c.post_count ? `${c.post_count} 张` : '—'),
      el('span', {},
        el('span', { class: 'badge' }, c.source === 'manual' ? '手动新增' : 'Danbooru'),
        c.edited.length && c.source !== 'manual' ? el('span', { class: 'badge warn', title: c.edited.map(k => FIELD_ZH[k] || k).join('、') }, '已编辑') : null)));

    fill(root,
      el('div', { class: 'panel-title' }, el('h2', {}, cfg.title), el('span', { class: 'muted small' }, st.total ? `共 ${st.total} 个` : ''),
        el('span', { class: 'grow' }),
        cfg.manual ? el('button', { class: 'btn small', onclick: () => openEditor('new') }, '＋ 手动新增') : null),
      el('div', { class: 'hint' }, cfg.hint),
      el('div', { class: 'models-tools' }, search, el('span', { class: 'grow' }), fetchName, fetchBtn),
      el('div', { class: 'clist' },
        el('div', { class: `crow head${cfg.full ? '' : ' four'}` }, el('span', {}, '名称'), el('span', {}, '标准标签'),
          cfg.full ? el('span', {}, '作品') : null, el('span', {}, '图片数'), el('span', {}, '来源')),
        rows.length ? rows : el('div', { class: 'muted', style: { padding: '30px', textAlign: 'center' } },
          st.q ? `没有匹配的${cfg.noun}` : cfg.empty)));
  }

  // ───────── 编辑 ─────────
  async function openEditor(id) {
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
    const del = async () => {
      if (!await confirmDialog(`删除这个${cfg.noun}？`, `「${c.name}」及其名称映射会从${cfg.title}删除；下次检索会重新从 Danbooru 获取。`, '删除', true)) return;
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
        `来源：${c.source === 'manual' ? '手动新增' : 'Danbooru'}`, c.post_count ? ` · ${c.post_count} 张图` : '',
        c.fetched_at ? ` · 获取于 ${fmtTime(c.fetched_at)}` : '') : null,
      el('div', { class: 'svc' },
        el('div', { class: cfg.full ? 'grid3 eq' : 'grid2' },
          field('名称', 'name', name, '显示用'),
          field(cfg.tagLabel, 'tag', tag, cfg.tagHint),
          cfg.full ? field('作品标签', 'series_tag', series, '写进 series') : null),
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
