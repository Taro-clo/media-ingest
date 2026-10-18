// pages/settings.js —— 设置：配置同步 / 集成 / 扫描 / 日志与历史 / 回调 / 重命名日志 / 路径映射 / 系统信息 / 密码
// 布局（区块顺序）：
//   1) 页头：标题 + 副标题 + 未保存提示 + 刷新 + 保存设置
//   2) 配置文件同步（config_sync.js 的 mountConfigStatus：GET /api/config + reload/export）
//   3) qBittorrent 接入（url / username / password + 测试连接 + 结果行 + 回调路径映射）
//   4) 目录扫描（scan_enabled / scan_interval_seconds）
//   5) 日志与历史（log_level / max_history / max_task_log_lines / orphan_cleanup_enabled）
//   5b) 重命名后缀白名单（rename_extensions，多行文本 ⇄ 数组；对所有媒体库生效）
//   6) qBittorrent 回调（hook_token 明文 + 复制 Token + 复制回调命令 + 重新生成 Token + curl 命令块）
//   7) 重命名日志（info.rename_logs 只读表格；§5.2）
//   8) 路径映射（只读，info.path_map.container_to_host）
//   9) 运行自检（/api/system/health，无需认证）
//  10) 系统信息（version / timezone / data_dir / config_dir / log_dir / processors / libraries / config / 端口 / 队列 / 同一文件系统 / 挂载点）
//  11) 修改密码
// 数据源单一形态：GET /api/settings 为已解析类型的裸对象；PUT 只提交变更字段。
// 注意：本页**不含**「脚本日志连续性」面板（`seed_logs_on_first_run` 开关随之取消）。

import { t } from '../i18n.js';
import * as api from '../api.js';
import { setSettings } from '../store.js';
import { mountConfigStatus } from '../config_sync.js';
import {
  html, raw, esc, icon, toast, field, textInput, numberInput, selectInput, textareaInput,
  switchControl, copyText, confirmDialog, fmtDateTime, skeletonBlock, errorState
} from '../ui.js';

const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];

const MOUNT_COLS = [
  ['container_path', 'set.system.mount.container'],
  ['host_path', 'set.system.mount.host'],
  ['device', 'set.system.mount.device'],
  ['fs_type', 'set.system.mount.fs'],
  ['options', 'set.system.mount.options'],
  ['st_dev', 'set.system.mount.stdev']
];

// v2：健康检查的键序**不写死**（healthCard 按后端 checks 对象键序渲染）；
// 这张表只是标签兜底字典：认得的键给中文名，认不得的键直接用原始 key 作标签。
const HEALTH_LABELS = {
  db: 'set.health.db',
  processors: 'set.health.processors',
  config: 'set.health.config',
  data_dir: 'set.health.data_dir'
};

// ---------------------------------------------------------------- 小工具

function kvRows(pairs) {
  return '<dl class="kv">' + pairs.map(function (p) {
    return '<dt>' + esc(p[0]) + '</dt><dd' + (p[2] ? ' class="' + esc(p[2]) + '"' : '') + '>' + p[1] + '</dd>';
  }).join('') + '</dl>';
}

function textCell(value, cls) {
  const text = value === null || value === undefined || value === '' ? t('common.none') : String(value);
  return '<span class="' + (cls || 'mono') + '">' + esc(text) + '</span>';
}

function boolBadge(ok, okKey, badKey) {
  return '<span class="badge badge-' + (ok ? 'ok' : 'danger') + '">' + esc(t(ok ? okKey : badKey)) + '</span>';
}

function linesToArray(text) {
  return String(text || '').split('\n').map(function (s) { return s.trim(); }).filter(function (s) { return s !== ''; });
}

// 后缀白名单专用：一行里可能塞了多个（".mkv .mp4 .ass"），空格/逗号/顿号/分号都当分隔符。
// 不这么拆，"看似生效、其实一个文件都匹配不上"（endswith 整个字符串永远为假）。
function suffixItemsToArray(text) {
  const out = [];
  linesToArray(text).forEach(function (line) {
    String(line).split(/[\s,，、;；]+/).forEach(function (token) {
      if (token) out.push(token.toLowerCase());
    });
  });
  return out;
}

function arrayToText(list) {
  return Array.isArray(list) ? list.join('\n') : '';
}

// ---------------------------------------------------------------- 回调路径映射
// 后端 `downloader_path_map` 存的是多行文本（一行一条 `左侧 => 右侧`，`#` 注释行、
// 空行由后端忽略）。WebUI 用「每行两个输入框」的编辑器呈现，因此这里做双向转换。

/** 文本 → [{from, to}]。缺 `=>` 的行按"只填了左侧"处理，交给 validate 提示。 */
function parsePathMap(text) {
  const out = [];
  String(text || '').split('\n').forEach(function (line) {
    const s = line.trim();
    if (!s || s.charAt(0) === '#') return;
    const idx = s.indexOf('=>');
    if (idx < 0) {
      out.push({ from: s, to: '' });
      return;
    }
    out.push({ from: s.slice(0, idx).trim(), to: s.slice(idx + 2).trim() });
  });
  return out;
}

function pathMapRowHtml(pair, index) {
  const from = (pair && pair.from) || '';
  const to = (pair && pair.to) || '';
  return '<div class="pathmap-row" data-pathmap-row>' +
    '<div class="pathmap-cell">' +
      '<label class="field-label" for="set-qb-pathmap-from-' + index + '">' + esc(t('set.qb.pathmap.from.label')) + '</label>' +
      '<input class="input mono" type="text" id="set-qb-pathmap-from-' + index + '" data-pathmap-from value="' + esc(from) + '"' +
        ' placeholder="' + esc(t('set.qb.pathmap.from.ph')) + '" autocomplete="off" spellcheck="false">' +
      '<p class="field-hint">' + esc(t('set.qb.pathmap.from.example')) + '</p>' +
    '</div>' +
    '<div class="pathmap-arrow" aria-hidden="true">→</div>' +
    '<div class="pathmap-cell">' +
      '<label class="field-label" for="set-qb-pathmap-to-' + index + '">' + esc(t('set.qb.pathmap.to.label')) + '</label>' +
      '<input class="input mono" type="text" id="set-qb-pathmap-to-' + index + '" data-pathmap-to value="' + esc(to) + '"' +
        ' placeholder="' + esc(t('set.qb.pathmap.to.ph')) + '" autocomplete="off" spellcheck="false">' +
      '<p class="field-hint">' + esc(t('set.qb.pathmap.to.example')) + '</p>' +
    '</div>' +
    '<button type="button" class="btn btn-ghost btn-sm pathmap-remove" data-pathmap-remove' +
      ' aria-label="' + esc(t('set.qb.pathmap.remove')) + '" title="' + esc(t('set.qb.pathmap.remove')) + '">' +
      icon('trash', 14) + '</button>' +
  '</div>';
}

