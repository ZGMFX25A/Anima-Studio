// 绘图页「从图片读取」：拖入 / 选择 / 粘贴图片，读取其中的生成参数填进表单（DESIGN.md §14）。
import { api, fileToDataURL, errorText } from './api.js';
import { $, el, modal, toast } from './ui.js';
import { app, saveForm, syncAll, emit, FIELD_NAMES, modelInfo } from './store.js';
import { applyRecord } from './records.js';
import { currentLlm } from './llm.js';

const SEED_KEY = 'anima.import.keepSeed';
const MODE_KEY = 'anima.import.splitMode';

const lsGet = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* 忽略 */ } };

export function initImgRead() {
  const zone = $('#import-drop');
  const input = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp', hidden: true });
  zone.after(input);
  zone.addEventListener('click', () => input.click());
  zone.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
  input.addEventListener('change', () => { if (input.files[0]) readFile(input.files[0]); input.value = ''; });
  zone.addEventListener('paste', (e) => {
    const f = [...(e.clipboardData?.items || [])].find(i => i.kind === 'file')?.getAsFile();
    if (f) { e.preventDefault(); readFile(f); }
  });

  // 整个参数栏都能拖入；落在参考图 / 灵感图上传框里的由它们自己处理（它们会 preventDefault）
  const params = $('#params');
  const hasFile = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  params.addEventListener('dragover', (e) => {
    if (!hasFile(e)) return;
    e.preventDefault();
    zone.classList.toggle('drag', !e.target.closest('.dz-wrap'));
  });
  params.addEventListener('dragleave', (e) => { if (!params.contains(e.relatedTarget)) zone.classList.remove('drag'); });
  params.addEventListener('drop', (e) => {
    zone.classList.remove('drag');
    if (e.defaultPrevented || !hasFile(e)) return;
    e.preventDefault();
    const f = [...e.dataTransfer.files].find(x => x.type.startsWith('image/'));
    if (f) readFile(f);
  });
}

async function readFile(file) {
  const zone = $('#import-drop');
  zone.classList.add('loading');
  try {
    const res = await api.post('/api/image-meta/read', { data: await fileToDataURL(file) });
    showDialog(res, file.name);
  } catch (e) {
    toast(e.code === 'NO_METADATA' ? e.message : `读取失败：${errorText(e)}`, 'warn', 7000);
  } finally {
    zone.classList.remove('loading');
  }
}

