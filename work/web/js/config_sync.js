// config_sync.js —— 配置文件同步状态（PROCESSORS.md §6/§7）
//
// 契约：GET /api/config → {config_dir, files:[{path,relative,exists,sha256,in_sync,size,updated_at}],
//                          last_import_at, last_export_at, errors:[{path,message,at}]}
//   POST /api/config/reload（?force=true 忽略哈希） / POST /api/config/export
//
// 运行时真源是 SQLite；磁盘上的 /data/config/libraries.yaml 与 /data/config/processors/<id>.yaml
// 是可移植镜像（YAML 形态，见 PROCESSORS.md §6.1）。这里只做展示与两个动作，不缓存任何权威状态。

import { t } from './i18n.js';
import * as api from './api.js';
import { isLoggedIn } from './store.js';
import { raw, esc, icon, toast, fmtDateTime, badge } from './ui.js';

const REFRESH_MS = 15000;

function fmtSize(n) {
  if (typeof n !== 'number' || !isFinite(n) || n < 0) return '—';
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v = v / 1024; i += 1; }
  return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
}

function shortSha(sha) {
  if (!sha || typeof sha !== 'string') return '—';
  return sha.length > 12 ? sha.slice(0, 12) + '…' : sha;
}

/** 是否存在需要提醒用户的情况（外部修改未对齐，或导入报错）。 */
export function configProblems(cfg) {
  if (!cfg || typeof cfg !== 'object') return { any: false, outOfSync: [], errors: [] };
  const files = Array.isArray(cfg.files) ? cfg.files : [];
  const outOfSync = files.filter(function (f) { return f && f.in_sync === false; });
  const errors = Array.isArray(cfg.errors) ? cfg.errors : [];
  return { any: outOfSync.length > 0 || errors.length > 0, outOfSync: outOfSync, errors: errors };
}