/** 至少渲染一行空行：用户一眼能看到两个输入框的标签和示例。 */
function pathMapRowsHtml(entries) {
  const list = entries && entries.length ? entries : [{ from: '', to: '' }];
  return list.map(pathMapRowHtml).join('');
}

/** DOM → 文本（跳过整行都空的占位行；只填一半的行保留，让后端报第几行）。 */
function pathMapRowsToText(rowsEl) {
  if (!rowsEl) return '';
  const out = [];
  Array.prototype.forEach.call(rowsEl.querySelectorAll('[data-pathmap-row]'), function (row) {
    const from = row.querySelector('[data-pathmap-from]').value.trim();
    const to = row.querySelector('[data-pathmap-to]').value.trim();
    if (!from && !to) return;
    out.push(from + ' => ' + to);
  });
  return out.join('\n');
}

/** 增删行之后重排 id/label 的 for，保证 label 点得到对应输入框。 */
function reindexPathMapRows(rowsEl) {
  if (!rowsEl) return;
  Array.prototype.forEach.call(rowsEl.querySelectorAll('[data-pathmap-row]'), function (row, i) {
    const from = row.querySelector('[data-pathmap-from]');
    const to = row.querySelector('[data-pathmap-to]');
    if (from) from.id = 'set-qb-pathmap-from-' + i;
    if (to) to.id = 'set-qb-pathmap-to-' + i;
    const labels = row.querySelectorAll('label');
    if (labels[0]) labels[0].setAttribute('for', 'set-qb-pathmap-from-' + i);
    if (labels[1]) labels[1].setAttribute('for', 'set-qb-pathmap-to-' + i);
  });
}

function sameValue(a, b) {
  if (Array.isArray(a) || Array.isArray(b)) return arrayToText(a) === arrayToText(b);
  if (typeof a === 'number' || typeof b === 'number') return Number(a) === Number(b);
  return String(a === null || a === undefined ? '' : a) === String(b === null || b === undefined ? '' : b);
}

// ---------------------------------------------------------------- 各区块 HTML

function qbCard(s) {
  const hasPassword = s.qbittorrent_password === '***';
  return html`<section class="card">
    <div class="card-head">
      <h2 class="card-title">${t('set.section.qb')}</h2>
      <div class="card-actions">
        <button type="button" class="btn btn-secondary btn-sm" id="set-qb-test">${raw(icon('chip', 13))}<span>${t('set.qb.test')}</span></button>
      </div>
    </div>
    <div class="card-body">
      <div class="form">
        <div class="form-grid">
          ${field({
            id: 'set-qb-url',
            label: t('set.qb.url'),
            control: textInput({ id: 'set-qb-url', value: s.qbittorrent_url || '', placeholder: t('set.qb.url.ph'), autocomplete: 'off' })
          })}
          ${field({
            id: 'set-qb-username',
            label: t('set.qb.username'),
            control: textInput({ id: 'set-qb-username', value: s.qbittorrent_username || '', autocomplete: 'off' })
          })}
          ${field({
            id: 'set-qb-password',
            label: t('set.qb.password'),
            hint: hasPassword ? t('set.qb.password.set') : t('set.qb.password.ph'),
            control: raw(
              '<div class="chip-row">' +
                textInput({ id: 'set-qb-password', type: 'password', value: '', placeholder: t('set.qb.password.ph'), autocomplete: 'new-password' }) +
                '<button type="button" class="btn btn-ghost btn-sm" id="set-qb-password-clear"' + (hasPassword ? '' : ' hidden') + '>' +
                  esc(t('set.qb.password.clear')) + '</button>' +
              '</div>' +
              '<p class="field-hint" id="set-qb-password-clear-state" hidden>' + esc(t('set.qb.password.cleared')) + '</p>'
            )
          })}
        </div>
        <div class="pathmap" id="set-qb-pathmap" data-field="set-qb-pathmap">
          <div class="pathmap-head">
            <span class="pathmap-title">${t('set.qb.pathmap.title')}</span>
            <button type="button" class="btn btn-secondary btn-sm" id="set-qb-pathmap-add">${raw(icon('plus', 13))}<span>${t('set.qb.pathmap.add')}</span></button>
          </div>
          <p class="field-hint">${t('set.qb.pathmap.hint')}</p>
          <div class="pathmap-rows" id="set-qb-pathmap-rows">${raw(pathMapRowsHtml(parsePathMap(s.downloader_path_map)))}</div>
          <p class="field-error" data-error-for="set-qb-pathmap"></p>
        </div>
        <p class="field-hint">${t('set.qb.hint')}</p>
        <p class="callout callout-info" id="set-qb-test-result" hidden></p>
      </div>
    </div>
  </section>`;
}

function scanCard(s) {
  return html`<section class="card">
    <div class="card-head"><h2 class="card-title">${t('set.section.scan')}</h2></div>
    <div class="card-body">
      <div class="form">
        ${field({
          id: 'set-scan-enabled',
          control: switchControl({ id: 'set-scan-enabled', checked: !!s.scan_enabled, label: t('set.scan.enabled') }),
          hint: t('set.scan.enabled.hint')
        })}
        ${field({
          id: 'set-scan-interval',
          label: t('set.scan.interval'),
          hint: t('set.scan.interval.hint'),
          control: numberInput({ id: 'set-scan-interval', value: s.scan_interval_seconds, min: 30, max: 86400, step: 1 })
        })}
      </div>
    </div>
  </section>`;
}

function cfgIntervalCard(s) {
  // 周期同步的真源是 DB 设置键 config_sync_interval_seconds（默认 5，0–86400，0 = 只监视不导入）。
  // env CONFIG_SYNC_INTERVAL_SECONDS 在 v2 无效 ⇒ WebUI 是唯一的修改入口。
  return html`<section class="card">
    <div class="card-head"><h2 class="card-title">${t('set.cfg.interval_card')}</h2></div>
    <div class="card-body">
      <div class="form">
        ${field({
          id: 'set-config-sync-interval',
          label: t('set.cfg.interval'),
          hint: t('set.cfg.interval.hint'),
          control: numberInput({ id: 'set-config-sync-interval', value: s.config_sync_interval_seconds == null ? 5 : s.config_sync_interval_seconds, min: 0, max: 86400, step: 1 })
        })}
      </div>
    </div>
  </section>`;
}

