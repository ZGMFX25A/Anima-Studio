// 历史页 / 收藏页。
import { api } from './api.js';
import { $, el, fill, toast, toastError, confirmDialog, lightbox, fmtTime } from './ui.js';
import { on, emit, refHashesInUse } from './store.js';
import { showPromptModal, reuseDialog } from './records.js';

const PAGE = 40;

function createGallery(kind) {
  const favorite = kind === 'favorites';
  const grid = $(`#${kind}-grid`);
  const foot = $(`#${kind}-foot`);
  const count = $(`#${kind}-count`);
  const st = { items: [], total: 0, loading: false, stale: true };

  const observer = new IntersectionObserver((ents) => {
    if (ents.some(e => e.isIntersecting)) loadMore();
  }, { root: $(`#page-${kind}`), rootMargin: '400px' });
  observer.observe(foot);

  async function reload() {
    st.items = [];
    st.total = 0;
    st.stale = false;
    fill(grid);
    await loadMore();
  }

  async function loadMore() {
    if (st.loading || (st.items.length && st.items.length >= st.total)) return;
    st.loading = true;
    foot.textContent = '加载中…';
    try {
      const d = await api.get(`/api/gallery?favorite=${favorite ? 1 : 0}&offset=${st.items.length}&limit=${PAGE}`);
      st.total = d.total;
      st.items.push(...d.items);
      grid.append(...d.items.map(card));
      renderMeta();
    } catch (e) {
      foot.textContent = '';
      toastError(e);
    } finally {
      st.loading = false;
    }
  }

  function renderMeta() {
    count.textContent = st.total ? `共 ${st.total} 张` : '';
    if (!st.total) {
      fill(grid, el('div', { class: 'empty-state' },
        favorite ? '还没有收藏。在历史或绘图页点 ☆ 即可收藏。' : '还没有生成记录。'));
      foot.textContent = '';
    } else {
      foot.textContent = st.items.length >= st.total ? '— 没有更多了 —' : '';
    }
  }

  function card(it) {
    const favBtn = el('button', { class: it.favorite ? 'fav-on' : '', title: '收藏 / 取消收藏' }, it.favorite ? '★' : '☆');
    const thumb = el('div', { class: 'gthumb', onclick: () => openLightbox(it) },
      it.exists ? el('img', { src: `/api/files/thumb/${it.id}`, loading: 'lazy', alt: '' }) : el('div', { class: 'lost' }, '文件已丢失'),
      it.favorite && !favorite ? el('span', { class: 'favmark' }, '★') : null);
    const node = el('div', { class: 'gcard', 'data-id': it.id },
      thumb,
      el('div', { class: 'gmeta', title: `seed ${it.seed}` }, el('span', {}, fmtTime(it.created_at)), el('span', {}, it.model)),
      el('div', { class: 'gbtns' },
        el('button', { onclick: () => showPromptModal(it) }, '提示词'),
        favBtn,
        el('button', { class: 'del', onclick: () => remove(it) }, '删除'),
        el('button', { onclick: () => reuseDialog(it) }, '复用')));
    favBtn.addEventListener('click', async () => {
      try {
        const r = await api.post(`/api/gallery/${it.id}/favorite`);
        it.favorite = r.favorite;
        emit('gallery-changed', { id: it.id, favorite: r.favorite, from: kind });
        toast(r.favorite ? '已收藏' : '已取消收藏', 'ok', 1500);
      } catch (e) { toastError(e); }
    });
    return node;
  }

  function openLightbox(it) {
    const list = st.items.filter(x => x.exists);
    const idx = Math.max(0, list.findIndex(x => x.id === it.id));
    lightbox(list.map(x => ({ src: `/api/files/image/${x.id}`, caption: `${fmtTime(x.created_at)}  ·  ${x.model}  ·  seed ${x.seed}` })), idx);
  }

  async function remove(it) {
    const ok = await confirmDialog('永久删除这张图片？', `原文件 ${it.filename} 将从磁盘删除，无法恢复。`, '永久删除', true);
    if (!ok) return;
    try {
      await api.post(`/api/gallery/${it.id}/delete`, { keep: refHashesInUse() });
      emit('gallery-changed', { id: it.id, deleted: true });
      toast('已删除', 'ok', 1500);
    } catch (e) { toastError(e); }
  }

  // 其它页面的改动同步到本页
  on('gallery-changed', ({ id, favorite: fav, deleted }) => {
    const idx = st.items.findIndex(x => x.id === id);
    if (deleted || (favorite && fav === false)) {
      if (idx >= 0) {
        st.items.splice(idx, 1);
        st.total = Math.max(0, st.total - 1);
        grid.querySelector(`[data-id="${id}"]`)?.remove();
        renderMeta();
      }
      return;
    }
    if (idx >= 0) {
      st.items[idx].favorite = fav;
      grid.querySelector(`[data-id="${id}"]`)?.replaceWith(card(st.items[idx]));
    } else if (favorite && fav) {
      st.stale = true;
    }
  });
  on('gallery-new', () => { if (!favorite) st.stale = true; });

  return {
    show() { if (st.stale) reload(); },
  };
}

export function initGallery() {
  const pages = { history: createGallery('history'), favorites: createGallery('favorites') };
  on('tab', (tab) => pages[tab]?.show());
}
