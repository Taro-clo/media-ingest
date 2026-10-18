// ui.js —— 通用 UI 组件：toast / modal / confirm / skeleton / badge / 表格 / 表单 / chip 输入 / 图标
// 依赖方向：ui.js → i18n.js（单向）。

import { t } from './i18n.js';

// ---------------------------------------------------------------- 模板工具

const RAW = Symbol('raw');

class RawHtml {
  constructor(value) {
    this.value = value;
    this[RAW] = true;
  }
  toString() {
    return String(this.value);
  }
}

/** 标记一段字符串为"已是安全 HTML"，在 html`` 中不再转义。 */
export function raw(value) {
  return new RawHtml(value);
}

export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtValue(v) {
  if (v === null || v === undefined) return '';
  if (Array.isArray(v)) return v.map(fmtValue).join('');
  if (v instanceof RawHtml) return v.toString();
  return esc(v);
}

/**
 * 带自动转义的模板字符串：html`<div>${userValue}</div>`
 * 嵌套时用 raw() 包住已生成的 HTML 片段。
 */
export function html(strings) {
  const values = Array.prototype.slice.call(arguments, 1);
  let out = '';
  for (let i = 0; i < strings.length; i += 1) {
    out += strings[i];
    if (i < values.length) out += fmtValue(values[i]);
  }
  return new RawHtml(out);
}

/** 把字符串或 RawHtml 挂到元素上。 */
export function setHtml(node, content) {
  node.innerHTML = content instanceof RawHtml ? content.toString() : esc(content);
  return node;
}

// ---------------------------------------------------------------- 图标（内联 SVG，无外部依赖）

const ICON_PATHS = {
  dashboard: '<path d="M3 3h7v7H3z"/><path d="M14 3h7v4h-7z"/><path d="M14 10h7v11h-7z"/><path d="M3 13h7v8H3z"/>',
  library: '<path d="M3 5.5A1.5 1.5 0 0 1 4.5 4H9l2 2h8.5A1.5 1.5 0 0 1 21 7.5v11A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5z"/>',
  tasks: '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h10"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9v.09a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z"/>',
  menu: '<path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h16"/>',
  close: '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="M4.9 4.9l1.4 1.4"/><path d="M17.7 17.7l1.4 1.4"/><path d="M19.1 4.9l-1.4 1.4"/><path d="M6.3 17.7l-1.4 1.4"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/>',
  monitor: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8"/><path d="M12 16v4"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  edit: '<path d="M4 20h4l10-10-4-4L4 16z"/><path d="M14.5 5.5l4 4"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/>',
  play: '<path d="M7 4.5l12 7.5-12 7.5z"/>',
  check: '<path d="M4 12.5l5 5L20 6.5"/>',
  alert: '<path d="M12 3l9.5 17H2.5z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.6"/><path d="M20 5v6h-6"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  back: '<path d="M11 5l-7 7 7 7"/><path d="M4 12h16"/>',
  logout: '<path d="M15 5V4a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2v-1"/><path d="M11 12h10"/><path d="M18 8.5L21.5 12 18 15.5"/>',
  chip: '<path d="M9.5 3.5L12 6l2.5-2.5"/>',
  folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2h8.5A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/>',
  arrow_right: '<path d="M5 12h14"/><path d="M13 6l6 6-6 6"/>',
  arrow_up: '<path d="M12 19V5"/><path d="M6 11l6-6 6 6"/>',
  arrow_down: '<path d="M12 5v14"/><path d="M6 13l6 6 6-6"/>',
  code: '<path d="M9 8l-4 4 4 4"/><path d="M15 8l4 4-4 4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
  // 待办看板的行首圆圈：未完成 = 空圈；已完成 = 圈里一个绿勾（见 dashboard.js 的 todo-check）。
  circle: '<circle cx="12" cy="12" r="9"/>',
  check_circle: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.6 2.6L16 9.8"/>'
};

/**
 * 生成内联 SVG 图标。name 未命中时返回空字符串（不抛出）。
 * size 为像素；fill 图标（play）用 currentColor 填充。
 */
export function icon(name, size) {
  const path = ICON_PATHS[name];
  if (!path) return '';
  const s = size || 16;
  const fill = name === 'play' ? 'currentColor' : 'none';
  return '<svg class="icon" width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="' + fill +
    '" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    path + '</svg>';
}

// ---------------------------------------------------------------- 时间/数值格式化

