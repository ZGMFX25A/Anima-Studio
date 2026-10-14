// 与前端服务通信。错误统一抛出 ApiError（message 为中文说明）。

const CODE_TEXT = {
  ENGINE_UNAVAILABLE: 'ComfyUI 引擎未启动',
  PAINTER_UNAVAILABLE: '绘画模块未启动',
  OUT_OF_MEMORY: '显存不足',
  NO_PERSON: '动作参考图中没有检测到人物',
  FEATURE_UNSUPPORTED: '该模型不支持所选参考',
  BAD_IMAGE: '图片无法识别',
  TIMEOUT: '生成超时',
  CANCELLED: '已取消',
  ENGINE_ERROR: '引擎执行出错',
  INVALID_REQUEST: '参数校验失败',
  UNKNOWN_MODEL: '模型不存在或未安装',
  STAGE_UNAVAILABLE: '该功能尚未安装',
  BUSY: '有任务正在执行',
};

export class ApiError extends Error {
  constructor(message, code, status, detail) {
    super(message);
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

export function errorText(e) {
  if (!e) return '';
  const base = CODE_TEXT[e.code];
  const msg = e.message || '';
  if (base && msg && !msg.includes(base)) return `${base}：${msg}`;
  return msg || base || '未知错误';
}

async function request(method, url, body, opts = {}) {
  let resp;
  try {
    resp = await fetch(url, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: opts.signal,
    });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError('无法连接前端服务，请确认启动窗口仍在运行', 'NETWORK', 0);
  }
  let data = null;
  const text = await resp.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) throw responseError(resp, data);
  return data;
}

function responseError(resp, data) {
  const err = (data && data.error) || {};
  let message = err.message || (typeof data === 'string' ? data : '') || `HTTP ${resp.status}`;
  if (err.detail && Array.isArray(err.detail)) {
    message += '：' + err.detail.map(d => `${(d.loc || []).slice(1).join('.')} ${d.msg}`).join('；');
  }
  return new ApiError(message, err.code || `HTTP_${resp.status}`, resp.status, err.detail);
}

// 下载文件（文件名取自 Content-Disposition），返回文件名；出错时与 api.* 一样抛 ApiError
export async function download(url) {
  let resp;
  try { resp = await fetch(url); } catch {
    throw new ApiError('无法连接前端服务，请确认启动窗口仍在运行', 'NETWORK', 0);
  }
  if (!resp.ok) {
    const text = await resp.text();
    let data = text;
    try { data = JSON.parse(text); } catch { /* 不是 JSON */ }
    throw responseError(resp, data);
  }
  const blob = await resp.blob();
  const cd = resp.headers.get('Content-Disposition') || '';
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd);
  const name = m ? decodeURIComponent(m[1]) : url.split('?')[0].split('/').pop();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 30000);
  return name;
}

export const api = {
  get: (url, opts) => request('GET', url, undefined, opts),
  post: (url, body = {}, opts) => request('POST', url, body, opts),
  put: (url, body = {}, opts) => request('PUT', url, body, opts),
  del: (url, opts) => request('DELETE', url, undefined, opts),
};

export function fileToDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

export async function uploadRef(file) {
  const data = await fileToDataURL(file);
  const res = await api.post('/api/refs', { data });
  return res.hash;
}
