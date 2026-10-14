// 人设卡：导出（单张 PNG / 列表打包 zip）与导入（多选、拖入，支持导出的 zip 与 SillyTavern 角色卡）。
import { api, download, errorText, fileToDataURL } from './api.js';
import { el, toast, toastError, modal } from './ui.js';

// ═══════════════════════════ 导出 ═══════════════════════════
export async function exportCard(p) {
  try {
    const name = await download(`/api/personas/${p.id}/card`);
    toast(`已导出 ${name}${p.cover ? '' : '（没有封面，卡面是占位图）'}，保存在浏览器的下载目录`, 'ok', 5000);
  } catch (e) { toastError(e); }
}

export async function exportList({ q, kind, group }, n) {
  try {
    toast(`正在导出 ${n} 个人设…`, 'info', 2000);
    const name = await download(`/api/personas/export.zip?${new URLSearchParams({ q, kind, group })}`);
    toast(`已导出 ${name}（${n} 张人设卡），保存在浏览器的下载目录`, 'ok', 5000);
  } catch (e) { toastError(e); }
}

// ═══════════════════════════ 导入 ═══════════════════════════
const isZip = (f) => /\.zip$/i.test(f.name) || /zip/.test(f.type);

// 导入多张人设卡（重名时逐个询问），返回每张的结果：{file, status, persona, ...} 或 {file, error}
export async function importCards(files) {
  const cards = [];
  const results = [];
  for (const f of files) {
    if (!isZip(f)) { cards.push(f); continue; }
    try { cards.push(...await unzipPngs(f)); } catch (e) { results.push({ file: f.name, error: e.message }); }
  }
  if (cards.length > 1) toast(`正在导入 ${cards.length} 张人设卡…`, 'info', 2500);
  let policy = '';     // 「后面的重名都这样处理」
  for (let i = 0; i < cards.length; i++) {
    const f = cards[i];
    let data;
    try { data = await fileToDataURL(f); } catch { results.push({ file: f.name, error: '无法读取文件' }); continue; }
    let choice = policy;
    for (;;) {
      try {
        results.push({ file: f.name, ...await api.post('/api/personas/cards/import', { data, on_conflict: choice }) });
        break;
      } catch (e) {
        if (e.code === 'NAME_CONFLICT' && !choice && e.detail) {
          const ans = await askConflict(e.detail, cards.length - i - 1);
          choice = ans.action;
          if (ans.all) policy = ans.action;
          continue;
        }
        results.push({ file: f.name, error: errorText(e) });
        break;
      }
    }
  }
  report(results);
  return results;
}

function askConflict(info, remaining) {
  return new Promise((resolve) => {
    const all = el('input', { type: 'checkbox' });
    const body = el('div', { class: 'card-conflict' },
      el('p', {}, `人设卡${info.source === 'sillytavern' ? '（SillyTavern）' : ''}「${info.name}」与已有人设「${info.conflict.name}」的${info.conflict.by === 'name' ? '名称' : '别名'}重复。`),
      el('ul', {},
        el('li', {}, el('b', {}, '覆盖：'), `「${info.conflict.name}」的资料换成卡里的（卡里有封面时封面也换掉），已有的预览图保留`),
        el('li', {}, el('b', {}, '另存为新：'), `改名为「${info.suggested_name}」，原有人设不变`),
        el('li', {}, el('b', {}, '跳过：'), '不导入这张')),
      remaining ? el('label', { class: 'switch-row' }, all, el('span', { class: 'switch' }),
        el('span', {}, `后面再遇到重名也这样处理（还剩 ${remaining} 张）`)) : null);
    modal({
      title: '人设重名', body,
      onClose: (v) => resolve({ action: v || 'skip', all: !!v && all.checked }),
      actions: [
        { label: '跳过', kind: 'ghost', value: 'skip' },
        { label: '另存为新', value: 'rename' },
        { label: '覆盖', kind: 'danger', value: 'overwrite' },
      ],
    });
  });
}

const STATUS = { created: '新建', overwritten: '覆盖', skipped: '跳过' };

