// AI 填写描述框下的「服装检索」栏（复制标签贴进描述，AI 就不会认错服装）：点开列出服装库的全部服装（按作品分组），输入时按中文名、名称映射、别名、标签、作品（含作品中文名、简称）、
// 常见标签筛选，空格分开的几个词要都对上。每行显示中文名、标签、作品，点「复制」把标签复制到剪贴板（不改动字段）。
// 只查服装库（菜单 → 服装库），不联网。
import { api } from './api.js';
import { el, toast } from './ui.js';
import { emit } from './store.js';

const fold = (s) => (s || '').normalize('NFKC').toLowerCase().replace(/[\s_\-·・.@()（）\[\]【】「」『』☆★!！?？'’]+/g, '');
const sp = (t) => (t || '').replace(/_/g, ' ');
const seriesLabel = (o) => o.series_names[0] || sp(o.series_tag) || '未注明作品';

// 对上名字的排最前，其次标签、作品，常见标签最后；有一个词哪里都对不上就不列
function score(o, terms) {
  const tiers = [[o.name, ...o.names, ...o.other_names], [o.tag], [o.series_tag, ...o.series_names], o.appearance];
  let total = 0;
  for (const t of terms) {
    const i = tiers.findIndex(list => list.some(x => fold(x).includes(t)));
    if (i < 0) return -1;
    total += i;
  }
  return total;
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = el('textarea', { style: { position: 'fixed', opacity: '0' } }, text);
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  toast(`已复制：${text}`, 'ok', 1800);
}

export function attachOutfitSearch(input) {
  let menu = null, items = [], all = [], active = -1;

  const close = () => { menu?.remove(); menu = null; items = []; active = -1; };
  const place = () => {
    if (!menu) return;
    const r = input.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight || !input.isConnected) return close();
    menu.style.left = `${r.left}px`;
    menu.style.width = `${Math.min(Math.max(r.width, 440), window.innerWidth - r.left - 8)}px`;
    const h = menu.offsetHeight;
    menu.style.top = `${r.bottom + 4 + h <= window.innerHeight - 8 ? r.bottom + 4 : r.top - 4 - h}px`;
  };
  const copy = (o) => copyText(sp(o.tag));
  const highlight = () => menu?.querySelectorAll('.ap-item').forEach((n, i) => {
    n.classList.toggle('on', i === active);
    if (i === active) n.scrollIntoView({ block: 'nearest' });
  });
  const gotoLib = () => { close(); emit('goto', 'menu'); emit('goto-panel', 'outfits'); };

  const render = () => {
    const q = input.value.trim();
    const terms = q.split(/\s+/).map(fold).filter(Boolean);
    items = terms.length
      ? all.map(o => [o, score(o, terms)]).filter(x => x[1] >= 0).sort((a, b) => a[1] - b[1]).map(x => x[0])
      : all;
    active = -1;
    const rows = [];
    let group = null;
    items.forEach((o, i) => {
      if (!terms.length && seriesLabel(o) !== group) {     // 没输入时按作品分组
        group = seriesLabel(o);
        rows.push(el('div', { class: 'op-group' }, group));
      }
      rows.push(el('div', { class: 'ap-item op-item', onclick: () => copy(o), onmouseenter: () => { active = i; highlight(); },
        title: `${o.name}\n${sp(o.tag)}\n点击复制标签` },
        el('div', { class: 'op-text' }, el('b', {}, o.name),
          el('span', { class: 'mono muted small' }, sp(o.tag) + (terms.length ? ` · ${seriesLabel(o)}` : ''))),
        el('span', { class: 'grow' }),
        el('span', { class: 'muted small mono' }, o.post_count ? `${o.post_count} 张` : ''),
        el('button', { class: 'btn small ghost', type: 'button', onclick: (e) => { e.stopPropagation(); copy(o); } }, '复制')));
    });
    menu?.remove();
    menu = el('div', { class: 'ap-menu op-menu', onmousedown: (e) => e.preventDefault() },   // 点菜单时输入框不失焦
      rows.length ? rows : el('button', { class: 'ap-empty', type: 'button', onclick: gotoLib },
        all.length ? `服装库里没有「${q}」，去 菜单 → 服装库 搜索添加` : '服装库还是空的，去 菜单 → 服装库 添加'));
    document.body.append(menu);
    place();
  };

  const open = async () => {
    try { all = (await api.get('/api/library/outfits/pick')).items; } catch { /* 用上次的 */ }
    if (document.activeElement === input) render();
  };

  input.setAttribute('autocomplete', 'off');
  input.addEventListener('focus', open);
  input.addEventListener('click', () => { if (!menu) open(); });
  input.addEventListener('input', () => { if (document.activeElement === input) render(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { close(); return; }
    if (!menu || !items.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      highlight();
    } else if (e.key === 'Enter' && active >= 0) {
      e.preventDefault();
      copy(items[active]);
    }
  });
  input.addEventListener('blur', () => setTimeout(close, 120));
  window.addEventListener('resize', () => place());
  document.addEventListener('scroll', (e) => { if (!e.target.closest?.('.op-menu')) place(); }, true);
}