function logCard(s) {
  const options = LOG_LEVELS.map(function (lv) {
    return { value: lv, label: lv };
  });
  return html`<section class="card">
    <div class="card-head"><h2 class="card-title">${t('set.section.log')}</h2></div>
    <div class="card-body">
      <div class="form">
        <div class="form-grid">
          ${field({
            id: 'set-log-level',
            label: t('set.log.level'),
            control: selectInput({ id: 'set-log-level', value: s.log_level || 'INFO', options: options })
          })}
          ${field({
            id: 'set-max-history',
            label: t('set.max_history'),
            hint: t('set.max_history.hint'),
            control: numberInput({ id: 'set-max-history', value: s.max_history, min: 10, max: 100000 })
          })}
          ${field({
            id: 'set-max-task-lines',
            label: t('set.log.max_lines'),
            hint: t('set.log.max_lines.hint'),
            control: numberInput({ id: 'set-max-task-lines', value: s.max_task_log_lines, min: 100, max: 1000000, step: 1 })
          })}
        </div>
        ${field({
          id: 'set-orphan',
          control: switchControl({ id: 'set-orphan', checked: !!s.orphan_cleanup_enabled, label: t('set.orphan_global') }),
          hint: t('set.orphan_global.hint')
        })}
      </div>
    </div>
  </section>`;
}

// 全局重命名后缀白名单 —— 一份白名单，应用于所有媒体库。
function renameExtCard(s) {
  const list = Array.isArray(s.rename_extensions) ? s.rename_extensions : [];
  return html`<section class="card">
    <div class="card-head"><h2 class="card-title">${t('set.section.rename_ext')}</h2></div>
    <div class="card-body">
      <div class="form">
        ${field({
          id: 'set-rename-ext',
          label: t('set.rename_ext'),
          hint: t('set.rename_ext.hint'),
          control: textareaInput({ id: 'set-rename-ext', rows: 4, value: arrayToText(list), placeholder: '.mkv\n.mp4\n.ass' })
        })}
        <p class="field-hint">${t('set.rename_ext.note')}</p>
      </div>
    </div>
  </section>`;
}

// L3：特典文件排除 —— 命中判据的文件**不改名、也不移动**，
// 保留原名留在原位置（早期实现会归入作品目录下的「其他/」，该行为已取消）。
function extrasCard(s) {
  const list = Array.isArray(s.extras_patterns) ? s.extras_patterns : [];
  return html`<section class="card">
    <div class="card-head"><h2 class="card-title">${t('set.section.extras')}</h2></div>
    <div class="card-body">
      <div class="form">
        ${field({
          id: 'set-extras-patterns',
          label: t('set.extras'),
          hint: t('set.extras.hint'),
          control: textareaInput({ id: 'set-extras-patterns', rows: 7, value: arrayToText(list), placeholder: 'NCOP\nNCED\nPreview\nCreditless\nInfo\n予告編\nCM集' })
        })}
        <p class="field-hint">${t('set.extras.note')}</p>
      </div>
    </div>
  </section>`;
}

function hookCard(s) {
  const token = s.hook_token || '';
  const origin = window.location.origin && window.location.origin !== 'null' ? window.location.origin : 'http://<host>:18081';
  const command = token
    ? 'curl -s -X POST "' + origin + '/api/hooks/qbittorrent?token=' + token + '"' +
      ' --data-urlencode "name=%N" --data-urlencode "hash=%I" --data-urlencode "category=%L"' +
      ' --data-urlencode "tags=%G" --data-urlencode "content_path=%F" --data-urlencode "save_path=%D"' +
      ' --data-urlencode "root_path=%R"'
    : '';
  return html`<section class="card">
    <div class="card-head">
      <h2 class="card-title">${t('set.section.hook')}</h2>
      <div class="card-actions">
        <button type="button" class="btn btn-secondary btn-sm" id="set-hook-copy-token"${token ? '' : ' disabled'}>
          ${raw(icon('copy', 13))}<span>${t('set.hook.copy_token')}</span>
        </button>
        <button type="button" class="btn btn-secondary btn-sm" id="set-hook-copy-cmd"${token ? '' : ' disabled'}>
          ${raw(icon('copy', 13))}<span>${t('set.hook.copy')}</span>
        </button>
        <button type="button" class="btn btn-ghost btn-sm" id="set-hook-regen"${token ? '' : ' disabled'}>
          ${raw(icon('refresh', 13))}<span>${t('set.hook.regen')}</span>
        </button>
      </div>
    </div>
    <div class="card-body">
      ${token
        ? html`<p class="field-label">${t('set.hook.token')}</p>
            <p class="token-display mono" id="set-hook-token">${token}</p>
            <p class="field-hint">${t('set.hook.token_plain')}</p>
            <p class="field-hint">${t('set.hook.token_source')}</p>
            <p class="field-label">${t('set.hook.command')}</p>
            <pre class="error-box mono" id="set-hook-command">${command}</pre>
            <p class="field-hint">${t('set.hook.kind_note')}</p>`
        : html`<p class="empty-desc">${t('set.hook.empty')}</p>`}
      <p class="field-hint">${t('set.hook.hint')}</p>
    </div>
  </section>`;
}


/**
 * 重命名日志（§5.1/§5.2）：每库一条 json_cache 日志，来源 `GET /api/system/info` 的 `rename_logs`。
 * v1 的「脚本日志连续性」面板与 `log_continuity` 字段已在 v2 整条删除（PROCESSORS §5.3），不要再渲染它。
 */
