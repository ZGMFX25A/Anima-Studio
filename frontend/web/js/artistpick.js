// artist 输入框：下拉候选与替换提示。只查画师库（名字、别名、标签的一部分，不分大小写和全半角）；
// 画师库里没有的画师去 菜单 → 画师库 添加，这里不联网。绘图页的 artist 框与人设页的「画师偏好」共用。
import { api } from './api.js';
import { el, fill } from './ui.js';
import { emit } from './store.js';

const fold = (s) => (s || '').normalize('NFKC').toLowerCase().replace(/[\s_\-·・.@]+/g, '');
const tagText = (tag) => '@' + tag.replace(/_/g, ' ');

// 逗号分隔的各段与光标所在的那一段
function tokenAt(value, pos) {
  const parts = value.split(',');
  let i = 0, start = 0;
  for (; i < parts.length - 1; i++) {
    if (pos <= start + parts[i].length) break;
    start += parts[i].length + 1;
  }
  return { parts, i, text: parts[i].trim().replace(/^@/, '').trim() };
}

function setToken(input, idx, value) {
  const parts = input.value.split(',');
  parts[idx] = value;
  input.value = parts.map(s => s.trim()).filter(Boolean).join(', ');
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

// 同一时间只开一个下拉；页面滚动 / 窗口缩放时跟着输入框挪位置，输入框滚出视野才收起
// （表单里出现提示行时浏览器会微调滚动位置，直接收起会让下拉一闪就没）。只注册一次，人设页重绘不会越积越多
let closeOpen = null, placeOpen = null;
window.addEventListener('resize', () => placeOpen?.());
document.addEventListener('scroll', (e) => { if (!e.target.closest?.('.ap-menu')) placeOpen?.(); }, true);

export function attachArtistPicker(input) {
  let menu = null, items = [], active = -1, timer = null, seq = 0, idx = 0;

  const close = () => {
    menu?.remove(); menu = null; items = []; active = -1;
    if (closeOpen === close) closeOpen = placeOpen = null;
  };
  const place = () => {
    if (!menu) return;
    const r = input.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight || !input.isConnected) return close();
    menu.style.left = `${r.left}px`;
    menu.style.width = `${Math.max(r.width, 320)}px`;
    const h = menu.offsetHeight;
    menu.style.top = `${r.bottom + 4 + h <= window.innerHeight - 8 ? r.bottom + 4 : r.top - 4 - h}px`;
  };
  const cancel = () => { clearTimeout(timer); seq++; close(); };     // 连还在路上的查询一起作废
  const pick = (a) => {
    setToken(input, idx, tagText(a.tag));
    cancel();
    input.focus();
  };
  const highlight = () => menu?.querySelectorAll('.ap-item').forEach((n, i) => n.classList.toggle('on', i === active));

  const show = (q, list) => {
    close();
    items = list;
    menu = el('div', { class: 'ap-menu', onmousedown: (e) => e.preventDefault() },   // 点菜单时输入框不失焦
      list.length ? list.map((a, i) => {
        const f = fold(q);
        const alias = [a.name, a.tag, ...a.names].includes(q) ? '' : a.names.find(n => fold(n).includes(f) && n !== a.name && n !== a.tag) || '';
        return el('button', { class: 'ap-item', type: 'button', onclick: () => pick(a), onmouseenter: () => { active = i; highlight(); } },
          el('b', {}, a.name), el('span', { class: 'mono' }, tagText(a.tag)),
          alias ? el('span', { class: 'muted small' }, `别名 ${alias}`) : null,
          el('span', { class: 'grow' }), el('span', { class: 'muted small mono' }, a.post_count ? `${a.post_count} 张` : ''));
      })
        : el('button', { class: 'ap-empty', type: 'button', onclick: () => { close(); emit('goto', 'menu'); emit('goto-panel', 'artists'); } },
          `画师库里没有「${q}」，去 菜单 → 画师库 添加`));
    closeOpen?.();
    closeOpen = close;
    placeOpen = place;
    document.body.append(menu);
    place();
  };

  const query = () => {
    const t = tokenAt(input.value, input.selectionStart ?? input.value.length);
    idx = t.i;
    if (!t.text) return close();
    const my = ++seq;
    api.get(`/api/library/artists?q=${encodeURIComponent(t.text)}&limit=8`)
      .then(d => { if (my === seq && document.activeElement === input) show(t.text, d.items); })
      .catch(() => {});
  };

  input.setAttribute('autocomplete', 'off');
  input.addEventListener('input', (e) => {
    if (!e.isTrusted) return;          // 自己替换后触发的 input 不再查
    clearTimeout(timer);
    timer = setTimeout(query, 150);
  });
  input.addEventListener('keydown', (e) => {
    if (!menu || !items.length) { if (e.key === 'Escape') cancel(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      highlight();
    } else if (e.key === 'Enter' && active >= 0) {
      e.preventDefault();
      pick(items[active]);
    } else if (e.key === 'Escape') cancel();
  });
  input.addEventListener('blur', () => setTimeout(cancel, 120));
}

// 框里不是标准标签的写法（@k子、@K子老师）→ 画师库里对应的画师；同样的写法只问一次
const resolved = new Map();
export async function artistHints(tokens) {
  const ask = tokens.filter(t => !resolved.has(t));
  if (ask.length) {
    try {
      const d = await api.post('/api/library/artists/resolve', { names: ask });
      for (const t of ask) resolved.set(t, d.items.find(x => x.input === t) || null);
    } catch { return []; }
  }
  return tokens.map(t => resolved.get(t) && { ...resolved.get(t), input: t }).filter(Boolean);
}

// 在 box 里显示「k子 → @courreges ace 替换」，点替换改写 input 里那一段
export function renderArtistHints(box, input, hints) {
  fill(box, hints.map(h => el('span', { class: 'ap-hint' },
    `${h.input.replace(/^@/, '')} 是 ${h.name} → `, el('span', { class: 'mono' }, tagText(h.tag)), ' ',
    el('button', { type: 'button', onclick: () => {
      const parts = input.value.split(',');
      const i = parts.findIndex(s => s.trim() === h.input);
      if (i >= 0) setToken(input, i, tagText(h.tag));
    } }, '替换'))));
  box.hidden = !hints.length;
}

// 画师库改动后（新增 / 编辑），重新问
export function clearArtistHints() { resolved.clear(); }