/**
 * 后端 format_time() 统一输出**本地时区**的 'YYYY-MM-DD HH:MM:SS'（不含偏移量），
 * 与 created_at 同格式；任务日志的 ts 也是这个格式。
 * ⇒ 这种字符串必须**原样显示**，绝不能再 new Date() 换算（那会二次偏移）。
 */
const LOCAL_TIME_RE = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$/;

/** 解析时间串为毫秒：无偏移量的按本地时间解析（ES 规范：带 'T' 且无偏移 = 本地时间）。 */
function parseTime(value) {
  if (!value) return NaN;
  const s = String(value).trim();
  return new Date(LOCAL_TIME_RE.test(s) ? s.replace(' ', 'T') : s).getTime();
}

export function fmtDateTime(value) {
  if (!value) return '—';
  const s = String(value);
  if (LOCAL_TIME_RE.test(s)) return s; // 后端 format_time() 产物：原样显示
  // 兜底：万一某个字段仍是带偏移的 ISO8601，按浏览器本地时区显示。
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  const pad = function (n) {
    return n < 10 ? '0' + n : String(n);
  };
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
    pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

export function fmtDuration(ms) {
  if (ms === null || ms === undefined || ms === '') return '—';
  const n = Number(ms);
  if (!isFinite(n) || n < 0) return '—';
  if (n < 1000) return Math.round(n) + ' ms';
  const s = n / 1000;
  if (s < 60) return s.toFixed(1) + ' s';
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  if (m < 60) return m + ' m ' + rest + ' s';
  const h = Math.floor(m / 60);
  return h + ' h ' + (m - h * 60) + ' m';
}

/** 从 started_at/finished_at 计算耗时；未结束返回 '—'。 */
export function durationBetween(startIso, endIso) {
  if (!startIso) return '—';
  const start = parseTime(startIso);
  const end = endIso ? parseTime(endIso) : Date.now();
  if (isNaN(start) || isNaN(end)) return '—';
  return fmtDuration(end - start);
}

/**
 * 任务里展示的「耗时」：优先用执行结果里的毫秒数（`result.duration_ms`，毫秒精度），
 * 只有没有结果（未跑完 / 旧记录）时才退回 started_at → finished_at ——
 * 那两个时间戳只到秒，短任务会算成 `0 ms`，与详情页的 `17 ms` 对不上。
 */
export function taskDuration(task) {
  const r = task && task.result;
  if (r && typeof r.duration_ms === 'number' && isFinite(r.duration_ms) && r.duration_ms >= 0) {
    return fmtDuration(r.duration_ms);
  }
  return durationBetween(task && task.started_at, task && task.finished_at);
}

export function fmtCount(value) {
  const n = Number(value);
  return isFinite(n) ? String(n) : '0';
}

// ---------------------------------------------------------------- toast

function ensureRoot(id, cls) {
  let node = document.getElementById(id);
  if (!node) {
    node = document.createElement('div');
    node.id = id;
    if (cls) node.className = cls;
    document.body.appendChild(node);
  }
  return node;
}

/**
 * 轻提示。type: info | success | warning | error
 * 返回一个可手动关闭的函数。
 */
export function toast(message, type, timeout) {
  const kind = type || 'info';
  const root = ensureRoot('toast-root', 'toast-root');
  const node = document.createElement('div');
  node.className = 'toast toast-' + kind;
  node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  const iconName = kind === 'success' ? 'check' : (kind === 'error' || kind === 'warning') ? 'alert' : 'info';
  node.innerHTML = '<span class="toast-icon">' + icon(iconName, 16) + '</span>' +
    '<span class="toast-text">' + esc(message) + '</span>' +
    '<button type="button" class="toast-close" aria-label="' + esc(t('common.close')) + '">' + icon('close', 14) + '</button>';
  const remove = function () {
    if (!node.parentNode) return;
    node.classList.add('toast-out');
    window.setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 160);
  };
  node.querySelector('.toast-close').addEventListener('click', remove);
  root.appendChild(node);
  const ms = timeout === undefined ? (kind === 'error' ? 6000 : 3200) : timeout;
  if (ms > 0) window.setTimeout(remove, ms);
  return remove;
}

// ---------------------------------------------------------------- modal / confirm

// 当前打开的模态框关闭函数；路由切换时统一关闭，避免弹窗跨页面残留。
const openModalClosers = [];