function renameLogsCard(info) {
  const logs = info && Array.isArray(info.rename_logs) ? info.rename_logs : [];
  if (!logs.length) {
    return html`<section class="card">
      <div class="card-head"><h2 class="card-title">${t('le.logs.title')}</h2></div>
      <div class="card-body"><p class="empty-desc">${t('le.logs.empty')}</p></div>
    </section>`;
  }
  const rows = logs.map(function (row) {
    const exists = !!row.exists;
    const hasFile = row.log_file !== null && row.log_file !== undefined && String(row.log_file) !== '';
    return '<tr>' +
      '<td data-label="' + esc(t('le.logs.lib')) + '">' +
        '<a class="link strong" href="#/libraries/' + encodeURIComponent(String(row.library_id)) + '/edit">' +
          esc(row.library_name || ('#' + row.library_id)) + '</a></td>' +
      // log_file 为 null / 空 ⇒ 显示「暂无日志」，不留空白单元格。
      '<td data-label="' + esc(t('le.logs.file')) + '">' +
        (hasFile ? textCell(row.log_file, 'mono') : '<span class="muted">' + esc(t('le.logs.file_none')) + '</span>') +
      '</td>' +
      '<td data-label="' + esc(t('set.rename.exists')) + '">' +
        '<span class="badge badge-' + (exists ? 'ok' : 'muted') + '">' +
          esc(t(exists ? 'set.rename.exists_yes' : 'set.rename.exists_no')) + '</span></td>' +
      '<td data-label="' + esc(t('le.logs.entries')) + '">' +
        '<span class="strong">' + esc(String(Number(row.entries || 0))) + '</span></td>' +
      '<td data-label="' + esc(t('le.logs.mtime')) + '">' +
        (row.last_write === null || row.last_write === undefined
          ? '<span class="muted">' + esc(t('le.logs.mtime_none')) + '</span>'
          : textCell(row.last_write)) +
      '</td>' +
      '</tr>';
  }).join('');
  return html`<section class="card">
    <div class="card-head"><h2 class="card-title">${t('le.logs.title')}</h2></div>
    <div class="card-body">
      <p class="field-hint">${t('set.rename.hint')}</p>
    </div>
    <div class="card-body card-body-flush">
      <div class="table-wrap"><table class="table responsive">
        <thead><tr>
          <th>${t('le.logs.lib')}</th><th>${t('le.logs.file')}</th>
          <th>${t('set.rename.exists')}</th><th>${t('le.logs.entries')}</th><th>${t('le.logs.mtime')}</th>
        </tr></thead>
        <tbody>${raw(rows)}</tbody>
      </table></div>
    </div>
  </section>`;
}

function pathMapCard(info) {
  const map = info && info.path_map && info.path_map.container_to_host ? info.path_map.container_to_host : null;
  const keys = map ? Object.keys(map) : [];
  const rows = keys.map(function (k) {
    return '<tr>' +
      '<td data-label="' + esc(t('set.paths.container')) + '">' + textCell(k, 'mono strong') + '</td>' +
      '<td data-label="' + esc(t('set.paths.host')) + '">' + textCell(map[k], 'mono') + '</td>' +
      '</tr>';
  }).join('');
  return html`<section class="card">
    <div class="card-head"><h2 class="card-title">${t('set.section.paths')}</h2></div>
    <div class="card-body${rows ? ' card-body-flush' : ''}">
      ${rows
        ? html`<div class="table-wrap"><table class="table responsive">
            <thead><tr><th>${t('set.paths.container')}</th><th>${t('set.paths.host')}</th></tr></thead>
            <tbody>${raw(rows)}</tbody>
          </table></div>`
        : html`<p class="empty-desc">${t('set.paths.empty')}</p>`}
      <p class="field-hint">${t('set.paths.hint')}</p>
    </div>
  </section>`;
}

function healthCard(health) {
  const checks = health && health.checks ? health.checks : null;
  // 顺序按后端返回的 checks 键序渲染（不写死键列表）：后端加检查项时前端不会漏渲染、也不会把顺序搞漂。
  // HEALTH_LABELS 只作标签兜底字典；认不得的键直接用原始 key 作标签（不静默吞掉）。
  const rows = checks ? Object.keys(checks).map(function (k) {
    const label = HEALTH_LABELS[k] ? t(HEALTH_LABELS[k]) : String(k);
    return '<tr>' +
      '<td data-label="' + esc(t('set.health.name')) + '">' + esc(label) + '</td>' +
      '<td data-label="' + esc(t('set.health.state')) + '">' +
        '<span class="badge badge-' + (checks[k] ? 'ok' : 'danger') + '">' + esc(t(checks[k] ? 'common.ok' : 'common.no')) + '</span>' +
      '</td>' +
      '</tr>';
  }).join('') : '';
  const status = health && health.status ? health.status : t('common.unknown');
  return html`<section class="card">
    <div class="card-head">
      <h2 class="card-title">${t('set.health.title')}</h2>
      <div class="card-actions">
        <span class="badge badge-${health && health.status === 'ok' ? 'ok' : 'warn'}">${status}</span>
      </div>
    </div>
    <div class="card-body${rows ? ' card-body-flush' : ''}">
      ${rows
        ? html`<div class="table-wrap"><table class="table responsive">
            <thead><tr><th>${t('set.health.name')}</th><th>${t('set.health.state')}</th></tr></thead>
            <tbody>${raw(rows)}</tbody>
          </table></div>`
        : html`<p class="empty-desc">${t('common.unknown')}</p>`}
      <p class="field-hint">${t('set.health.hint')}</p>
    </div>
  </section>`;
}

