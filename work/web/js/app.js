// app.js —— 启动、布局骨架、路由注册、主题与语言初始化

import { t, getLocale, setLocale } from './i18n.js';
import {
  state, setAuth, clearUser, isLoggedIn, getStoredTheme, setStoredTheme,
  getStoredLocale, setStoredLocale, emit
} from './store.js';
import * as api from './api.js';
import * as router from './router.js';
import { icon, raw, esc, toast, closeModals } from './ui.js';
import { startConfigWatch } from './config_sync.js';

import * as pageLogin from './pages/login.js';
import * as pageDashboard from './pages/dashboard.js';
import * as pageLibraries from './pages/libraries.js';
import * as pageLibraryEdit from './pages/library_edit.js';
import * as pageWorks from './pages/works.js';
import * as pageProcessors from './pages/processors.js';
import * as pageProcessorEdit from './pages/processor_edit.js';
import * as pageTasks from './pages/tasks.js';
import * as pageTaskDetail from './pages/task_detail.js';
import * as pageSettings from './pages/settings.js';

const appEl = document.getElementById('app');
appEl.__renderNotFound = function () {};

let currentHost = null;
let shellEls = null;
let stopConfigWatch = null;

// ---------------------------------------------------------------- 主题

const THEME_ORDER = ['system', 'light', 'dark'];
const THEME_ICON = { system: 'monitor', light: 'sun', dark: 'moon' };
const THEME_LABEL_KEY = { system: 'theme.system', light: 'theme.light', dark: 'theme.dark' };

export function applyTheme(theme) {
  const value = THEME_ORDER.indexOf(theme) >= 0 ? theme : 'system';
  document.documentElement.setAttribute('data-theme', value);
  state.theme = value;
  syncThemeButtons();
}

function syncThemeButtons() {
  document.querySelectorAll('[data-theme-value]').forEach(function (btn) {
    btn.setAttribute('aria-pressed', String(btn.dataset.themeValue === state.theme));
  });
}

function themeSwitchHtml() {
  const buttons = THEME_ORDER.map(function (value) {
    return '<button type="button" data-theme-value="' + value + '" aria-pressed="false" title="' +
      t(THEME_LABEL_KEY[value]) + '" aria-label="' + t('theme.label') + '：' + t(THEME_LABEL_KEY[value]) + '">' +
      icon(THEME_ICON[value], 14) + '<span class="theme-text">' + t(THEME_LABEL_KEY[value]) + '</span></button>';
  }).join('');
  return '<div class="theme-switch" role="group" aria-label="' + t('theme.label') + '">' + buttons + '</div>';
}

function bindThemeSwitch(root) {
  (root || document).querySelectorAll('[data-theme-value]').forEach(function (btn) {
    if (btn.__bound) return;
    btn.__bound = true;
    btn.addEventListener('click', function () {
      const value = btn.dataset.themeValue;
      setStoredTheme(value);
      applyTheme(value);
    });
  });
  syncThemeButtons();
}

// ---------------------------------------------------------------- 布局

const NAV_ITEMS = [
  { key: 'dashboard', href: '#/dashboard', icon: 'dashboard', label: 'nav.dashboard' },
  { key: 'libraries', href: '#/libraries', icon: 'library', label: 'nav.libraries' },
  { key: 'works', href: '#/works', icon: 'folder', label: 'nav.works' },
  { key: 'processors', href: '#/processors', icon: 'chip', label: 'nav.processors' },
  { key: 'tasks', href: '#/tasks', icon: 'tasks', label: 'nav.tasks' },
  { key: 'settings', href: '#/settings', icon: 'settings', label: 'nav.settings' }
];

function navHtml() {
  return NAV_ITEMS.map(function (item) {
    return '<a class="nav-link" href="' + item.href + '" data-nav="' + item.key + '">' +
      icon(item.icon, 16) + '<span>' + t(item.label) + '</span></a>';
  }).join('');
}