/** 关闭所有已打开的模态框（路由切换时由 app.js 调用）。 */
export function closeModals() {
  openModalClosers.slice().forEach(function (fn) {
    try {
      fn(undefined);
    } catch (e) {
      /* 单个弹窗关闭失败不影响其余弹窗 */
    }
  });
  openModalClosers.length = 0;
  const root = document.getElementById('modal-root');
  if (root) root.innerHTML = '';
}

/**
 * 打开模态框。opts: {title, body(html 字符串或节点), actions:[{label,kind,onClick,close}],
 * closeOnBackdrop, width, onClose}
 * 返回 {close, root, body}
 */
export function modal(opts) {
  const options = opts || {};
  const root = ensureRoot('modal-root', 'modal-root');
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';

  const box = document.createElement('div');
  box.className = 'modal' + (options.width ? ' modal-' + options.width : '');
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  if (options.title) box.setAttribute('aria-label', String(options.title));

  const header = document.createElement('div');
  header.className = 'modal-header';
  header.innerHTML = '<h3 class="modal-title">' + esc(options.title || '') + '</h3>' +
    '<button type="button" class="icon-btn modal-x" aria-label="' + esc(t('common.close')) + '">' + icon('close', 16) + '</button>';

  const body = document.createElement('div');
  body.className = 'modal-body';
  if (options.body instanceof Node) {
    body.appendChild(options.body);
  } else if (options.body instanceof RawHtml) {
    body.innerHTML = options.body.toString();
  } else if (typeof options.body === 'string') {
    // body 参数按契约就是 HTML 片段（拼装时务必用 esc()/html`` 处理插值）
    body.innerHTML = options.body;
  }

  const footer = document.createElement('div');
  footer.className = 'modal-footer';

  box.appendChild(header);
  box.appendChild(body);
  if (options.footer !== false) box.appendChild(footer);
  overlay.appendChild(box);

  let closed = false;
  const close = function (result) {
    if (closed) return;
    closed = true;
    const idx = openModalClosers.indexOf(close);
    if (idx >= 0) openModalClosers.splice(idx, 1);
    document.removeEventListener('keydown', onKey);
    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    if (typeof options.onClose === 'function') options.onClose(result);
  };
  openModalClosers.push(close);

  function onKey(ev) {
    if (ev.key === 'Escape' && options.closeOnEscape !== false) {
      ev.preventDefault();
      close(undefined);
    }
  }
  document.addEventListener('keydown', onKey);
  header.querySelector('.modal-x').addEventListener('click', function () {
    close(undefined);
  });
  if (options.closeOnBackdrop !== false) {
    overlay.addEventListener('mousedown', function (ev) {
      if (ev.target === overlay) close(undefined);
    });
  }

  (options.actions || []).forEach(function (action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn ' + (action.kind ? 'btn-' + action.kind : 'btn-secondary');
    btn.textContent = action.label;
    if (action.id) btn.id = action.id;
    btn.addEventListener('click', function () {
      let result;
      if (typeof action.onClick === 'function') result = action.onClick(close, body);
      if (action.close !== false && result !== false) close(result);
    });
    footer.appendChild(btn);
  });
  if (options.footer !== false && (!options.actions || options.actions.length === 0)) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-secondary';
    btn.textContent = t('common.close');
    btn.addEventListener('click', function () {
      close(undefined);
    });
    footer.appendChild(btn);
  }

  root.appendChild(overlay);
  const focusTarget = box.querySelector('.btn-primary') || header.querySelector('.modal-x');
  if (focusTarget) focusTarget.focus();
  return { close: close, root: box, body: body, footer: footer };
}

/** confirmDialog 的正文块：Node 原样返回；RawHtml 作为 HTML；其余按纯文本转义。 */
function confirmBlock(value, className) {
  if (value === undefined || value === null || value === '') return null;
  if (value instanceof Node) return value;
  const p = document.createElement('p');
  p.className = className;
  if (value instanceof RawHtml) p.innerHTML = value.toString();
  else p.textContent = String(value);
  return p;
}

/** 二次确认。danger=true 时确认按钮为红色。返回 Promise<boolean>。
 *  message/detail 可以是字符串（按纯文本插入，自动转义）、RawHtml 或 DOM 节点
 *  （节点会被原样插入，供需要嵌入表单/单选组的确认框使用）。 */