function systemCard(info) {
  if (!info || typeof info !== 'object') {
    return html`<section class="card">
      <div class="card-head"><h2 class="card-title">${t('set.section.system')}</h2></div>
      <div class="card-body"><p class="empty-desc">${t('common.unknown')}</p></div>
    </section>`;
  }
  const ports = info.ports && typeof info.ports === 'object'
    ? (info.ports.container === undefined ? '' : String(info.ports.container)) +
      (info.ports.host === undefined ? '' : ' → ' + info.ports.host) +
      (info.ports.protocol ? ' / ' + info.ports.protocol : '')
    : '';
  const queue = info.queue && typeof info.queue === 'object'
    ? (info.queue.running_task_id ? t('td.title', { id: info.queue.running_task_id }) : t('dash.queue.idle')) +
      ' / ' + t('dash.queue.queued_label') + ' ' + String(info.queue.queued_count === undefined ? 0 : info.queue.queued_count)
    : '';
  const sameFs = info.same_filesystem && typeof info.same_filesystem === 'object' ? info.same_filesystem : null;
  // `downloads_vs_library` 现在是**三态**（true / false / null）。
  // 旧代码写 `? 'ok' : 'danger'`，会把 null 渲染成「跨挂载」——正是误报。
  //
  // 颜色口径（**全产品唯一**，复核后定为四级）：
  //   绿 = 已实证通过；黄 = 无法判定、或「确定但不能阻断」；红 = **只**留给阻断性错误
  //   （例如本页健康检查的 db / data_dir / config 为 false）。
  // 本卡取的是 `GET /api/system/info`，后端在 `_same_filesystem_view()` 里刻意传 `probe=False`
  // （不做实证探测，避免每次打开设置页都去写目标目录）⇒ 这里拿到的 `true` **必然是**「未实证」的
  // 预筛结论（挂载 ID 或 st_dev，相同只是必要不充分条件），所以按四级口径只能是**黄「通过（未实证）」**，
  // 不能显示成绿。`false`（跨挂载）与 `null`（此刻无法实证）也都是非阻断情形 ⇒ 黄。
  const sameFsLabel = sameFs
    ? (sameFs.downloads_vs_library === true ? t('val.same_fs.unverified')
      : (sameFs.downloads_vs_library === false ? t('val.error') : t('val.same_fs.unknown')))
    : '';
  // 本卡出现的每种情形都落在「黄」上；写成常量而非三元链，避免以后有人手滑把 true 改回绿。
  const sameFsTri = sameFs ? 'warn' : '';
  // 后端给出可执行的中文 message（含参与比较的路径与 st_dev）；响应缺 message 字段时退回自拼。
  const sameFsLegacy = sameFs
    ? (sameFs.source_path || '') + ' (st_dev ' + String(sameFs.source_dev) + ') → ' +
      (sameFs.target_path || '') + ' (st_dev ' + String(sameFs.target_dev) + ')'
    : '';
  // `true`（未实证）时后端 message 里已写明「未实证…可能假绿」；message 缺失的老后端退回静态说明。
  const sameFsExtra = sameFs && sameFs.downloads_vs_library === true && !sameFs.message
    ? ' ' + t('val.same_fs.unverified_hint')
    : (sameFs && sameFs.downloads_vs_library === false ? ' ' + t('val.same_fs.false_top') : '');
  // v2 /api/system/info 新增：processors（{total,builtin_ids,items}）/ libraries[] / config（同步快照）。
  const procs = info.processors && typeof info.processors === 'object' ? info.processors : null;
  const procText = procs && procs.total !== undefined
    ? t('set.system.processors.v', {
        total: String(procs.total),
        builtin: String(Array.isArray(procs.builtin_ids) ? procs.builtin_ids.length : 0)
      })
    : '';
  const libText = Array.isArray(info.libraries) ? t('set.system.libraries.v', { total: String(info.libraries.length) }) : '';
  const cfg = info.config && typeof info.config === 'object' ? info.config : null;
  const cfgCell = cfg && cfg.in_sync !== undefined
    ? boolBadge(!!cfg.in_sync, 'set.system.config_sync.ok', 'set.system.config_sync.bad')
    : '';
  // v2 新增 info.scan（{enabled,interval_seconds,last_run_at,next_run_at,running}；时间走 §5.0 第 7 条本地格式）
  const scan = info.scan && typeof info.scan === 'object' ? info.scan : null;
  const scanText = scan
    ? t('set.system.scan.v', {
        state: t(scan.enabled ? 'common.enabled' : 'common.disabled'),
        last: scan.last_run_at ? fmtDateTime(scan.last_run_at) : t('common.none'),
        next: scan.enabled && scan.next_run_at ? fmtDateTime(scan.next_run_at) : t('common.none')
      })
    : '';
  const mounts = Array.isArray(info.mounts) ? info.mounts : [];
  const mountRows = mounts.map(function (m) {
    const cells = MOUNT_COLS.map(function (col) {
      const key = col[0];
      let value = m[key];
      if (key === 'st_dev' && (value === undefined || value === null)) value = '';
      return '<td data-label="' + esc(t(col[1])) + '">' + textCell(value) + '</td>';
    }).join('');
    const flags = '<td data-label="' + esc(t('set.system.mount.flags')) + '">' +
      '<span class="badge badge-' + (m.read_only ? 'info' : 'warn') + '">' + esc(t(m.read_only ? 'set.system.mount.ro' : 'set.system.mount.rw')) + '</span> ' +
      '<span class="badge badge-' + (m.exists === false ? 'danger' : 'ok') + '">' + esc(t(m.exists === false ? 'set.system.mount.missing' : 'set.system.mount.exists')) + '</span>' +
      '</td>';
    return '<tr>' + cells + flags + '</tr>';
  }).join('');

  return html`<section class="card">
    <div class="card-head">
      <h2 class="card-title">${t('set.section.system')}</h2>
    </div>
    <div class="card-body">
      ${raw(kvRows([
        [t('set.system.version'), textCell(info.version, 'mono'), ''],
        [t('set.system.timezone'), textCell(info.timezone, 'mono'), ''],
        [t('set.system.data_dir'), textCell(info.data_dir, 'mono'), ''],
        [t('set.system.config_dir'), textCell(info.config_dir, 'mono'), ''],
        [t('set.system.log_dir'), textCell(info.log_dir, 'mono'), ''],
        [t('set.system.processors'), textCell(procText, 'mono'), ''],
        [t('set.system.libraries'), textCell(libText, 'mono'), ''],
        [t('set.system.config_sync'), cfgCell || '—', ''],
        [t('set.system.scan'), textCell(scanText, 'mono'), ''],
        [t('set.system.ports'), ports ? '<span class="mono">' + esc(ports) + '</span>' : '—', ''],
        [t('set.system.queue'), queue ? '<span class="mono">' + esc(queue) + '</span>' : '—', ''],
        [t('set.system.log_level'), textCell(info.log_level, 'mono'), '']
      ]))}
      ${sameFs
        ? html`<p class="callout callout-${sameFsTri}">
            ${raw(icon(sameFsTri === 'ok' ? 'check' : (sameFsTri === 'warn' ? 'info' : 'alert'), 13))}
            <span>${t('set.system.same_fs')}：${sameFsLabel}</span>
            <span class="muted">${(sameFs.message || sameFsLegacy) + sameFsExtra}</span>
          </p>`
        : raw('')}
      <p class="field-hint">${t('set.system.stdev_hint')}</p>
    </div>
    ${mountRows
      ? html`<div class="card-head"><h3 class="card-title">${t('set.system.mounts')}</h3></div>
          <div class="card-body card-body-flush">
            <div class="table-wrap"><table class="table responsive">
              <thead><tr>
                ${raw(MOUNT_COLS.map(function (col) { return '<th>' + esc(t(col[1])) + '</th>'; }).join(''))}
                <th>${t('set.system.mount.flags')}</th>
              </tr></thead>
              <tbody>${raw(mountRows)}</tbody>
            </table></div>
          </div>`
      : raw('')}
  </section>`;
}

function passwordCard() {
  return html`<section class="card">
    <div class="card-head"><h2 class="card-title">${t('set.section.password')}</h2></div>
    <div class="card-body">
      <div class="form">
        <div class="form-grid">
          ${field({
            id: 'set-pw-old',
            required: true,
            label: t('set.pw.old'),
            control: textInput({ id: 'set-pw-old', type: 'password', autocomplete: 'current-password' })
          })}
          ${field({
            id: 'set-pw-new',
            required: true,
            label: t('set.pw.new'),
            control: textInput({ id: 'set-pw-new', type: 'password', autocomplete: 'new-password' })
          })}
          ${field({
            id: 'set-pw-confirm',
            required: true,
            label: t('set.pw.confirm'),
            control: textInput({ id: 'set-pw-confirm', type: 'password', autocomplete: 'new-password' })
          })}
        </div>
        <div class="action-bar">
          <button type="button" class="btn btn-secondary" id="set-pw-submit">${t('set.pw.submit')}</button>
          <span class="action-hint">${t('set.pw.hint')}</span>
        </div>
      </div>
    </div>
  </section>`;
}