// ───────────────────────────── 弹窗 ─────────────────────────────
function showDialog(res, filename) {
  const anima = res.source === 'anima';
  const rec = res.record;
  const hasFields = anima && (rec || res.form);
  const p = res.params || {};
  const seed = rec ? rec.seed : res.seed;

  const block = (title, text) => el('div', { class: 'pblock' },
    el('div', { class: 'pblock-head' }, title), el('div', { class: 'pbox import-text' }, text || '（空）'));
  const notes = [];
  // 种子：本程序的图和其它工具的图（A1111 的 Seed、ComfyUI 采样节点的 seed、NovelAI 的 seed）都能沿用。
  // 默认勾上；手动取消过一次就记住（'0'），旧版本记下的 '' 也算取消过
  let keepSeed = null;
  if (seed != null) {
    keepSeed = el('input', { type: 'checkbox', checked: lsGet(SEED_KEY) === null || lsGet(SEED_KEY) === '1' });
    keepSeed.addEventListener('change', () => lsSet(SEED_KEY, keepSeed.checked ? '1' : '0'));
  }
  if (res.seed_out_of_range) {
    notes.push(el('div', { class: 'hint warn' }, `原图的种子 ${res.seed_out_of_range} 超出本程序能用的范围（最大 2^53−1），不能沿用。`));
  }
  if (anima) {
    if (rec) notes.push(el('div', { class: 'hint' }, `本机图库里有这张图的记录（#${rec.id}）：会像「复用设置」一样恢复全部设置，包括人物 / 动作参考图。`));
    else {
      const f = res.form || {};
      const used = [];
      if (f.character_ref?.enabled || (!res.form && p.character_ref)) used.push(f.character_ref?.images ? `人物参考（${f.character_ref.images} 张）` : '人物参考');
      if (f.pose_ref?.enabled || (!res.form && p.pose_ref)) used.push('动作参考');
      if (used.length) notes.push(el('div', { class: 'hint warn' }, `原图使用了${used.join('、')}，但参考图不在图片里（本机图库也没有这张图）：读取后这些开关会关闭，需要复现请自己上传参考图再打开。`));
      if (p.model && !modelInfo(p.model)) notes.push(el('div', { class: 'hint warn' }, `本机没有原图的模型 ${p.model}，会改用默认模型。`));
      if (!res.form) notes.push(el('div', { class: 'hint' }, '这是旧版本生成的图，只记录了整段提示词：会按下面选的方式拆到各字段，模型与采样参数照原图恢复。'));
    }
  } else {
    notes.push(el('div', { class: 'hint' }, `其它工具生成的图：读取提示词，拆到各字段后覆盖当前字段${seed != null ? '，种子可选沿用' : ''}；模型、采样参数和画幅保持不变（原图的模型与本程序不同，采样参数不通用）。`));
  }

  // 拆分方式（没有字段原值时）
  let mode = null;
  const llm = currentLlm();
  if (!hasFields) {
    const saved = lsGet(MODE_KEY);
    mode = llm && saved !== 'rule' ? 'ai' : 'rule';
    const seg = el('span', { class: 'seg small' },
      el('button', { 'data-v': 'ai', disabled: !llm, title: llm ? '' : '没有可用的 LLM（菜单 → LLM 中勾选模型）' }, llm ? `AI 拆分（${llm.label}）` : 'AI 拆分（未配置 LLM）'),
      el('button', { 'data-v': 'rule', title: '不用 LLM，按常见写法快速拆分，可能不够准' }, '按规则拆分'));
    const render = () => [...seg.children].forEach(b => b.classList.toggle('on', b.dataset.v === mode));
    seg.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b || b.disabled) return;
      mode = b.dataset.v;
      lsSet(MODE_KEY, mode);
      render();
    });
    render();
    notes.push(el('div', { class: 'import-opt' }, el('span', {}, '拆分方式'), seg,
      el('small', { class: 'muted' }, 'AI 只给标签分类、不改写；规则粗分按质量词 / 人数 / @画师 / 长句归类，其余放进 appearance')));
  }

  const paramLine = anima ? `model ${p.model ?? '—'}   steps ${p.steps ?? '—'}   cfg ${p.cfg ?? '—'}   seed ${seed ?? '—'}\n` +
    `sampler ${p.sampler_name ?? '—'}   scheduler ${p.scheduler ?? '—'}   size ${p.width ?? '—'}×${p.height ?? '—'}`
    : Object.entries(res.info || {}).map(([k, v]) => `${k}: ${v}`).join('\n');

  let controller = null;
  let requestId = '';
  const status = el('span', { class: 'muted small' });
  modal({
    title: '从图片读取', wide: true,
    body: el('div', {},
      el('div', { class: 'import-src' }, `来源：${res.source_label}`, el('span', { class: 'muted' }, `  ${filename}`)),
      block('正向提示词', res.positive),
      block('负向提示词', res.negative),
      paramLine ? block(anima ? '参数' : seed != null ? '原图参数（只读取种子）' : '原图参数（不会读取）', paramLine) : null,
      ...notes,
      keepSeed ? el('label', { class: 'import-opt check' }, keepSeed,
        el('span', {}, `${anima ? '保持相同种子' : '使用原图的种子'}（${seed}）`),
        el('small', { class: 'muted' }, anima ? '不勾选则种子设为随机' : '勾选后锁定为这个种子；不勾选则种子保持当前设置')) : null,
      status),
    onClose: () => {
      if (controller) {
        controller.abort();
        api.post('/api/llm/cancel', { request_id: requestId }).catch(() => {});
      }
    },
    actions: [
      { label: '取消', kind: 'ghost' },
      {
        label: '应用', kind: 'accent', onClick: async () => {
          if (controller) return true;
          const withSeed = !!keepSeed?.checked;
          if (rec) { applyRecord(rec, withSeed, '已读取图片的全部设置'); return; }
          let fields = res.form?.fields || null;
          if (!fields) {
            requestId = Math.random().toString(36).slice(2) + Date.now().toString(36);
            controller = new AbortController();
            const t0 = Date.now();
            const tick = setInterval(() => { status.textContent = `${mode === 'ai' ? 'AI 拆分' : '拆分'}中… ${Math.round((Date.now() - t0) / 1000)}s`; }, 300);
            try {
              const out = await api.post('/api/image-meta/split', {
                mode, positive: res.positive, negative: res.negative, request_id: requestId,
                ...(mode === 'ai' ? { provider_id: llm.provider_id, model_id: llm.model_id, level: llm.level } : {}),
              }, { signal: controller.signal });
              fields = out.fields;
              for (const w of out.warnings || []) toast(w, 'warn', 7000);
            } catch (e) {
              if (e.name !== 'AbortError') { status.textContent = ''; toast(`拆分失败：${errorText(e)}`, 'error', 8000); }
              return true;      // 失败时保留弹窗，可以换规则粗分再试
            } finally {
              clearInterval(tick);
              controller = null;
              if (mode === 'ai') emit('llm-models-changed');   // 思考档位可能被自动改掉
            }
          }
          if (anima) applyAnima(res, fields, withSeed);
          else applyFields(fields, res.source_label, withSeed ? seed : null);
        },
      },
    ],
  });
}