function filesTable(files) {
  if (!files.length) {
    return '<p class="muted">' + esc(t('cfg.empty')) + '</p>';
  }
  const rows = files.map(function (f) {
    const state = f.exists === false
      ? badge(t('cfg.file.missing'), 'danger')
      : (f.in_sync === false ? badge(t('cfg.file.pending'), 'warn') : badge(t('cfg.file.synced'), 'ok'));
    return '<tr>' +
      '<td data-label="' + esc(t('cfg.file.relative')) + '"><span class="mono">' + esc(f.relative || f.path || '') + '</span></td>' +
      '<td data-label="' + esc(t('cfg.file.size')) + '">' + esc(fmtSize(f.size)) + '</td>' +
      '<td data-label="' + esc(t('cfg.file.updated')) + '">' + esc(fmtDateTime(f.updated_at)) + '</td>' +
      '<td data-label="' + esc(t('cfg.file.sha')) + '"><span class="mono" title="' + esc(f.sha256 || '') + '">' + esc(shortSha(f.sha256)) + '</span></td>' +
      '<td data-label="' + esc(t('cfg.file.state')) + '">' + state + '</td>' +
      '</tr>';
  }).join('');
  return '<div class="table-wrap"><table class="table responsive"><thead><tr>' +
    '<th>' + esc(t('cfg.file.relative')) + '</th>' +
    '<th>' + esc(t('cfg.file.size')) + '</th>' +
    '<th>' + esc(t('cfg.file.updated')) + '</th>' +
    '<th>' + esc(t('cfg.file.sha')) + '</th>' +
    '<th>' + esc(t('cfg.file.state')) + '</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}

function errorsBox(errors) {
  if (!errors.length) return '';
  const items = errors.map(function (e) {
    return '<li><span class="mono">' + esc(e.path || '') + '</span>' +
      (e.at ? ' <span class="muted">(' + esc(fmtDateTime(e.at)) + ')</span>' : '') +
      '<br><span class="error-text">' + esc(e.message || '') + '</span></li>';
  }).join('');
  return '<div class="callout callout-danger">' + raw(icon('alert', 14)) +
    '<div><strong>' + esc(t('cfg.errors.title')) + '</strong><ul class="cfg-error-list">' + raw(items) + '</ul></div></div>';
}

/** 渲染状态块主体（不含按钮状态管理）。 */
function bodyHtml(cfg) {
  const p = configProblems(cfg);
  const files = Array.isArray(cfg.files) ? cfg.files : [];
  const state = p.errors.length
    ? badge(t('cfg.errors.title'), 'danger')
    : (p.outOfSync.length ? badge(t('cfg.state.pending'), 'warn') : badge(t('cfg.state.synced'), 'ok'));
  const kv =
    '<dl class="kv">' +
    '<div><dt>' + esc(t('cfg.dir')) + '</dt><dd><span class="mono">' + esc(cfg.config_dir || '—') + '</span></dd></div>' +
    '<div><dt>' + esc(t('cfg.last_import')) + '</dt><dd>' + esc(fmtDateTime(cfg.last_import_at)) + '</dd></div>' +
    '<div><dt>' + esc(t('cfg.last_export')) + '</dt><dd>' + esc(fmtDateTime(cfg.last_export_at)) + '</dd></div>' +
    '</dl>';
  return {
    state: state,
    outOfSync: p.outOfSync,
    html: kv + errorsBox(p.errors) +
      '<h3 class="cfg-sub">' + esc(t('cfg.files')) + '</h3>' + filesTable(files)
  };
}

function shellHtml(title) {
  return '<section class="card cfg-card">' +
    '<div class="card-head">' +
      '<h2 class="card-title">' + esc(title) + '</h2>' +
      '<div class="card-actions" data-role="cfg-actions">' +
        '<span data-role="cfg-state"></span>' +
        '<button type="button" class="btn btn-secondary btn-sm" data-role="cfg-refresh" aria-label="' + esc(t('common.refresh')) + '">' +
          raw(icon('refresh', 13)) + '<span>' + esc(t('common.refresh')) + '</span></button>' +
        '<button type="button" class="btn btn-secondary btn-sm" data-role="cfg-export">' + esc(t('cfg.export')) + '</button>' +
        '<button type="button" class="btn btn-secondary btn-sm" data-role="cfg-reload">' + esc(t('cfg.reload')) + '</button>' +
      '</div>' +
    '</div>' +
    '<div class="card-body" data-role="cfg-body">' + esc(t('common.loading')) + '</div>' +
    '</section>';
}

/**
 * 把配置同步状态块挂到 host（会清空 host）。返回 {refresh, stop}。
 * opts.title 可覆盖标题；opts.compact=true 时由调用方自行决定是否隐藏。
 */
export function mountConfigStatus(host, opts) {
  const o = opts || {};
  if (!host) return { refresh: function () {}, stop: function () {} };
  host.innerHTML = shellHtml(o.title || t('cfg.title'));
  const body = host.querySelector('[data-role="cfg-body"]');
  const stateSlot = host.querySelector('[data-role="cfg-state"]');
  const refreshBtn = host.querySelector('[data-role="cfg-refresh"]');
  const exportBtn = host.querySelector('[data-role="cfg-export"]');
  const reloadBtn = host.querySelector('[data-role="cfg-reload"]');
  let alive = true;

  function paint(cfg) {
    if (!alive) return;
    if (!cfg) {
      stateSlot.innerHTML = badge(t('common.unknown'), 'muted');
      body.innerHTML = '<div class="callout callout-warn">' + raw(icon('alert', 14)) +
        '<span>' + esc(t('cfg.unavailable')) + '</span></div>';
      return;
    }
    const view = bodyHtml(cfg);
    stateSlot.innerHTML = view.state;
    body.innerHTML = view.html;
  }

  async function refresh(silent) {
    if (!alive) return;
    try {
      const cfg = await api.getConfig();
      paint(cfg);
      if (!silent && !configProblems(cfg).any) toast(t('cfg.state.synced'), 'success');
    } catch (e) {
      if (!alive) return;
      stateSlot.innerHTML = badge(t('common.unknown'), 'muted');
      body.innerHTML = '<div class="callout callout-warn">' + raw(icon('alert', 14)) +
        '<span>' + esc(t('cfg.unavailable')) + '</span>' +
        '<p class="muted mono">' + esc(e && e.detail ? e.detail : '') + '</p></div>';
    }
  }

  async function doReload() {
    reloadBtn.disabled = true;
    try {
      await api.reloadConfig(false);
      toast(t('cfg.reload_ok'), 'success');
      await refresh(true);
    } catch (e) {
      toast(t('err.action_failed', { msg: e && e.detail ? e.detail : t('err.unknown') }), 'error');
    } finally {
      if (reloadBtn.isConnected) reloadBtn.disabled = false;
    }
  }

  async function doExport() {
    exportBtn.disabled = true;
    try {
      await api.exportConfig();
      toast(t('cfg.export_ok'), 'success');
      await refresh(true);
    } catch (e) {
      toast(t('err.action_failed', { msg: e && e.detail ? e.detail : t('err.unknown') }), 'error');
    } finally {
      if (exportBtn.isConnected) exportBtn.disabled = false;
    }
  }

  refreshBtn.addEventListener('click', function () { refresh(false); });
  reloadBtn.addEventListener('click', doReload);
  exportBtn.addEventListener('click', doExport);
  refresh(true);

  return {
    refresh: refresh,
    stop: function () { alive = false; }
  };
}

/**
 * 全局监听：出现「外部修改未对齐 / 导入报错」时通过 onState 通知（用于顶栏横幅）。
 * 返回 stop()。document.hidden 时跳过轮询。
 */
export function startConfigWatch(onState) {
  let stopped = false;
  let timer = null;

  async function tick() {
    // 已登出（含改密码成功后的强制登出）时不要继续轮询：否则 /api/config 的 401 会
    // 触发全局 onUnauthorized，把「登录已失效」的提示反复弹到登录页上。
    if (stopped || document.hidden || !isLoggedIn()) return;
    try {
      const cfg = await api.getConfig();
      const p = configProblems(cfg);
      if (p.any) {
        onState({
          title: t('cfg.banner'),
          message: p.errors.length
            ? t('cfg.errors.title')
            : t('cfg.out_of_sync', { files: p.outOfSync.map(function (f) { return f.relative || f.path; }).join('、') }),
          danger: p.errors.length > 0
        });
      } else {
        onState(null);
      }
    } catch (e) {
      onState(null);
    }
  }

  function schedule() {
    if (stopped) return;
    timer = window.setTimeout(function () {
      tick().then(schedule, schedule);
    }, REFRESH_MS);
  }

  tick().then(schedule, schedule);
  return function stop() {
    stopped = true;
    if (timer) window.clearTimeout(timer);
  };
}