function pageHtml(state) {
  return html`<div class="page-head">
      <div>
        <h1 class="page-title">${t('set.title')}</h1>
        <p class="page-sub">${t('set.subtitle')}</p>
      </div>
      <div class="page-actions">
        <span class="badge badge-warn" id="set-dirty" hidden>${t('set.unsaved')}</span>
        <button type="button" class="btn btn-secondary btn-sm" id="set-refresh" aria-label="${t('common.refresh')}">${raw(icon('refresh', 13))}<span>${t('common.refresh')}</span></button>
        <button type="button" class="btn btn-primary btn-sm" id="set-save" aria-label="${t('set.save')}">${raw(icon('check', 14))}<span>${t('set.save')}</span></button>
      </div>
    </div>
    <div id="set-cfg"></div>
    ${cfgIntervalCard(state.settings)}
    <div class="callout callout-danger" id="set-form-error" hidden>${raw(icon('alert', 14))}<span id="set-form-error-text"></span></div>
    ${qbCard(state.settings)}
    ${scanCard(state.settings)}
    ${logCard(state.settings)}
    ${renameExtCard(state.settings)}
    ${extrasCard(state.settings)}
    ${hookCard(state.settings)}
    ${renameLogsCard(state.info)}
    ${pathMapCard(state.info)}
    ${healthCard(state.health)}
    ${systemCard(state.info)}
    ${passwordCard()}`;
}

// ---------------------------------------------------------------- 渲染