function detailLines(r) {
  const out = [];
  if (r.renamed_from) out.push(`与已有人设重名，已改名（原名「${r.renamed_from}」）`);
  if (r.dropped_aliases?.length) out.push(`去掉了与其它人设重复的别名：${r.dropped_aliases.join('、')}`);
  return [...out, ...(r.notes || [])];
}

function report(results) {
  if (!results.length) return;
  const count = (s) => results.filter(r => r.status === s).length;
  const failed = results.filter(r => r.error).length;
  if (results.length === 1) {
    const r = results[0];
    if (r.error) return toast(`${r.file}：${r.error}`, 'error', 8000);
    if (r.status === 'skipped') return toast(`已跳过「${r.name}」`, 'info');
    toast(`已导入人设「${r.persona.name}」（${STATUS[r.status]}）`, 'ok');
    const lines = detailLines(r);
    if (lines.length) toast(lines.join('；'), 'warn', 8000);
    return;
  }
  const summary = [`新建 ${count('created')}`, `覆盖 ${count('overwritten')}`, `跳过 ${count('skipped')}`,
    failed ? `失败 ${failed}` : ''].filter(Boolean).join(' · ');
  if (!failed && !results.some(r => detailLines(r).length)) return toast(`导入完成：${summary}`, 'ok');
  modal({
    title: '导入结果', wide: true,
    body: el('div', { class: 'card-report' },
      el('p', {}, summary),
      el('ul', {}, results.map(r => el('li', { class: r.error ? 'bad' : r.status === 'skipped' ? 'skip' : 'ok' },
        r.error ? [el('b', {}, r.file), `：${r.error}`]
          : r.status === 'skipped' ? [el('b', {}, r.name), '：已跳过']
            : [el('b', {}, r.persona.name), `：${STATUS[r.status]}`, ...detailLines(r).map(t => el('div', { class: 'muted small' }, t))])))),
    actions: [{ label: '知道了', kind: 'accent' }],
  });
}

// 导出的 zip 在页面里解开（只取 PNG；支持存储与 deflate 两种压缩方式）
async function unzipPngs(file) {
  const buf = new Uint8Array(await file.arrayBuffer());
  const dv = new DataView(buf.buffer);
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) {
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('无法读取的 zip 文件');
  const out = [];
  let p = dv.getUint32(eocd + 16, true);
  for (let k = dv.getUint16(eocd + 10, true); k > 0; k--) {
    if (dv.getUint32(p, true) !== 0x02014b50) throw new Error('zip 文件已损坏');
    const flags = dv.getUint16(p + 8, true), method = dv.getUint16(p + 10, true), size = dv.getUint32(p + 20, true);
    const nameLen = dv.getUint16(p + 28, true), skip = nameLen + dv.getUint16(p + 30, true) + dv.getUint16(p + 32, true);
    const local = dv.getUint32(p + 42, true);
    // 没有 UTF-8 标记的文件名多半是资源管理器按 GBK 写的
    const name = new TextDecoder(flags & 0x800 ? 'utf-8' : 'gbk').decode(buf.subarray(p + 46, p + 46 + nameLen));
    p += 46 + skip;
    if (!/\.png$/i.test(name) || (method !== 0 && method !== 8)) continue;
    const start = local + 30 + dv.getUint16(local + 26, true) + dv.getUint16(local + 28, true);
    let data = buf.subarray(start, start + size);
    if (method === 8) {
      data = new Uint8Array(await new Response(new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'))).arrayBuffer());
    }
    out.push(new File([data], name.split('/').pop(), { type: 'image/png' }));
  }
  if (!out.length) throw new Error('zip 里没有 PNG 图片');
  return out;
}

// 列表页：把人设卡拖进来导入
export function cardDropTarget(node, onFiles) {
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  const reset = () => { depth = 0; node.classList.remove('dropping'); };
  node.addEventListener('dragenter', (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; node.classList.add('dropping'); });
  node.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
  node.addEventListener('dragleave', () => { if (--depth <= 0) reset(); });
  node.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    reset();
    if (e.dataTransfer.files.length) onFiles([...e.dataTransfer.files]);
  });
}