function buildShell() {
  appEl.innerHTML = '';
  const shell = document.createElement('div');
  shell.className = 'app-shell';
  shell.innerHTML =
    '<aside class="sidebar" id="sidebar">' +
      '<div class="brand">' +
        '<span class="brand-mark">' + icon('library', 17) + '</span>' +
        '<span class="brand-text">' +
          '<span class="brand-title">' + t('app.short') + '</span>' +
          '<span class="brand-sub">' + t('app.tagline') + '</span>' +
        '</span>' +
      '</div>' +
      '<nav class="nav" id="main-nav">' + navHtml() + '</nav>' +
      '<div class="sidebar-footer">' +
        '<div class="user-chip">' +
          '<span class="user-avatar">' + icon('logout', 0) + '</span>' +
          '<span class="user-name" id="user-name"></span>' +
        '</div>' +
        themeSwitchHtml() +
        '<button type="button" class="btn btn-secondary btn-sm btn-block" id="logout-btn" aria-label="' + esc(t('nav.logout')) + '">' +
          icon('logout', 14) + '<span>' + t('nav.logout') + '</span></button>' +
      '</div>' +
    '</aside>' +
    '<div class="sidebar-backdrop" id="nav-backdrop"></div>' +
    '<div class="main">' +
      '<header class="topbar">' +
        '<button type="button" class="icon-btn hamburger" id="nav-toggle" aria-label="' + esc(t('nav.menu')) + '" aria-expanded="false">' +
          icon('menu', 18) + '</button>' +
        '<div class="topbar-title" id="topbar-title">' + t('app.short') + '</div>' +
        '<div class="topbar-actions" id="topbar-actions">' + themeSwitchHtml() + '</div>' +
      '</header>' +
      '<div class="app-banner" id="app-cfg-banner" hidden></div>' +
      '<main class="page" id="page" tabindex="-1"></main>' +
    '</div>';
  appEl.appendChild(shell);

  const userNameEl = shell.querySelector('#user-name');
  const name = state.user && state.user.username ? state.user.username : '';
  userNameEl.textContent = name || t('common.not_set');
  const avatar = shell.querySelector('.user-avatar');
  avatar.innerHTML = '';
  avatar.textContent = name ? name.slice(0, 1).toUpperCase() : '?';

  const toggle = shell.querySelector('#nav-toggle');
  toggle.addEventListener('click', function () {
    const open = document.body.classList.toggle('nav-open');
    toggle.setAttribute('aria-expanded', String(open));
  });
  const backdrop = shell.querySelector('#nav-backdrop');
  backdrop.addEventListener('click', closeNav);
  shell.querySelectorAll('.nav-link').forEach(function (link) {
    link.addEventListener('click', closeNav);
  });
  shell.querySelector('#logout-btn').addEventListener('click', doLogout);
  bindThemeSwitch(shell);

  shellEls = { shell: shell, page: shell.querySelector('#page'), title: shell.querySelector('#topbar-title') };
  if (stopConfigWatch) stopConfigWatch();
  stopConfigWatch = startConfigWatch(renderConfigBanner);
  return shellEls.page;
}

/** 顶栏配置同步横幅：仅在「外部修改未对齐 / 导入报错」时显示。 */
function renderConfigBanner(info) {
  const el = document.getElementById('app-cfg-banner');
  if (!el) return;
  if (!info) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
  el.className = 'app-banner' + (info.danger ? ' app-banner-danger' : '');
  el.innerHTML = raw(icon('alert', 14)) +
    '<span class="app-banner-text">' + esc(info.title + '：' + info.message) + '</span>' +
    '<a class="btn btn-secondary btn-sm" href="#/settings">' + esc(t('cfg.details')) + '</a>';
}

function closeNav() {
  document.body.classList.remove('nav-open');
  const toggle = document.getElementById('nav-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', 'false');
}

function buildAuthLayout() {
  appEl.innerHTML = '';
  const host = document.createElement('div');
  host.id = 'auth-host';
  appEl.appendChild(host);
  shellEls = null;
  return host;
}

/** 返回页面宿主容器；必要时重建布局。 */
function ensureLayout(kind) {
  if (kind === 'auth') {
    if (currentHost && currentHost.id === 'auth-host' && document.body.contains(currentHost)) return currentHost;
    currentHost = buildAuthLayout();
    return currentHost;
  }
  if (currentHost && currentHost.id === 'page' && document.body.contains(currentHost)) return currentHost;
  currentHost = buildShell();
  return currentHost;
}

function setTopbarTitle(text) {
  if (shellEls && shellEls.title) shellEls.title.textContent = text;
}

function syncNav(routeName) {
  document.querySelectorAll('[data-nav]').forEach(function (link) {
    link.classList.toggle('active', link.dataset.nav === routeName);
  });
}

// ---------------------------------------------------------------- 登出

async function doLogout() {
  try {
    await api.authLogout();
  } catch (e) {
    /* 会话可能已过期，仍然清理本地状态 */
  }
  clearUser();
  toast(t('nav.logout_ok'), 'success');
  router.navigate('#/login', true);
}

// ---------------------------------------------------------------- 路由包装

function runPageCleanup(fn) {
  if (typeof fn !== 'function') return;
  try {
    fn();
  } catch (e) {
    console.error('[app] page cleanup error', e);
  }
}

// eslint-disable-next-line no-unused-vars
function withLayout(kind, routeName, titleKey, handler) {
  return function (root, params) {
    const host = ensureLayout(kind);
    runPageCleanup(host.__pageCleanup);
    host.__pageCleanup = null;
    if (kind !== 'auth' && !isLoggedIn()) {
      router.navigate('#/login', true);
      return null;
    }
    syncNav(routeName);
    setTopbarTitle(t(titleKey));
    closeNav();
    // 同一容器上可能有多次渲染交叠（例如启动期的默认路由跳转）。
    // 用序号标记「当前最新的渲染」：被取代的渲染返回时要立刻执行它自己的
    // 清理，否则它留下的定时器会在几秒后把旧页面重新画回容器里。
    const token = (host.__pageSeq || 0) + 1;
    host.__pageSeq = token;
    const result = handler(host, params);
    if (result && typeof result.then === 'function') {
      return result.then(function (pageCleanup) {
        const fn = typeof pageCleanup === 'function' ? pageCleanup : null;
        if (host.__pageSeq !== token) {
          runPageCleanup(fn);
          return null;
        }
        host.__pageCleanup = fn;
        return null;
      });
    }
    if (host.__pageSeq !== token) {
      runPageCleanup(result);
      return null;
    }
    host.__pageCleanup = typeof result === 'function' ? result : null;
    return null;
  };
}