// 本程序生成、但本机图库里没有记录：字段 + 模型与采样参数 + 画幅，参考开关关闭（参考图不在图片里）
function applyAnima(res, fields, withSeed) {
  const p = res.params || {};
  const f = res.form || {};
  const presets = app.boot.defaults.aspect_presets;
  let ar = f.aspect_ratio;
  if (!ar) ar = Object.keys(presets).find(k => presets[k][0] === p.width && presets[k][1] === p.height) || 'custom';
  const form = {
    model: p.model, fields,
    aspect_ratio: ar, width: f.width ?? p.width, height: f.height ?? p.height,
    sampler: res.form ? (f.sampler || '') : (p.sampler_name || ''),
    scheduler: res.form ? (f.scheduler || '') : (p.scheduler || ''),
    character_ref: { ...(f.character_ref || {}), enabled: false, images: [] },
    pose_ref: { ...(f.pose_ref || {}), enabled: false, image: null },
    llm: { description: app.form.llm.description, images: app.form.llm.images, persona: app.form.llm.persona },
  };
  applyRecord({ form, steps: p.steps, cfg: p.cfg, seed: p.seed }, withSeed, '已读取图片的设置');
}

// 其它来源：覆盖提示词字段；seed 不为空时锁定为原图的种子（否则种子不动）
function applyFields(fields, label, seed) {
  for (const k of FIELD_NAMES) app.form.fields[k] = fields[k] ?? '';
  if (seed != null) { app.form.seed_mode = 'locked'; app.form.seed = seed; }
  saveForm();
  syncAll();
  emit('change', 'fields');
  for (const k of FIELD_NAMES) {
    const node = document.querySelector(`[data-field="${k}"]`);
    if (node) { node.classList.remove('flash'); void node.offsetWidth; node.classList.add('flash'); }
  }
  if (seed != null) emit('form-replaced');      // 种子栏跟着刷新
  toast(seed != null ? `已读取 ${label} 的提示词并锁定种子 ${seed}` : `已读取 ${label} 的提示词`, 'ok');
}
