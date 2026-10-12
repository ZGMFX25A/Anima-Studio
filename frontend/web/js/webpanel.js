// 菜单 → 联网：联网方式、Danbooru 测试、DanbooruSearch Key / 参数 / 恢复默认 / 测试、清空标签参考缓存。
import { api } from './api.js';
import { $, el, fill, toast, toastError, confirmDialog } from './ui.js';

let s = null;
let draft = null;      // 页面上尚未保存的输入；重绘时保留，测试时直接使用
const busy = {};

export async function renderWeb() {
  try { s = await api.get('/api/web/settings'); } catch (e) { toastError(e); return; }
  if (!draft) resetDraft();
  draw();
}

function resetDraft() {
  const ts = s.tagsearch;
  draft = { proxy_mode: s.proxy_mode, proxy_url: s.proxy_url, api_key: '', client: ts.client,
    top_k: ts.top_k, limit: ts.limit, popularity_weight: ts.popularity_weight };
}

function dirty() {
  const ts = s.tagsearch;
  return draft.api_key !== '' || draft.proxy_mode !== s.proxy_mode || draft.proxy_url !== s.proxy_url
    || draft.client !== ts.client || String(draft.top_k) !== String(ts.top_k) || String(draft.limit) !== String(ts.limit)
    || String(draft.popularity_weight) !== String(ts.popularity_weight);
}

