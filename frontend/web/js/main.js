// 入口：读取能力与默认值、初始化各页面、标签切换、顶栏状态。
import { api, setOomHint } from './api.js';
import { $, $$, toast } from './ui.js';
import { app, loadForm, on, emit } from './store.js';
import { initDraw } from './draw.js';
import { initGallery } from './gallery.js';
import { initMenu } from './menu.js';
import { initLlm } from './llm.js';
import { initImgRead } from './imgread.js';
import { initPersona } from './persona.js';

async function bootstrap() {
  for (;;) {
    try { return await api.get('/api/bootstrap'); }
    catch { await new Promise(r => setTimeout(r, 2000)); }
  }
}

function showTab(tab) {
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  $$('main > .page').forEach(p => p.classList.toggle('active', p.id === `page-${tab}`));
  try { localStorage.setItem('anima.tab', tab); } catch { /* 忽略 */ }
  emit('tab', tab);
}

// ───────── 主题：跟随系统 / 浅色 / 深色（顶栏右侧按钮，点一下换一种；index.html 里在样式加载前先设好） ─────────
const THEMES = [['system', '◐', '跟随系统'], ['light', '☀', '浅色'], ['dark', '☾', '深色']];
const systemLight = window.matchMedia('(prefers-color-scheme: light)');

function themeMode() {
  try { return localStorage.getItem('anima.theme') || 'system'; } catch { return 'system'; }
}

function applyTheme() {
  const mode = themeMode();
  const dark = mode === 'dark' || (mode === 'system' && !systemLight.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const [, icon, label] = THEMES.find(t => t[0] === mode) || THEMES[0];
  const btn = $('#theme-btn');
  btn.querySelector('.theme-icon').textContent = icon;
  btn.querySelector('.theme-label').textContent = label;
  btn.title = `外观：${label}${mode === 'system' ? `（当前系统为${dark ? '深色' : '浅色'}）` : ''}，点击切换`;
}

function initTheme() {
  applyTheme();
  systemLight.addEventListener('change', applyTheme);
  $('#theme-btn').addEventListener('click', () => {
    const i = THEMES.findIndex(t => t[0] === themeMode());
    const [next, , label] = THEMES[(i + 1) % THEMES.length];
    try { localStorage.setItem('anima.theme', next); } catch { /* 忽略 */ }
    applyTheme();
    toast(`外观：${label}`, 'ok', 1500);
  });
}

// ───────── 顶栏状态 ─────────
let statusTimer = null;
async function refreshStatus() {
  clearTimeout(statusTimer);
  try {
    const s = await api.get('/api/system/status');
    setOomHint(s.comfyui.gpu?.oom_hint);
    emit('status', s);
    const c = s.comfyui.state, p = s.painter.state;
    const dot = $('#status-dot');
    let text, cls;
    if (c === 'running' && p === 'running') {
      cls = 'ok';
      const n = (s.painter.queue || 0) + (s.painter.running_job ? 1 : 0);
      text = n ? `引擎就绪 · 任务 ${n}` : '引擎就绪';
    } else if (c === 'starting' || p === 'starting' || c === 'stopping') {
      cls = 'warn'; text = c === 'stopping' ? '引擎停止中' : '引擎启动中…';
    } else if (c === 'stopped') {
      cls = 'bad'; text = s.comfyui.blocked ? '引擎未启动 · 原因见 菜单 → 引擎' : '引擎已停止';
    } else if (p !== 'running') {
      cls = 'bad'; text = '绘画模块未运行';
    } else {
      cls = 'bad'; text = '引擎无响应';
    }
    dot.className = `dot ${cls}`;
    $('#status-text').textContent = text;
    if (cls === 'ok' && !app.boot.engine_ready) reloadCapabilities();
  } catch {
    $('#status-dot').className = 'dot bad';
    $('#status-text').textContent = '前端服务已断开';
  }
  statusTimer = setTimeout(refreshStatus, 5000);
}

async function reloadCapabilities() {
  try {
    const b = await api.get('/api/bootstrap');
    if (b.engine_ready) {
      app.boot = b;
      emit('boot-updated');
    }
  } catch { /* 忽略 */ }
}

async function main() {
  app.boot = await bootstrap();
  loadForm();
  initDraw();
  initLlm();
  initImgRead();
  initGallery();
  initMenu();
  initPersona();

  $('#tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b) showTab(b.dataset.tab);
  });
  $('#status-pill').addEventListener('click', () => { showTab('menu'); emit('goto-panel', 'engine'); });
  on('goto', showTab);
  on('refresh-status', refreshStatus);

  let tab = 'draw';
  try { tab = localStorage.getItem('anima.tab') || 'draw'; } catch { /* 忽略 */ }
  showTab(['draw', 'personas', 'history', 'favorites', 'menu'].includes(tab) ? tab : 'draw');
  refreshStatus();
  if (!app.boot.engine_ready) toast('引擎还在启动，采样器列表稍后自动加载', 'info');
}

initTheme();
main();