export function confirmDialog(opts) {
  const options = opts || {};
  return new Promise(function (resolve) {
    let settled = false;
    const bodyEl = document.createElement('div');
    bodyEl.className = 'confirm-body';
    const msgEl = confirmBlock(options.message, 'confirm-text');
    if (msgEl) bodyEl.appendChild(msgEl);
    const detailEl = confirmBlock(options.detail, 'muted confirm-detail');
    if (detailEl) bodyEl.appendChild(detailEl);
    modal({
      title: options.title || t('common.ok'),
      width: 'sm',
      body: bodyEl,
      closeOnBackdrop: false,
      onClose: function () {
        if (!settled) {
          settled = true;
          resolve(false);
        }
      },
      actions: [
        {
          label: options.cancelText || t('common.cancel'),
          kind: 'secondary',
          onClick: function () {
            settled = true;
            resolve(false);
          }
        },
        {
          label: options.confirmText || t('common.ok'),
          kind: options.danger ? 'danger' : 'primary',
          onClick: function () {
            settled = true;
            resolve(true);
          }
        }
      ]
    });
  });
}

// ---------------------------------------------------------------- 骨架 / 空态 / 错误态

export function skeletonBlock(lines) {
  const n = lines || 3;
  let out = '<div class="skeleton-block" aria-hidden="true">';
  for (let i = 0; i < n; i += 1) {
    const w = 100 - (i % 3) * 12;
    out += '<div class="skeleton-line" style="width:' + w + '%"></div>';
  }
  return out + '</div>';
}

export function skeletonCards(count) {
  const n = count || 4;
  let out = '<div class="stat-grid" aria-hidden="true">';
  for (let i = 0; i < n; i += 1) out += '<div class="skeleton-card"></div>';
  return out + '</div>';
}

export function skeletonTable(rows, cols) {
  const r = rows || 5;
  const c = cols || 5;
  let out = '<div class="skeleton-table" aria-hidden="true">';
  for (let i = 0; i < r; i += 1) {
    out += '<div class="skeleton-row">';
    for (let j = 0; j < c; j += 1) out += '<div class="skeleton-line"></div>';
    out += '</div>';
  }
  return out + '</div>';
}

export function loadingCard(label) {
  return '<div class="card"><div class="card-body">' + skeletonBlock(3) +
    '<p class="muted center-text">' + esc(label || t('common.loading')) + '</p></div></div>';
}

/** 空状态：必须带一句引导文案 + 可选主行动按钮。 */
export function emptyState(opts) {
  const o = opts || {};
  return html`<div class="card empty-state">
    <div class="empty-icon">${raw(icon(o.icon || 'folder', 28))}</div>
    <h3 class="empty-title">${o.title || ''}</h3>
    <p class="muted empty-desc">${o.desc || ''}</p>
    ${o.actionLabel
      ? html`<a class="btn btn-primary empty-action" href="${o.actionHref || '#/dashboard'}">${o.actionLabel}</a>`
      : raw('')}
  </div>`;
}

/** 错误态：可重试。retryId 由调用方绑定点击事件。 */
export function errorState(opts) {
  const o = opts || {};
  return html`<div class="card error-state">
    <div class="error-icon">${raw(icon('alert', 26))}</div>
    <h3 class="error-title">${o.title || t('err.load_failed', { msg: o.message || t('err.unknown') })}</h3>
    ${o.detail ? html`<p class="muted">${o.detail}</p>` : raw('')}
    <button type="button" class="btn btn-secondary" id="${o.retryId || 'retry-btn'}">${raw(icon('refresh', 15))}${t('common.retry')}</button>
  </div>`;
}

// ---------------------------------------------------------------- badge / 状态

const STATUS_KIND = {
  queued: 'muted',
  running: 'info',
  success: 'ok',
  failed: 'danger',
  skipped: 'warn',
  cancelled: 'muted'
};

export function statusLabel(status) {
  const key = 'status.' + status;
  const label = t(key);
  return label === key ? (status || t('common.unknown')) : label;
}

export function statusBadge(status) {
  const kind = STATUS_KIND[status] || 'muted';
  const pulse = status === 'running' ? ' badge-pulse' : '';
  return '<span class="badge badge-' + kind + pulse + '"><span class="badge-dot"></span>' +
    esc(statusLabel(status)) + '</span>';
}

export function badge(text, kind) {
  return '<span class="badge badge-' + (kind || 'muted') + '">' + esc(text) + '</span>';
}

