// 通用界面组件：元素构造、提示、弹窗、大图浏览、上传框。
import { uploadRef, errorText } from './api.js';

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ───────── 提示 ─────────
export function toast(message, kind = 'info', ms = 3800) {
  const t = el('div', { class: `toast ${kind}` }, message);
  $('#toasts').append(t);
  setTimeout(() => { t.style.transition = 'opacity .3s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, ms);
}
export const toastError = (e) => toast(errorText(e), 'error', 6000);

// ───────── 弹窗 ─────────
export function modal({ title, body, actions = [], wide = false, stack = false, onClose }) {
  const root = $('#modal-root');
  const close = (v) => { mask.remove(); document.removeEventListener('keydown', onKey); onClose && onClose(v); };
  const onKey = (e) => { if (e.key === 'Escape') close(null); };
  const acts = el('div', { class: `modal-actions${stack ? ' stack' : ''}` },
    actions.map(a => el('button', {
      class: `btn ${a.kind || ''}`,
      onclick: async () => {
        if (a.onClick) {
          const keep = await a.onClick();
          if (keep === true) return;
        }
        close(a.value);
      },
    }, a.label, a.sub ? el('small', {}, a.sub) : null)));
  const box = el('div', { class: `modal${wide ? ' wide' : ''}`, role: 'dialog' },
    title ? el('h3', {}, title) : null,
    el('div', { class: 'modal-body' }, body),
    actions.length ? acts : null);
  const mask = el('div', { class: 'modal-mask', onclick: (e) => { if (e.target === mask) close(null); } }, box);
  root.append(mask);
  document.addEventListener('keydown', onKey);
  return { close };
}

export function confirmDialog(title, message, okLabel = '确定', danger = false) {
  return new Promise((resolve) => {
    modal({
      title, body: message, onClose: (v) => resolve(v === true),
      actions: [{ label: '取消', kind: 'ghost', value: false }, { label: okLabel, kind: danger ? 'danger' : 'accent', value: true }],
    });
  });
}

// ───────── 大图浏览 ─────────
export function lightbox(items, index = 0) {
  // items: [{src, caption}]
  let i = index;
  const img = el('img', { alt: '' });
  const cap = el('div', { class: 'lb-cap' });
  const show = () => { img.src = items[i].src; cap.textContent = `${items[i].caption || ''}   ${i + 1} / ${items.length}`; };
  const step = (d) => { i = (i + d + items.length) % items.length; show(); };
  const close = () => { box.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => {
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowRight') step(1);
  };
  const box = el('div', { class: 'lightbox', onclick: (e) => { if (e.target === box) close(); } },
    img, cap,
    items.length > 1 ? el('button', { class: 'lb-nav lb-prev', onclick: () => step(-1) }, '‹') : null,
    items.length > 1 ? el('button', { class: 'lb-nav lb-next', onclick: () => step(1) }, '›') : null,
    el('button', { class: 'lb-close', onclick: close }, '×'));
  document.body.append(box);
  document.addEventListener('keydown', onKey);
  show();
}

// ───────── 上传框 ─────────
// 点击选择 / 拖入 / 获得焦点后 Ctrl+V 粘贴。上传后以内容哈希保存。
export function dropzone(container, { max = 1, get, set, label = '上传', isDisabled = () => false, onChange }) {
  container.tabIndex = 0;
  const input = el('input', { type: 'file', accept: 'image/*', multiple: max > 1, hidden: true });
  let loading = 0;

  async function addFiles(files) {
    if (isDisabled()) return;
    const list = [...files].filter(f => f.type.startsWith('image/'));
    if (!list.length) return;
    loading++;
    render();
    try {
      for (const f of list) {
        const h = await uploadRef(f);
        let cur = get().slice();
        if (max === 1) cur = [h];
        else if (cur.length < max) { if (!cur.includes(h)) cur.push(h); }
        else { toast(`最多 ${max} 张`, 'warn'); break; }
        set(cur);
      }
    } catch (e) {
      toastError(e);
    } finally {
      loading--;
      render();
      onChange && onChange();
    }
  }

  function render() {
    const hashes = get();
    container.classList.toggle('disabled', isDisabled());
    fill(container, 
      ...hashes.map((h) => {
        const img = el('img', { src: `/api/files/ref/${h}?thumb=1`, alt: '' });
        const box = el('div', { class: 'dz-thumb' }, img,
          el('button', { class: 'x', title: '移除', onclick: (e) => { e.stopPropagation(); set(get().filter(x => x !== h)); render(); onChange && onChange(); } }, '×'));
        img.onerror = () => { box.classList.add('missing'); img.remove(); box.prepend('已丢失'); };
        return box;
      }),
      hashes.length < max || max === 1
        ? el('div', { class: `dz-add${loading ? ' loading' : ''}`, onclick: () => { container.focus(); input.click(); } },
            el('b', {}, loading ? '◌' : '+'), el('span', {}, hashes.length && max === 1 ? '替换' : label))
        : null,
      input);
  }

  input.addEventListener('change', () => { addFiles(input.files); input.value = ''; });
  container.addEventListener('dragover', (e) => { e.preventDefault(); container.classList.add('drag'); });
  container.addEventListener('dragleave', () => container.classList.remove('drag'));
  container.addEventListener('drop', (e) => {
    e.preventDefault();
    container.classList.remove('drag');
    addFiles(e.dataTransfer.files);
  });
  container.addEventListener('paste', (e) => {
    const files = [...(e.clipboardData?.items || [])].filter(i => i.kind === 'file').map(i => i.getAsFile());
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  render();
  return { render };
}

export function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtBytes(n) {
  if (n == null) return '—';
  return (n / 1024 ** 3).toFixed(1) + ' GB';
}

export function fmtDuration(s) {
  if (s == null) return '—';
  s = Math.floor(s);
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('已复制', 'ok', 1500); }
  catch { toast('复制失败', 'error'); }
}

// 用子节点替换内容；忽略 null / undefined / false（原生 replaceChildren 会把它们显示成文字）
export function fill(node, ...kids) {
  node.replaceChildren(...kids.flat().filter(k => k !== null && k !== undefined && k !== false));
}
