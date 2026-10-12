// 菜单 → 存储：图片保存目录。
import { api } from './api.js';
import { $, el, fill, toast, toastError, confirmDialog } from './ui.js';

let s = null;
let draft = null;      // 尚未保存的输入，重绘时保留
let busy = '';

export async function renderStorage() {
  try { s = await api.get('/api/storage'); } catch (e) { toastError(e); return; }
  if (draft === null) draft = s.output_dir;
  draw();
}

function draw() {
  const root = $('#panel-storage');
  const input = el('input', { type: 'text', value: draft, placeholder: `留空 = 默认（${s.default}）` });
  input.addEventListener('input', () => { draft = input.value; mark(); });
  const dirty = el('span', { class: 'small', style: { color: 'var(--warn)' } });
  const mark = () => { dirty.textContent = draft.trim() !== s.output_dir ? '有未保存的修改' : ''; };
  mark();

  const save = async (value, force = false) => {
    busy = 'save';
    draw();
    try {
      s = await api.put(`/api/storage${force ? '?force=1' : ''}`, { output_dir: value });
      draft = s.output_dir;
      toast(`已保存：新图片将保存到 ${s.effective}`, 'ok', 5000);
    } catch (e) {
      if (e.code === 'BUSY' && !force) {
        busy = '';
        if (await confirmDialog('有任务正在生成', '保存后需要重启绘画模块，会取消正在生成和排队的任务，是否继续？', '继续', true)) return save(value, true);
      } else toastError(e);
    } finally { busy = ''; draw(); }
  };
  const browse = async () => {
    busy = 'browse';
    draw();
    try {
      const r = await api.post('/api/storage/browse');
      if (r.path) draft = r.path;
    } catch (e) { toastError(e); }
    finally { busy = ''; draw(); }
  };
  const btn = (label, key, onclick, cls = '') => el('button', {
    class: `btn small ${cls}${busy === key ? ' busy' : ''}`, disabled: !!busy, onclick }, label);

  fill(root,
    el('div', { class: 'panel-title' }, el('h2', {}, '存储'), el('span', { class: 'grow' }), dirty,
      btn('保存', 'save', () => save(draft), 'accent')),
    el('div', { class: 'svc' },
      el('div', { class: 'svc-head' }, el('h3', {}, '图片保存目录'),
        el('div', { class: 'svc-btns' }, btn('打开目录', 'open', () => api.post('/api/storage/open').catch(toastError), 'ghost'))),
      el('div', { class: 'kv' },
        el('div', {}, el('span', { class: 'k' }, '当前使用'), el('span', { class: 'v' }, s.effective)),
        el('div', {}, el('span', { class: 'k' }, '默认目录'), el('span', { class: 'v' }, s.default))),
      el('div', { class: 'seed-row' }, input,
        btn('选择文件夹…', 'browse', browse),
        btn('恢复默认', 'reset', () => { draft = ''; save(''); }, 'ghost')),
      el('div', { class: 'hint' },
        '新生成的图片保存到「目录\\日期\\」下。保存设置后会自动重启绘画模块使其生效（约几秒）。',
        el('br'), '已经生成的图片不会移动，历史和收藏照常可用。'),
      s.pending_restart ? el('div', { class: 'hint warn' },
        s.painter_external ? '绘画模块不是由本程序启动的，无法确认它正在使用的目录；点「保存」会重启它并使设置生效。'
          : '绘画模块仍在使用旧目录，点「保存」重启后生效。') : null));
}
