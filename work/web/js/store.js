// store.js —— 轻量全局状态（当前用户、媒体库缓存、设置缓存、系统信息、主题）

const THEME_KEY = 'mlm.theme';
const LOCALE_KEY = 'mlm.locale';

export const state = {
  /** @type {{username:string|null}|null} */
  user: null,
  needsSetup: false,
  authChecked: false,
  /** @type {Array<object>} */
  libraries: [],
  librariesLoaded: false,
  /** @type {Record<string,string>|null} */
  settings: null,
  /** @type {object|null} */
  systemInfo: null,
  /** @type {Array<object>} 处理器注册表缓存（v2） */
  processors: [],
  processorsLoaded: false,
  theme: 'system'
};

const listeners = new Set();

export function subscribe(fn) {
  listeners.add(fn);
  return function unsubscribe() {
    listeners.delete(fn);
  };
}

export function emit() {
  listeners.forEach(function (fn) {
    try {
      fn(state);
    } catch (e) {
      // 单个订阅者异常不应影响其他订阅者
      console.error('[store] listener error', e);
    }
  });
}

// ---------- 用户 ----------

export function setAuth(payload) {
  state.needsSetup = !!(payload && payload.needs_setup);
  state.user = payload && payload.username ? { username: payload.username } : null;
  state.authChecked = true;
  emit();
}

export function setUser(username) {
  state.user = username ? { username: username } : null;
  state.needsSetup = false;
  state.authChecked = true;
  emit();
}

export function clearUser() {
  state.user = null;
  state.libraries = [];
  state.librariesLoaded = false;
  state.settings = null;
  state.systemInfo = null;
  state.processors = [];
  state.processorsLoaded = false;
  emit();
}

export function isLoggedIn() {
  return !!(state.user && state.user.username);
}

// ---------- 缓存 ----------

export function setLibraries(list) {
  state.libraries = Array.isArray(list) ? list : [];
  state.librariesLoaded = true;
  emit();
}

export function getLibrary(id) {
  const numId = Number(id);
  return state.libraries.find(function (l) {
    return Number(l.id) === numId;
  }) || null;
}

export function setSettings(settings) {
  state.settings = settings || null;
  emit();
}

export function getSetting(key, fallback) {
  if (!state.settings) return fallback;
  const v = state.settings[key];
  return v === undefined || v === null ? fallback : v;
}

export function setSystemInfo(info) {
  state.systemInfo = info || null;
  emit();
}


// ---------- 处理器（v2） ----------

export function setProcessors(list) {
  state.processors = Array.isArray(list) ? list : [];
  state.processorsLoaded = true;
  emit();
}

export function getProcessor(id) {
  const key = String(id);
  return state.processors.find(function (p) {
    return String(p.id) === key;
  }) || null;
}

/** 处理器 id → 展示名；未加载到注册表时退回 id 本身，避免界面出现空白。 */
export function processorName(id) {
  if (!id) return '';
  const p = getProcessor(id);
  return p && p.name ? p.name : String(id);
}

// ---------- 主题 / 语言持久化 ----------

export function getStoredTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === 'light' || v === 'dark' || v === 'system') return v;
  } catch (e) {
    /* localStorage 不可用（隐私模式等）时忽略 */
  }
  return 'system';
}

export function setStoredTheme(theme) {
  state.theme = theme;
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch (e) {
    /* 忽略持久化失败 */
  }
  emit();
}

export function getStoredLocale() {
  try {
    return localStorage.getItem(LOCALE_KEY) || 'zh-CN';
  } catch (e) {
    return 'zh-CN';
  }
}

export function setStoredLocale(locale) {
  try {
    localStorage.setItem(LOCALE_KEY, locale);
  } catch (e) {
    /* 忽略 */
  }
}