export function kindLabel(kind) {
  if (kind === 'hardlink' || kind === 'link') return t('tasks.kind.link');
  if (kind === 'rename') return t('tasks.kind.rename');
  if (kind === 'rename_hardlink' || kind === 'link+rename' || kind === 'link_rename') return t('tasks.kind.link_rename');
  return kind || t('common.unknown');
}

export function triggerLabel(trigger) {
  const key = 'tasks.trigger.' + trigger;
  const label = t(key);
  return label === key ? (trigger || t('common.unknown')) : label;
}

export function scopeLabel(scope) {
  if (!scope) return t('tasks.scope.full');
  if (typeof scope === 'string') {
    try {
      return scopeLabel(JSON.parse(scope));
    } catch (e) {
      return scope;
    }
  }
  if (scope.mode === 'folder' && scope.folder) return t('tasks.scope.folder', { folder: scope.folder });
  return t('tasks.scope.full');
}

// ---------------------------------------------------------------- 表单组件

/**
 * 包装一个表单字段。opts: {label, control(html 或 Node), hint, error, id, required}
 */
export function field(opts) {
  const o = opts || {};
  const controlHtml = o.control instanceof RawHtml || typeof o.control === 'string'
    ? o.control
    : '';
  return html`<div class="field${o.error ? ' field-invalid' : ''}" ${o.id ? raw('data-field="' + esc(o.id) + '"') : raw('')}>
    ${o.label
      ? html`<label class="field-label" ${o.id ? raw('for="' + esc(o.id) + '"') : raw('')}>${o.label}${o.required ? html`<span class="req">*</span>` : raw('')}</label>`
      : raw('')}
    ${controlHtml}
    ${o.hint ? html`<p class="field-hint">${o.hint}</p>` : raw('')}
    <p class="field-error" data-error-for="${o.id || ''}">${o.error || ''}</p>
  </div>`;
}

export function textInput(opts) {
  const o = opts || {};
  return html`<input class="input" type="${o.type || 'text'}" id="${o.id || ''}" name="${o.name || ''}"
    value="${o.value === undefined || o.value === null ? '' : o.value}"
    placeholder="${o.placeholder || ''}" ${o.autocomplete ? raw('autocomplete="' + esc(o.autocomplete) + '"') : raw('')}
    ${o.list ? raw('list="' + esc(o.list) + '"') : raw('')}
    ${o.required ? raw('required') : raw('')} ${o.disabled ? raw('disabled') : raw('')}>`;
}

export function numberInput(opts) {
  const o = opts || {};
  return html`<input class="input" type="number" id="${o.id || ''}" name="${o.name || ''}"
    value="${o.value === undefined || o.value === null ? '' : o.value}"
    min="${o.min === undefined ? '' : o.min}" max="${o.max === undefined ? '' : o.max}"
    step="${o.step === undefined ? '1' : o.step}" ${o.disabled ? raw('disabled') : raw('')}>`;
}

export function selectInput(opts) {
  const o = opts || {};
  const options = (o.options || []).map(function (opt) {
    const selected = String(opt.value) === String(o.value) ? ' selected' : '';
    return '<option value="' + esc(opt.value) + '"' + selected + '>' + esc(opt.label) + '</option>';
  }).join('');
  return html`<select class="input select" id="${o.id || ''}" name="${o.name || ''}" ${o.disabled ? raw('disabled') : raw('')}>${raw(options)}</select>`;
}

export function textareaInput(opts) {
  const o = opts || {};
  return html`<textarea class="input textarea" id="${o.id || ''}" name="${o.name || ''}" rows="${o.rows || 3}"
    placeholder="${o.placeholder || ''}" ${o.readonly ? raw('readonly') : raw('')}>${o.value || ''}</textarea>`;
}

export function switchControl(opts) {
  const o = opts || {};
  return html`<label class="switch">
    <input type="checkbox" id="${o.id || ''}" name="${o.name || ''}" ${o.checked ? raw('checked') : raw('')}>
    <span class="switch-track"><span class="switch-thumb"></span></span>
    <span class="switch-label">${o.label || ''}</span>
  </label>`;
}

/** 读取 input/select/textarea 的值（trim 后）。 */
export function val(root, selector) {
  const node = root.querySelector(selector);
  if (!node) return '';
  if (node.type === 'checkbox') return node.checked;
  return (node.value || '').trim();
}

