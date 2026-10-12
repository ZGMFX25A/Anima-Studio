// 生成记录相关的弹窗：提示词显示、复用。
import { el, modal, copyText, toast } from './ui.js';
import { app, defaultForm, saveForm, emit, FIELD_NAMES, modelInfo } from './store.js';

const ZH = {
  quality_meta_year_safe: '质量/元数据/年份/安全', aspect_ratio: '画幅比例', count: '人数', character: '角色',
  series: '作品', artist: '画师', appearance: '外貌', action: '动作', nltags: '自然语言', neg: '负向提示词',
};

export function showPromptModal(rec) {
  const f = rec.form || {};
  const fields = f.fields || {};
  const block = (title, text) => el('div', { class: 'pblock' },
    el('div', { class: 'pblock-head' }, title,
      el('button', { class: 'btn ghost small', onclick: () => copyText(text || '') }, '复制')),
    el('div', { class: 'pbox' }, text || '（空）'));
  const rows = [...FIELD_NAMES.slice(0, -1), 'aspect_ratio'].map(k => {
    const v = k === 'aspect_ratio' ? (f.aspect_ratio === 'custom' ? `自定义 ${rec.width}×${rec.height}` : f.aspect_ratio) : fields[k];
    return el('tr', {}, el('td', {}, k, el('br'), el('small', {}, ZH[k])), el('td', {}, v || '—'));
  });
  const refs = [];
  if (f.character_ref?.enabled) refs.push(`人物参考 ${f.character_ref.images.length} 张（strength ${f.character_ref.strength}）`);
  if (f.pose_ref?.enabled) refs.push(`动作参考（${f.pose_ref.input_type === 'skeleton' ? '骨架图' : '照片'}，strength ${f.pose_ref.strength}）`);
  modal({
    title: '提示词与参数', wide: true,
    body: el('div', {},
      block('正向提示词', rec.positive),
      block('负向提示词', rec.negative),
      el('div', { class: 'pblock' }, el('div', { class: 'pblock-head' }, '参数'),
        el('div', { class: 'pbox' },
          `model ${rec.model}   steps ${rec.steps}   cfg ${rec.cfg}   seed ${rec.seed}\n` +
          `sampler ${rec.sampler}   scheduler ${rec.scheduler}   size ${rec.width}×${rec.height}` +
          (refs.length ? `\n${refs.join('；')}` : '') +
          (f.llm?.description ? `\nAI 描述：${f.llm.description}` : ''))),
      el('div', { class: 'pblock' }, el('div', { class: 'pblock-head' }, '各字段'), el('table', { class: 'ftable' }, rows))),
    actions: [{ label: '关闭', kind: 'ghost' }],
  });
}

export function reuseDialog(rec) {
  modal({
    title: '复用这张图的设置',
    body: '会恢复提示词各字段、模型与采样参数、画幅、参考开关与参考图，然后切换到绘图页。',
    stack: true,
    actions: [
      { label: '正常复用', sub: '种子设为随机', kind: '', onClick: () => applyRecord(rec, false) },
      { label: '连同种子复用', sub: `锁定种子 ${rec.seed}，可复现这张图`, kind: 'accent', onClick: () => applyRecord(rec, true) },
      { label: '取消', kind: 'ghost' },
    ],
  });
}

export function applyRecord(rec, withSeed) {
  const base = defaultForm();
  const f = structuredClone(rec.form || {});
  const keepLlm = app.form.llm;           // 保留当前 LLM 选择，只恢复描述与灵感图
  const merged = {
    ...base, ...f,
    fields: { ...base.fields, ...(f.fields || {}) },
    character_ref: { ...base.character_ref, ...(f.character_ref || {}) },
    pose_ref: { ...base.pose_ref, ...(f.pose_ref || {}) },
    llm: { ...keepLlm, description: f.llm?.description || '', images: f.llm?.images || [], persona: f.llm?.persona || null },
  };
  delete merged.persona;                  // 人设预览的记录复用到绘图页时，不能再被当成人设预览
  if (!modelInfo(merged.model)) merged.model = base.model;
  // 采样参数以实际使用的值为准
  if (rec.steps != null) merged.steps = rec.steps;
  if (rec.cfg != null) merged.cfg = rec.cfg;
  if (withSeed) { merged.seed_mode = 'locked'; merged.seed = rec.seed; }
  else { merged.seed_mode = 'random'; merged.seed = null; }
  app.form = merged;
  saveForm();
  emit('form-replaced');
  emit('goto', 'draw');
  toast(withSeed ? `已复用设置并锁定种子 ${rec.seed}` : '已复用设置', 'ok');
}