export async function render(container) {
  let alive = true;
  let settings = null;
  let info = null;
  let health = null;
  /** 用户点了「清除已保存密码」：collect() 会提交 qbittorrent_password = null。 */
  let clearPassword = false;

  container.innerHTML = '<div class="page-head">' + skeletonBlock(2) + '</div>' +
    '<div class="card"><div class="card-body">' + skeletonBlock(8) + '</div></div>' +
    '<div class="card"><div class="card-body">' + skeletonBlock(6) + '</div></div>';

  try {
    const res = await Promise.all([api.getSettings(), api.systemInfo()]);
    settings = res[0];
    info = res[1];
  } catch (err) {
    if (!alive) return null;
    container.innerHTML = errorState({
      retryId: 'set-retry-load',
      message: err && err.message ? err.message : t('err.unknown')
    });
    const btn = container.querySelector('#set-retry-load');
    if (btn) btn.addEventListener('click', function () { load(); });
    return null;
  }
  if (!alive) return null;

  try {
    health = await api.systemHealth();
  } catch (err) {
    health = null;
  }
  if (!alive) return null;

  setSettings(settings);
  paint();

  function q(selector) {
    return container.querySelector(selector);
  }

  function paint() {
    clearPassword = false; // 每次重绘都重置「清除密码」意图
    container.innerHTML = pageHtml({ settings: settings, info: info, health: health });
    dirtyShown(false);
    bindForm();
    bindActions();
    mountConfigStatus(q('#set-cfg'));
  }

  async function load() {
    try {
      const res = await Promise.all([api.getSettings(), api.systemInfo()]);
      if (!alive) return;
      settings = res[0];
      info = res[1];
      setSettings(settings);
      try {
        health = await api.systemHealth();
      } catch (err) {
        health = null;
      }
      if (!alive) return;
      paint();
      toast(t('set.reloaded'), 'info');
    } catch (err) {
      /* apiFetch 已提示 */
    }
  }

  function dirtyShown(on) {
    const flag = q('#set-dirty');
    if (flag) flag.hidden = !on;
  }

  function bindForm() {
    container.querySelectorAll('input, select, textarea').forEach(function (el) {
      if (el.closest('#set-pw-old, #set-pw-new, #set-pw-confirm')) return;
      if (el.id && el.id.indexOf('set-pw-') === 0) return;
      el.addEventListener('change', function () { dirtyShown(true); });
      el.addEventListener('input', function () { dirtyShown(true); });
    });
  }

  function collect() {
    const payload = {
      scan_enabled: !!(q('#set-scan-enabled') && q('#set-scan-enabled').checked),
      scan_interval_seconds: Number(q('#set-scan-interval').value),
      orphan_cleanup_enabled: !!(q('#set-orphan') && q('#set-orphan').checked),
      log_level: q('#set-log-level').value,
      max_history: Number(q('#set-max-history').value),
      max_task_log_lines: Number(q('#set-max-task-lines').value),
      config_sync_interval_seconds: Number(q('#set-config-sync-interval').value),
      // 后缀统一小写（后端也做，这里只是让"改了没反应"不至于出在大小写上）。
      rename_extensions: suffixItemsToArray(q('#set-rename-ext').value),
      // L3：正则一行一条 —— 只按行切、不按空格切（正则里可以含空格）。
      extras_patterns: linesToArray(q('#set-extras-patterns').value),
      qbittorrent_url: q('#set-qb-url').value.trim(),
      qbittorrent_username: q('#set-qb-username').value.trim(),
      downloader_path_map: pathMapRowsToText(q('#set-qb-pathmap-rows'))
    };
    const password = q('#set-qb-password').value;
    // null = 显式清空（后端语义）；''/缺省 = 保持原值；非空 = 新密码。
    if (clearPassword) payload.qbittorrent_password = null;
    else if (password !== '') payload.qbittorrent_password = password;
    return payload;
  }

  function diff(payload) {
    const out = {};
    Object.keys(payload).forEach(function (key) {
      if (!sameValue(payload[key], settings[key])) out[key] = payload[key];
    });
    return out;
  }

  /**
   * 客户端校验 —— **乐观提交**：只给即时提示，绝不阻断保存（硬要求，与
   * processor_edit.js / library_edit.js 同款策略）。范围判定（interval ≥ 60 等）
   * 一律以后端为准：后端 PUT /api/settings 类型/范围不符时返回 400 {detail}。
   */
  function validate(payload) {
    container.querySelectorAll('.field').forEach(function (f) {
      f.classList.remove('field-invalid');
      const e = f.querySelector('.field-error');
      if (e) e.textContent = '';
    });
    const mapBox = q('#set-qb-pathmap');
    if (mapBox) {
      mapBox.classList.remove('field-invalid');
      const mapErr = mapBox.querySelector('.field-error');
      if (mapErr) mapErr.textContent = '';
    }
    function hint(id, key) {
      const box = q('[data-field="' + id + '"]');
      if (box) {
        box.classList.add('field-invalid');
        const e = box.querySelector('.field-error');
        if (e) e.textContent = t(key);
      }
    }
    if (String(q('#set-scan-interval').value).trim() === '') hint('set-scan-interval', 'set.err.interval');
    if (String(q('#set-max-history').value).trim() === '') hint('set-max-history', 'set.err.max_history');
    if (String(q('#set-max-task-lines').value).trim() === '') hint('set-max-task-lines', 'set.err.max_lines');
    if (String(q('#set-config-sync-interval').value).trim() === '') hint('set-config-sync-interval', 'set.err.cfg_interval');
    if (payload.rename_extensions.some(function (s) { return !/^\.[a-z0-9][a-z0-9._+-]*$/i.test(s); })) {
      hint('set-rename-ext', 'set.err.rename_ext_format');
    }
    // L3：正则逐条试编译（JS 与 Python 的方言略有差异，这里只挡明显的括号/转义错误；
    // 最终仍以后端 PUT /api/settings 的 400 为准）。
    payload.extras_patterns.forEach(function (line, i) {
      try {
        new RegExp(line);
      } catch (e) {
        const box = q('[data-field="set-extras-patterns"]');
        if (box) {
          box.classList.add('field-invalid');
          const err = box.querySelector('.field-error');
          if (err) err.textContent = t('set.err.extras_regex', { n: i + 1 });
        }
      }
    });
    // 回调路径映射：每行都必须是「左侧 => 右侧」，两侧都以 / 开头（绝对路径）。
    // 同样只提示不阻断；行号按提交文本的物理行算，与后端报错口径一致。
    const mapErrors = [];
    String(payload.downloader_path_map || '').split('\n').forEach(function (line, i) {
      if (!line.trim()) return;
      const idx = line.indexOf('=>');
      const from = idx < 0 ? line.trim() : line.slice(0, idx).trim();
      const to = idx < 0 ? '' : line.slice(idx + 2).trim();
      if (!from || !to) mapErrors.push(t('set.qb.pathmap.err.partial', { n: i + 1 }));
      else if (from.charAt(0) !== '/') mapErrors.push(t('set.qb.pathmap.err.from_abs', { n: i + 1 }));
      else if (to.charAt(0) !== '/') mapErrors.push(t('set.qb.pathmap.err.to_abs', { n: i + 1 }));
    });
    if (mapErrors.length && mapBox) {
      mapBox.classList.add('field-invalid');
      const mapErr = mapBox.querySelector('.field-error');
      if (mapErr) mapErr.textContent = mapErrors.join('；');
    }
  }

  /** 后端错误字段名 → 设置页控件 id；映射不到的（含 ""）进顶部通用错误区，message 原样保留。 */
  const SET_FIELD_MAP = {
    scan_enabled: 'set-scan-enabled',
    scan_interval_seconds: 'set-scan-interval',
    orphan_cleanup_enabled: 'set-orphan',
    log_level: 'set-log-level',
    max_history: 'set-max-history',
    max_task_log_lines: 'set-max-task-lines',
    config_sync_interval_seconds: 'set-config-sync-interval',
    rename_extensions: 'set-rename-ext',
    extras_patterns: 'set-extras-patterns',
    qbittorrent_url: 'set-qb-url',
    qbittorrent_username: 'set-qb-username',
    qbittorrent_password: 'set-qb-password',
    downloader_path_map: 'set-qb-pathmap'
  };

  function applyServerErrors(err) {
    const rawDetail = err && err.detail;
    let list = null;
    if (rawDetail && Array.isArray(rawDetail.detail)) list = rawDetail.detail;
    else if (Array.isArray(rawDetail)) list = rawDetail;
    const host = q('#set-form-error');
    const text = q('#set-form-error-text');
    if (text) text.textContent = '';
    if (!host || !text) return false;
    if (!list || !list.length) {
      host.hidden = true;
      return false;
    }
    const general = [];
    list.forEach(function (item) {
      if (!item) return;
      let field = '';
      if (Array.isArray(item.loc)) {
        const parts = item.loc.filter(function (p) { return p !== 'body' && !/^\d+$/.test(String(p)); });
        field = parts.length ? String(parts[parts.length - 1]) : '';
      } else if (item.field !== undefined) {
        field = String(item.field).split('.').pop();
      }
      const message = String(item.message !== undefined ? item.message : (item.msg || ''));
      const target = SET_FIELD_MAP[field];
      if (target) {
        const box = q('[data-field="' + target + '"]');
        if (box) {
          box.classList.add('field-invalid');
          const e = box.querySelector('.field-error');
          if (e) e.textContent = message;
        } else {
          general.push({ field: field, message: message });
        }
      } else {
        general.push({ field: field, message: message });
      }
    });
    if (general.length) {
      const ul = document.createElement('ul');
      ul.className = 'error-list';
      general.forEach(function (g) {
        const li = document.createElement('li');
        if (g.field) {
          const code = document.createElement('code');
          code.className = 'mono';
          code.textContent = g.field;
          li.appendChild(code);
          li.appendChild(document.createTextNode(' '));
        }
        li.appendChild(document.createTextNode(g.message));
        ul.appendChild(li);
      });
      text.appendChild(ul);
      host.hidden = false;
      return true;
    }
    host.hidden = true;
    return true;
  }

  async function save() {
    const btn = q('#set-save');
    const payload = collect();
    // 乐观提交：客户端校验只给即时提示（见 validate 注释），后端 400/422 才是真源。
    validate(payload);
    const changed = diff(payload);
    if (!Object.keys(changed).length) {
      toast(t('set.no_changes'), 'info');
      dirtyShown(false);
      return;
    }
    btn.disabled = true;
    try {
      const updated = await api.putSettings(changed);
      if (!alive) return;
      settings = updated && typeof updated === 'object' ? updated : settings;
      setSettings(settings);
      paint();
      toast(t('set.saved'), 'success');
    } catch (err) {
      /* apiFetch 已 toast（detail 为字符串时的兜底）；数组形状在这里按字段回显。 */
      applyServerErrors(err);
    } finally {
      if (btn.isConnected) btn.disabled = false;
    }
  }

  async function testConnection() {
    const btn = q('#set-qb-test');
    const box = q('#set-qb-test-result');
    const payload = {};
    const url = q('#set-qb-url').value.trim();
    const username = q('#set-qb-username').value.trim();
    const password = q('#set-qb-password').value;
    if (url) payload.url = url;
    if (username) payload.username = username;
    if (password) payload.password = password;

    btn.disabled = true;
    const old = btn.innerHTML;
    btn.innerHTML = icon('clock', 13) + '<span>' + t('set.qb.testing') + '</span>';
    if (box) {
      box.hidden = false;
      box.className = 'callout callout-info';
      box.textContent = t('set.qb.testing');
    }
    try {
      const res = await api.testQbittorrent(payload);
      if (!alive) return;
      const ok = !!(res && res.ok);
      const latency = res && res.latency_ms !== undefined ? t('set.qb.test_latency', { ms: res.latency_ms }) : '';
      const msg = ok
        ? t('set.qb.test_ok', { version: (res && res.version) || t('common.unknown') }) + (latency ? '（' + latency + '）' : '')
        : t('set.qb.test_fail', { msg: (res && res.message) || t('err.unknown') });
      if (box) {
        box.className = 'callout callout-' + (ok ? 'ok' : 'danger');
        box.textContent = msg;
      }
      toast(msg, ok ? 'success' : 'error');
    } catch (err) {
      if (!alive) return;
      if (box) {
        box.className = 'callout callout-danger';
        box.textContent = t('set.qb.test_fail', { msg: err && err.message ? err.message : t('err.unknown') });
      }
    } finally {
      btn.disabled = false;
      btn.innerHTML = old;
    }
  }

  async function changePassword() {
    const oldPw = q('#set-pw-old');
    const newPw = q('#set-pw-new');
    const confirmPw = q('#set-pw-confirm');
    const btn = q('#set-pw-submit');
    function fail(id, key) {
      const box = q('[data-field="' + id + '"]');
      if (box) {
        box.classList.add('field-invalid');
        const e = box.querySelector('.field-error');
        if (e) e.textContent = t(key);
      }
    }
    if (!oldPw.value) return fail('set-pw-old', 'set.pw.err.old_required');
    if (String(newPw.value).length < 8) return fail('set-pw-new', 'set.pw.err.new_short');
    if (newPw.value !== confirmPw.value) return fail('set-pw-confirm', 'set.pw.err.mismatch');

    btn.disabled = true;
    try {
      await api.authChangePassword(oldPw.value, newPw.value);
      if (!alive) return;
      oldPw.value = '';
      newPw.value = '';
      confirmPw.value = '';
      toast(t('set.pw.ok'), 'success');
    } catch (err) {
      /* apiFetch 已 toast */
    } finally {
      if (btn.isConnected) btn.disabled = false;
    }
  }

  function bindActions() {
    const saveBtn = q('#set-save');
    if (saveBtn) saveBtn.addEventListener('click', save);

    const refreshBtn = q('#set-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', load);

    const testBtn = q('#set-qb-test');
    if (testBtn) testBtn.addEventListener('click', testConnection);

    const clearBtn = q('#set-qb-password-clear');
    if (clearBtn) clearBtn.addEventListener('click', function () {
      clearPassword = !clearPassword;
      const input = q('#set-qb-password');
      if (input) {
        input.value = '';
        input.disabled = clearPassword;
      }
      clearBtn.textContent = t(clearPassword ? 'set.qb.password.clear_undo' : 'set.qb.password.clear');
      const state = q('#set-qb-password-clear-state');
      if (state) state.hidden = !clearPassword;
      dirtyShown(true);
    });

    const pathMapAdd = q('#set-qb-pathmap-add');
    const pathMapRows = q('#set-qb-pathmap-rows');
    if (pathMapAdd && pathMapRows) pathMapAdd.addEventListener('click', function () {
      pathMapRows.insertAdjacentHTML(
        'beforeend',
        pathMapRowHtml({ from: '', to: '' }, pathMapRows.querySelectorAll('[data-pathmap-row]').length)
      );
      reindexPathMapRows(pathMapRows);
      const last = pathMapRows.querySelectorAll('[data-pathmap-row] [data-pathmap-from]');
      if (last.length) last[last.length - 1].focus();
      dirtyShown(true);
    });
    if (pathMapRows) {
      // bindForm() 只在 paint 时跑一次：后加的行靠冒泡补「未保存」标记。
      pathMapRows.addEventListener('input', function () { dirtyShown(true); });
      pathMapRows.addEventListener('change', function () { dirtyShown(true); });
      pathMapRows.addEventListener('click', function (ev) {
        const btn = ev.target.closest('[data-pathmap-remove]');
        if (!btn) return;
        const row = btn.closest('[data-pathmap-row]');
        if (row) row.remove();
        // 删空后补一行空行：标签与示例始终可见（整行空的占位行提交时会被忽略）。
        if (!pathMapRows.querySelector('[data-pathmap-row]')) {
          pathMapRows.insertAdjacentHTML('beforeend', pathMapRowHtml({ from: '', to: '' }, 0));
        }
        reindexPathMapRows(pathMapRows);
        dirtyShown(true);
      });
    }

    const copyToken = q('#set-hook-copy-token');
    if (copyToken) copyToken.addEventListener('click', function () {
      copyText(settings.hook_token || '').then(function (ok) {
        toast(t(ok ? 'common.copied' : 'common.copy_failed'), ok ? 'success' : 'error');
      });
    });

    const copyCmd = q('#set-hook-copy-cmd');
    if (copyCmd) copyCmd.addEventListener('click', function () {
      const pre = q('#set-hook-command');
      copyText(pre ? pre.textContent : '').then(function (ok) {
        toast(t(ok ? 'common.copied' : 'common.copy_failed'), ok ? 'success' : 'error');
      });
    });

    const regenBtn = q('#set-hook-regen');
    if (regenBtn) regenBtn.addEventListener('click', async function () {
      const yes = await confirmDialog({
        title: t('set.hook.regen.title'),
        message: t('set.hook.regen.msg'),
        detail: t('set.hook.regen.detail'),
        confirmText: t('set.hook.regen.ok'),
        cancelText: t('common.cancel'),
        danger: true
      });
      if (!yes || !alive) return;
      regenBtn.disabled = true;
      try {
        // 空串 = 由后端重新随机生成（''/'***'/缺省 的语义与密码字段不同，别混用）。
        const updated = await api.putSettings({ hook_token: '' });
        if (!alive) return;
        settings = updated && typeof updated === 'object' ? updated : settings;
        setSettings(settings);
        paint();
        toast(t('set.hook.regen.done'), 'success');
      } catch (err) {
        /* apiFetch 已 toast */
      } finally {
        if (regenBtn.isConnected) regenBtn.disabled = false;
      }
    });

    const pwBtn = q('#set-pw-submit');
    if (pwBtn) pwBtn.addEventListener('click', changePassword);
  }

  return function cleanup() {
    alive = false;
    // 只释放资源，不清空 DOM：内容由每次渲染负责覆盖，
    // 这样即使清理被更晚的渲染延迟调用，也不会抹掉新页面。
  };
}