function draw() {
  const root = $('#panel-web');
  const ts = s.tagsearch;
  const bind = (node, k) => {
    node.value = draft[k] ?? '';
    node.addEventListener(node.tagName === 'SELECT' ? 'change' : 'input', () => { draft[k] = node.value; markDirty(); });
    return node;
  };
  const mode = bind(el('select', {},
    el('option', { value: 'direct' }, '直连（推荐）'),
    el('option', { value: 'system' }, '跟随系统代理（HTTPS_PROXY 等环境变量）'),
    el('option', { value: 'custom' }, '自定义代理')), 'proxy_mode');
  const proxy = bind(el('input', { type: 'text', placeholder: 'http://127.0.0.1:7890' }), 'proxy_url');
  const proxyWrap = el('label', { class: 'mini' }, el('span', {}, '代理地址'), proxy);
  const syncMode = () => { proxyWrap.hidden = mode.value !== 'custom'; };
  mode.addEventListener('change', syncMode);
  syncMode();

  const key = bind(el('input', { type: 'password', autocomplete: 'off',
    placeholder: ts.has_key ? `已保存 ${ts.api_key_masked}（留空不修改）` : '不填 = 使用公共池' }), 'api_key');
  const clientName = bind(el('input', { type: 'text', placeholder: '申请 Key 时登记的 Client 名' }), 'client');
  const topk = bind(el('input', { type: 'number', min: 1, max: 200 }), 'top_k');
  const limit = bind(el('input', { type: 'number', min: 1, max: 200 }), 'limit');
  const pop = bind(el('input', { type: 'number', min: 0, max: 1, step: 0.05 }), 'popularity_weight');
  const dirtyMark = el('span', { class: 'small', style: { color: 'var(--warn)' } });
  const markDirty = () => { dirtyMark.textContent = dirty() ? '有未保存的修改' : ''; };
  markDirty();

  const save = async () => {
    try {
      s = await api.put('/api/web/settings', {
        proxy_mode: draft.proxy_mode, proxy_url: draft.proxy_url,
        tagsearch: { api_key: draft.api_key, client: draft.client, top_k: draft.top_k, limit: draft.limit,
          popularity_weight: draft.popularity_weight },
      });
      resetDraft();
      toast(s.tagsearch.has_key ? '已保存，DanbooruSearch 使用个人额度' : '已保存', 'ok');
      draw();
    } catch (e) { toastError(e); }
  };
  const reset = async () => {
    if (!await confirmDialog('恢复默认？', '清空 DanbooruSearch 的 Key 与 Client 名，回到公共池；召回参数也还原为默认值。', '恢复默认')) return;
    try { s = await api.post('/api/web/settings/reset-tagsearch'); resetDraft(); toast('已恢复默认（公共池）', 'ok'); draw(); }
    catch (e) { toastError(e); }
  };
  const test = (target) => async () => {
    busy[target] = true;
    draw();
    try {
      // 用当前输入（含未保存的 Key / Client / 联网方式）测试，不写入设置
      const r = await api.post(`/api/web/test/${target}`, {
        proxy_mode: draft.proxy_mode, proxy_url: draft.proxy_url, api_key: draft.api_key, client: draft.client });
      busy[target + '_res'] = target === 'danbooru'
        ? { ok: true, text: `连接正常（${r.elapsed}s）` }
        : { ok: true, text: `连接正常（${r.elapsed}s）· ${r.pool === 'personal' ? '个人额度' : '公共池'}${r.remaining != null ? ` · 今日剩余 ${r.remaining} 点` : ''}${dirty() ? ' · 测试用的是未保存的输入，记得保存' : ''}` };
    } catch (e) {
      busy[target + '_res'] = { ok: false, text: e.message };
    } finally { busy[target] = false; draw(); }
  };
  const res = (t) => busy[t + '_res'] ? el('span', { class: `small ${busy[t + '_res'].ok ? 'ok-text' : 'bad-text'}` }, busy[t + '_res'].text) : null;
  const testBtn = (t, label) => el('button', { class: `btn small${busy[t] ? ' busy' : ''}`, disabled: !!busy[t], onclick: test(t) }, label);

  fill(root,
    el('div', { class: 'panel-title' }, el('h2', {}, '联网'), el('span', { class: 'grow' }), dirtyMark,
      el('button', { class: 'btn accent small', onclick: save }, '保存')),
    el('div', { class: 'svc' },
      el('div', { class: 'svc-head' }, el('h3', {}, '联网方式')),
      el('div', { class: 'hint' }, '用于访问 Danbooru 与 DanbooruSearch（LLM 请求不受影响）。本机的系统代理会导致 Danbooru 连接失败，默认直连。'),
      el('div', { class: 'grid2' }, el('label', { class: 'mini' }, el('span', {}, '方式'), mode), proxyWrap)),
    el('div', { class: 'svc' },
      el('div', { class: 'svc-head' }, el('h3', {}, 'Danbooru'),
        el('span', { class: 'muted small' }, '联网检索角色：标准角色名、wiki 简介、共现外貌标签'),
        el('div', { class: 'svc-btns' }, res('danbooru'), testBtn('danbooru', '测试连接')))),
    el('div', { class: 'svc' },
      el('div', { class: 'svc-head' }, el('h3', {}, 'DanbooruSearch'),
        el('span', { class: `badge ${ts.has_key ? 'ok' : ''}` }, ts.has_key ? '个人额度' : '公共池'),
        el('div', { class: 'svc-btns' }, res('tagsearch'), testBtn('tagsearch', '测试（消耗 3 点）'),
          el('button', { class: 'btn small ghost', onclick: reset }, '恢复默认'))),
      el('div', { class: 'hint' },
        '标签参考：按描述语义检索标准标签，每次 3 点（相同描述走缓存不扣点）。不填 Key 使用所有人共享的公共池（每日 15000 点）；',
        '用 Hugging Face 账号在 ',
        el('a', { href: 'https://sakizuki-danboorusearch.hf.space/developer/apply', target: '_blank', rel: 'noreferrer' }, '申请页'),
        ' 申请个人 Key（自动批准，每日 3000 点），填入 Key 与登记的 Client 名后保存即切换为个人额度。'),
      el('div', { class: 'grid2' },
        el('label', { class: 'mini' }, el('span', {}, 'API Key'), key),
        el('label', { class: 'mini' }, el('span', {}, 'Client 名'), clientName)),
      el('div', { class: 'grid3 eq' },
        el('label', { class: 'mini' }, el('span', {}, 'Top K（每个分词的候选数）'), topk),
        el('label', { class: 'mini' }, el('span', {}, '结果上限'), limit),
        el('label', { class: 'mini' }, el('span', {}, '热度权重（0–1）'), pop))),
    el('div', { class: 'svc' },
      el('div', { class: 'svc-head' }, el('h3', {}, '缓存'),
        el('span', { class: 'muted small' }, '角色库请在「角色库」页面管理'),
        el('div', { class: 'svc-btns' }, el('button', {
          class: 'btn small ghost', onclick: async () => {
            if (!await confirmDialog('清空标签参考缓存？', '之后相同的描述会重新请求 DanbooruSearch（消耗额度）。', '清空')) return;
            try { const r = await api.post('/api/web/tagsearch-cache/clear'); toast(`已清空 ${r.removed} 条`, 'ok'); }
            catch (e) { toastError(e); }
          },
        }, '清空标签参考缓存')))));
}