export function setFieldError(scope, fieldId, message) {
  const box = scope.querySelector('[data-field="' + fieldId + '"]');
  if (!box) return;
  const err = box.querySelector('.field-error');
  if (err) err.textContent = message || '';
  box.classList.toggle('field-invalid', !!message);
}

export function clearFieldErrors(scope) {
  scope.querySelectorAll('.field').forEach(function (f) {
    f.classList.remove('field-invalid');
    const e = f.querySelector('.field-error');
    if (e) e.textContent = '';
  });
}

// ---------------------------------------------------------------- chip 多值输入

/**
 * 多值输入：输入 + 回车 / 加号按钮 添加；chip 可删除。
 * opts: {id, values:[], placeholder, addLabel}
 * 返回 { root: HTMLElement, getValues(), setValues(list) }
 */
export function createChipInput(opts) {
  const o = opts || {};
  const root = document.createElement('div');
  root.className = 'chip-input';
  root.dataset.chipId = o.id || '';
  let values = Array.isArray(o.values) ? o.values.slice() : [];

  const chipWrap = document.createElement('div');
  chipWrap.className = 'chip-list';

  const row = document.createElement('div');
  row.className = 'chip-row';
  const input = document.createElement('input');
  input.className = 'input chip-text';
  input.type = 'text';
  input.placeholder = o.placeholder || '';
  if (o.id) input.id = o.id + '-input';
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'btn btn-secondary btn-sm chip-add';
  addBtn.innerHTML = icon('plus', 14) + '<span>' + esc(o.addLabel || t('le.chip.add')) + '</span>';
  row.appendChild(input);
  row.appendChild(addBtn);

  root.appendChild(chipWrap);
  root.appendChild(row);

  function render() {
    chipWrap.innerHTML = '';
    if (values.length === 0) {
      const empty = document.createElement('span');
      empty.className = 'chip-empty muted';
      empty.textContent = o.emptyText || '';
      chipWrap.appendChild(empty);
      return;
    }
    values.forEach(function (value) {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.innerHTML = '<span class="chip-text-value">' + esc(value) + '</span>';
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'chip-del';
      del.setAttribute('aria-label', t('le.chip.remove', { value: value }));
      del.innerHTML = icon('close', 12);
      del.addEventListener('click', function () {
        values = values.filter(function (v) {
          return v !== value;
        });
        render();
        if (typeof o.onChange === 'function') o.onChange(values.slice());
      });
      chip.appendChild(del);
      chipWrap.appendChild(chip);
    });
  }

  function add() {
    const v = (input.value || '').trim();
    if (!v) return false;
    if (values.indexOf(v) >= 0) {
      input.value = '';
      return false;
    }
    values.push(v);
    input.value = '';
    render();
    input.focus();
    if (typeof o.onChange === 'function') o.onChange(values.slice());
    return true;
  }

  input.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      add();
    } else if (ev.key === 'Backspace' && !input.value && values.length) {
      values.pop();
      render();
      if (typeof o.onChange === 'function') o.onChange(values.slice());
    }
  });
  input.addEventListener('blur', function () {
    if ((input.value || '').trim()) add();
  });
  addBtn.addEventListener('click', function (ev) {
    ev.preventDefault();
    add();
  });

  render();
  return {
    root: root,
    getValues: function () {
      return values.slice();
    },
    setValues: function (list) {
      values = Array.isArray(list) ? list.slice() : [];
      render();
    },
    focus: function () {
      input.focus();
    }
  };
}

// ---------------------------------------------------------------- 其它小工具

/** 复制文本到剪贴板；不可用时回退到临时 textarea + execCommand。 */
export async function copyText(text) {
  const value = String(text === undefined || text === null ? '' : text);
  try {
    if (navigator.clipboard && window.isSecureContext !== false) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch (e) {
    /* 继续尝试回退方案 */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = value;
    ta.setAttribute('readonly', 'readonly');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

/** 给元素绑定一次性点击（渲染后使用）。 */
export function on(node, selector, event, handler) {
  if (!node) return;
  const target = typeof selector === 'string' ? node.querySelector(selector) : selector;
  if (target) target.addEventListener(event, handler);
}

export function debounce(fn, wait) {
  let timer = null;
  return function () {
    const args = arguments;
    const self = this;
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(function () {
      timer = null;
      fn.apply(self, args);
    }, wait || 300);
  };
}

export { ensureRoot };
