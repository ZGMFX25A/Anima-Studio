// 绘图表单状态：默认值、本地保存、双向绑定、事件。

export const FIELD_NAMES = ['quality_meta_year_safe', 'count', 'character', 'series', 'artist',
  'appearance', 'action', 'nltags', 'neg'];

export const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));
export const on = (name, fn) => bus.addEventListener(name, (e) => fn(e.detail));

const KEY = 'anima.form.v1';

export const app = {
  boot: null,          // /api/bootstrap 结果
  form: null,
  lastSeed: null,
};

export function modelInfo(id = app.form?.model) {
  return app.boot?.capabilities.models.find(m => m.id === id) || null;
}

export function defaultForm() {
  const caps = app.boot.capabilities;
  const d = app.boot.defaults;
  const model = modelInfo(caps.default_model) || caps.models[0];
  const [w, h] = d.aspect_presets[d.aspect_ratio] || [1024, 1024];
  return {
    model: model?.id || '',
    steps: model?.defaults.steps ?? 30,
    cfg: model?.defaults.cfg ?? 4,
    fields: { ...d.fields },
    aspect_ratio: d.aspect_ratio,
    width: w, height: h,
    seed_mode: 'random', seed: null,
    sampler: '', scheduler: '',
    character_ref: { enabled: false, images: [], strength: 1.0, start_percent: 0.0, end_percent: 1.0 },
    pose_ref: { enabled: false, image: null, input_type: 'photo', strength: 1.0, fit: 'person', margin: 0.05,
      persons: 'largest', hands: true, face: true, feet: true },
    llm: { description: '', images: [], model_key: '', level: '', vision_key: '', safety: '', web_character: false,
      tag_reference: false, persona_match: true, persona: null },   // vision_key：识图模型，'' = 同上；persona：在绘图页选用的人设 {id, outfit_id, name, outfit_name}
  };
}

function merge(base, saved) {
  if (!saved || typeof saved !== 'object') return base;
  const out = Array.isArray(base) ? [...base] : { ...base };
  for (const k of Object.keys(base)) {
    if (!(k in saved)) continue;
    const b = base[k], s = saved[k];
    out[k] = (b && typeof b === 'object' && !Array.isArray(b)) ? merge(b, s) : s;
  }
  return out;
}

export function loadForm() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { /* 忽略 */ }
  app.form = merge(defaultForm(), saved);
  if (!modelInfo(app.form.model)) app.form.model = defaultForm().model;
}

let saveTimer = null;
export function saveForm() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try { localStorage.setItem(KEY, JSON.stringify(app.form)); } catch { /* 忽略 */ }
  }, 300);
}

export function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
export function setPath(obj, path, value) {
  const ks = path.split('.');
  let o = obj;
  for (const k of ks.slice(0, -1)) o = o[k];
  o[ks.at(-1)] = value;
}

// ───────── data-bind 双向绑定 ─────────
function readEl(elm) {
  if (elm.type === 'checkbox') return elm.checked;
  const t = elm.dataset.type;
  if (t === 'int') return elm.value === '' ? null : parseInt(elm.value, 10);
  if (t === 'float') return elm.value === '' ? null : parseFloat(elm.value);
  return elm.value;
}

export function bindAll(root = document) {
  for (const elm of root.querySelectorAll('[data-bind]')) {
    const evt = (elm.type === 'checkbox' || elm.tagName === 'SELECT') ? 'change' : 'input';
    elm.addEventListener(evt, () => {
      const path = elm.dataset.bind;
      setPath(app.form, path, readEl(elm));
      saveForm();
      emit('change', path);
    });
  }
}

export function syncAll(root = document) {
  for (const elm of root.querySelectorAll('[data-bind]')) {
    const v = getPath(app.form, elm.dataset.bind);
    if (elm.type === 'checkbox') elm.checked = !!v;
    else if (elm.tagName === 'SELECT') {
      if ([...elm.options].some(o => o.value === String(v ?? ''))) elm.value = String(v ?? '');
    } else if (document.activeElement !== elm || elm.value !== String(v ?? '')) {
      elm.value = v ?? '';
    }
  }
}

export function setField(path, value) {
  setPath(app.form, path, value);
  saveForm();
  emit('change', path);
}

export function refHashesInUse() {
  const f = app.form;
  return [...f.character_ref.images, f.pose_ref.image, ...f.llm.images].filter(Boolean);
}