function authOnly(handler) {
  return function (root, params) {
    if (isLoggedIn() && !state.needsSetup) {
      router.navigate('#/dashboard', true);
      return null;
    }
    return handler(root, params);
  };
}

function registerRoutes() {
  router.addRoute('/login', withLayout('auth', 'login', 'login.title', authOnly(pageLogin.render)));

  router.addRoute('/dashboard', withLayout('app', 'dashboard', 'nav.dashboard', pageDashboard.render));
  router.addRoute('/libraries', withLayout('app', 'libraries', 'nav.libraries', pageLibraries.render));
  router.addRoute('/libraries/new', withLayout('app', 'libraries', 'le.title_new', pageLibraryEdit.render));
  router.addRoute('/libraries/:id/edit', withLayout('app', 'libraries', 'nav.libraries', pageLibraryEdit.render));
  router.addRoute('/works', withLayout('app', 'works', 'nav.works', pageWorks.render));
  router.addRoute('/works/:id', withLayout('app', 'works', 'nav.works', pageWorks.render));
  router.addRoute('/processors', withLayout('app', 'processors', 'nav.processors', pageProcessors.render));
  router.addRoute('/processors/:id', withLayout('app', 'processors', 'pe.title_short', pageProcessorEdit.render));
  router.addRoute('/tasks', withLayout('app', 'tasks', 'nav.tasks', pageTasks.render));
  router.addRoute('/tasks/:id', withLayout('app', 'tasks', 'nav.tasks', pageTaskDetail.render));
  router.addRoute('/settings', withLayout('app', 'settings', 'nav.settings', pageSettings.render));
}

// ---------------------------------------------------------------- 启动

function fatalScreen(message, detail) {
  appEl.innerHTML =
    '<div class="auth-page"><div class="auth-card">' +
    '<div class="auth-brand"><span class="auth-mark">' + icon('alert', 18) + '</span>' +
    '<h1 class="auth-title">' + t('app.short') + '</h1></div>' +
    '<p>' + message + '</p>' +
    (detail ? '<pre>' + detail + '</pre>' : '') +
    '<button type="button" class="btn btn-primary" id="fatal-reload">' + t('common.retry') + '</button>' +
    '</div></div>';
  const btn = document.getElementById('fatal-reload');
  if (btn) btn.addEventListener('click', function () { window.location.reload(); });
}

// 启动期写入默认路由：只改地址栏、不派发 hashchange。
// 若改用 router.navigate()，hashchange 会在 router.start() 之后才到达，
// 于是同一次启动会渲染两遍；被取代的那次渲染的清理器会丢失，其轮询定时器
// 便会在几秒后把旧页面重新画回容器里（侧栏仍停在用户真正打开的那一页）。
function replaceHashSilently(target) {
  if (window.location.hash === target) return;
  try {
    window.history.replaceState(null, '', target);
  } catch (e) {
    window.location.hash = target;
  }
}

async function boot() {
  setLocale(getStoredLocale() || 'zh-CN');
  applyTheme(getStoredTheme());
  bindThemeSwitch(document);

  api.onUnauthorized(function () {
    clearUser();
    toast(t('err.unauthorized'), 'warning');
    router.navigate('#/login', true);
  });

  appEl.__renderNotFound = function (path) {
    const host = ensureLayout('app');
    host.__pageCleanup = null;
    host.innerHTML =
      '<div class="card empty-state">' +
      '<div class="empty-icon">' + icon('alert', 26) + '</div>' +
      '<h3 class="empty-title">404</h3>' +
      '<p class="muted">未找到页面：' + path + '</p>' +
      '<a class="btn btn-primary empty-action" href="#/dashboard">' + t('nav.dashboard') + '</a>' +
      '</div>';
  };

  registerRoutes();

  try {
    const me = await api.authMe();
    setAuth(me || { needs_setup: false, username: null });
  } catch (e) {
    fatalScreen(t('err.load_failed', { msg: e && e.message ? e.message : t('err.unknown') }), '');
    return;
  }

  if (state.needsSetup) {
    replaceHashSilently('#/login');
  } else if (!window.location.hash || window.location.hash === '#' || window.location.hash === '#/') {
    replaceHashSilently('#/dashboard');
  }

  router.start(appEl, {
    onChange: function () {
      closeModals();
      syncThemeButtons();
    }
  });

  // 数据缓存回填后刷新侧栏用户名
  window.addEventListener('hashchange', function () {
    if (shellEls) {
      const nameEl = shellEls.shell.querySelector('#user-name');
      const name = state.user && state.user.username ? state.user.username : '';
      if (nameEl) nameEl.textContent = name || t('common.not_set');
    }
  });
}

boot();

export { emit };
