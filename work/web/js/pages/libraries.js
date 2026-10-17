// pages/libraries.js —— 媒体库列表
// 布局（区块顺序）：
//   1) 页头：标题 + 副标题 + 「新建媒体库」按钮
//   2) 规则表格卡片：名称 / 类型 / 处理器 / 启停开关 / 源→目标 / 最近任务 / 操作(执行·作品记录·预检·编辑·删除)
//   3) 空态（无规则时）
//   4) 弹窗：手动执行（范围）、预检结果、删除二次确认（作品记录见 #/works/<id>）

import { t } from '../i18n.js';
import * as api from '../api.js';
import * as router from '../router.js';
import { setLibraries, getLibrary, processorName } from '../store.js';
import {
  html, raw, icon, esc, toast, statusBadge, badge, modal, confirmDialog,
  switchControl, field, skeletonTable, emptyState, errorState, fmtDateTime, kindLabel
} from '../ui.js';

function asList(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === 'string' && value) {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
  }
  return [];
}

function processorLabel(processorId) {
  return processorName(processorId) || t('common.unknown');
}

/**
 * 处理器单元格（v2 响应字段）：
 * 后端在库对象里直接给 processor_name / processor_kind / processor_missing，
 * 优先用它们（注册表未加载或规则被删也显示得出来）；无这些字段时退回 store 的注册表查询。
 */
function processorCell(lib) {
  if (!lib.processor_id) return '—';
  const name = lib.processor_name || processorLabel(lib.processor_id);
  const kind = lib.processor_kind ? ' · ' + kindLabel(lib.processor_kind) : '';
  if (lib.processor_missing) {
    return esc(name) + kind +
      ' <span class="badge badge-danger">' + esc(t('lib.processor.missing_badge')) + '</span>';
  }
  return esc(name) + kind;
}

/** 提交给后端时带 LibraryInput 的 12 个字段（§5 + v2：processor_id、log_file）。 */
function toPayload(lib) {
  return {
    name: lib.name,
    type: lib.type,
    processor_id: lib.processor_id,
    enabled: !!lib.enabled,
    source_dir: lib.source_dir,
    target_dir: lib.target_dir,
    match_categories: asList(lib.match_categories),
    match_tags: asList(lib.match_tags),
    match_path_prefixes: asList(lib.match_path_prefixes),
    orphan_cleanup: !!lib.orphan_cleanup,
    permission_mode: !!lib.permission_mode,
    log_file: lib.log_file || ''
  };
}

/**
 * 最近一次任务信息（契约已冻结，单一形态）：
 * stats = {last_task_id,last_kind,last_status,last_trigger,last_finished_at,last_error}；无任务时 stats 为 null。
 */
function lastTask(lib) {
  const stats = lib && lib.stats ? lib.stats : null;
  if (!stats || !stats.last_status) return null;
  return {
    id: stats.last_task_id,
    status: stats.last_status,
    kind: stats.last_kind,
    trigger: stats.last_trigger,
    finished_at: stats.last_finished_at,
    error: stats.last_error
  };
}

function lastTaskCell(lib) {
  const last = lastTask(lib);
  if (!last) return html`<span class="muted">${t('lib.last.none')}</span>`;
  const id = last.id;
  return html`<div class="last-task">
    ${raw(statusBadge(last.status))}
    ${id !== undefined && id !== null
      ? html`<a class="mono link" href="#/tasks/${encodeURIComponent(String(id))}">#${id}</a>`
      : raw('')}
    <span class="muted">${fmtDateTime(last.finished_at)}</span>
    ${last.error
      ? html`<span class="badge badge-danger" title="${last.error}">${t('status.failed')}</span>`
      : raw('')}
  </div>`;
}

function rowHtml(lib) {
  const id = lib.id;
  const warnings = asList(lib.warnings);
  return html`<tr data-id="${id}">
    <td data-label="${t('lib.col.name')}">
      <a class="strong link" href="#/libraries/${encodeURIComponent(String(id))}/edit">${lib.name || '—'}</a>
      ${warnings.length
        ? html`<div class="row-warn">${raw(icon('alert', 12))}<span>${warnings.join('；')}</span></div>`
        : raw('')}
    </td>
    <td data-label="${t('lib.col.type')}">${raw(badge(lib.type || '—', 'info'))}</td>
    <td data-label="${t('lib.col.processor')}"><span class="mono">${raw(processorCell(lib))}</span></td>
    <td data-label="${t('lib.col.enabled')}">
      ${raw(switchControl({ id: 'lib-enabled-' + id, checked: !!lib.enabled, label: '' }))}
    </td>
    <td data-label="${t('lib.col.paths')}">
      <div class="path-pair">
        <span class="mono path">${lib.source_dir || '—'}</span>
        <span class="path-arrow">${raw(icon('arrow_right', 13))}</span>
        <span class="mono path">${lib.target_dir || '—'}</span>
      </div>
    </td>
    <td data-label="${t('lib.col.last_task')}" data-cell="last-task">${raw(lastTaskCell(lib))}</td>
    <td data-label="${t('lib.col.actions')}">
      <div class="row-actions">
        <button type="button" class="btn btn-primary btn-sm" data-action="run" data-id="${id}"
          title="${t('lib.action.run')}">${raw(icon('play', 13))}<span>${t('lib.action.run')}</span></button>
        <button type="button" class="btn btn-secondary btn-sm" data-action="validate" data-id="${id}"
          title="${t('lib.action.validate')}">${raw(icon('check', 13))}<span>${t('lib.action.validate')}</span></button>
        <button type="button" class="btn btn-secondary btn-sm" data-action="works" data-id="${id}"
          title="${t('lib.action.works')}">${raw(icon('folder', 13))}<span>${t('lib.action.works')}</span></button>
        <button type="button" class="btn btn-ghost btn-sm" data-action="edit" data-id="${id}"
          title="${t('common.edit')}">${raw(icon('edit', 13))}<span>${t('common.edit')}</span></button>
        <button type="button" class="btn btn-ghost btn-sm btn-danger-text" data-action="delete" data-id="${id}"
          title="${t('lib.action.delete')}">${raw(icon('trash', 13))}<span>${t('lib.action.delete')}</span></button>
      </div>
    </td>
  </tr>`;
}

function listHtml(items) {
  return html`<div class="page-head">
    <div>
      <h1 class="page-title">${t('lib.title')}</h1>
      <p class="page-sub">${t('lib.subtitle')}</p>
    </div>
    <div class="page-actions">
      <button type="button" class="btn btn-secondary btn-sm" id="lib-refresh">
        ${raw(icon('refresh', 14))}<span>${t('common.refresh')}</span>
      </button>
      <a class="btn btn-primary btn-sm" href="#/libraries/new">
        ${raw(icon('plus', 14))}<span>${t('lib.new')}</span>
      </a>
    </div>
  </div>

  ${items.length
    ? html`<section class="card">
        <div class="card-body card-body-flush">
          <div class="table-wrap">
            <table class="table responsive" id="lib-table">
              <thead>
                <tr>
                  <th>${t('lib.col.name')}</th>
                  <th>${t('lib.col.type')}</th>
                  <th>${t('lib.col.processor')}</th>
                  <th>${t('lib.col.enabled')}</th>
                  <th>${t('lib.col.paths')}</th>
                  <th>${t('lib.col.last_task')}</th>
                  <th>${t('lib.col.actions')}</th>
                </tr>
              </thead>
              <tbody>${raw(items.map(rowHtml).join(''))}</tbody>
            </table>
          </div>
        </div>
      </section>`
    : raw(emptyState({
        icon: 'library',
        title: t('lib.empty.title'),
        desc: t('lib.empty.desc'),
        actionLabel: t('lib.new'),
        actionHref: '#/libraries/new'
      }))}`;
}

// ---------------------------------------------------------------- 弹窗

/**
 * 手动执行弹窗。**不渲染整库强制开关**：后端对 `force + 整库范围` 直接 400
 * （整库 force 会让已改过名的文件按原名再链一份、重命名阶段多出 `xxx (2).mkv`，
 * 见 app/routers/libraries.py 的 run_library）。这里只留一条说明指路「作品记录」。
 * `lib.run.force` 这个 i18n 键保留（其它调用方可能还在用），只是本弹窗不渲染它。
 */
function openRunDialog(lib, onDone) {
  let mode = 'full';
  const body =
    '<div class="form">' +
    field({
      id: 'run-scope',
      label: t('lib.run.scope'),
      control: raw(
        '<div class="radio-group">' +
        '<label class="radio"><input type="radio" name="run-mode" value="full" checked><span>' + esc(t('lib.run.mode_full')) + '</span></label>' +
        '<label class="radio"><input type="radio" name="run-mode" value="folder"><span>' + esc(t('lib.run.mode_folder')) + '</span></label>' +
        '</div>'
      )
    }) +
    field({
      id: 'run-folder',
      label: t('lib.run.folder'),
      control: raw('<input class="input" id="run-folder-input" type="text" placeholder="' +
        esc(t('lib.run.folder.ph')) + '" disabled>'),
      hint: t('lib.run.folder.ph')
    }) +
    '<p class="callout callout-warn">' + icon('alert', 13) + '<span>' + esc(t('lib.run.force_note')) + '</span></p>' +
    '<p class="callout callout-info">' + icon('info', 13) + '<span>' + esc(t('lib.kind.hint')) + '</span></p>' +
    '</div>';

  const box = modal({
    title: t('lib.run.title', { name: lib.name }),
    body: body,
    width: 'md',
    closeOnBackdrop: false,
    actions: [
      { label: t('common.cancel'), kind: 'secondary', onClick: function () { box.close(); } },
      {
        label: t('lib.run.submit'),
        kind: 'primary',
        close: false,
        onClick: async function (close) {
          const folderInput = box.root.querySelector('#run-folder-input');
          const folder = (folderInput.value || '').trim();
          if (mode === 'folder' && !folder) {
            box.root.querySelector('[data-field="run-folder"]').classList.add('field-invalid');
            const err = box.root.querySelector('[data-error-for="run-folder"]');
            if (err) err.textContent = t('lib.run.err.empty_folder');
            folderInput.focus();
            return false;
          }
          const scope = mode === 'folder' ? { mode: 'folder', folder: folder } : { mode: 'full' };
          try {
            // force 恒为 false：单作品重做走「作品记录」里的即时操作或删记录，见上面的说明。
            const task = await api.runLibrary(lib.id, scope, false);
            toast(t('lib.run.done', { id: task && task.id !== undefined ? task.id : '?' }), 'success');
            close(task);
            if (typeof onDone === 'function') onDone(task);
          } catch (err) {
            /* apiFetch 已 toast；保持弹窗打开以便修改后重试 */
          }
          return false;
        }
      }
    ]
  });

  box.root.querySelectorAll('input[name="run-mode"]').forEach(function (radio) {
    radio.addEventListener('change', function () {
      mode = radio.value;
      const folderInput = box.root.querySelector('#run-folder-input');
      folderInput.disabled = mode !== 'folder';
      if (mode === 'folder') folderInput.focus();
    });
  });
}

/**
 * 预检结果（§5.0 第 9 条，扁平 **21** 字段；v1 的 `{ok, checks[]}` 已作废。`combination_ok` 是
 * v1 幽灵字段 —— app/ 全域零引用，真源是 `processor_ok`，这里只把它当旧后端容错读）。
 * 判定只看 `errors[]`：非空即失败；`warnings[]` 只是提示。
 *
 * 颜色口径（全产品统一）：**红 = 阻断性错误**（后端 `ok:false` 或 `errors[]` 非空，
 * 这类情形后端自己一定写进 `errors[]`）；**黄 = 无法判定或「确定但不能阻断」**；**绿 = 已实证通过**。
 * 行级同理：`same_filesystem:true` 但 `probe.ok !== true` 是「未实证」⇒ 黄，不能冒充绿；
 * 目标目录不存在只进 `warnings[]` ⇒ 黄「待创建」，不能报红。
 *
 * 顶部总判定 callout：
 *   红 `callout-danger` —— **只**由后端 `ok`/`errors[]` 驱动（后端「必须拦截」的权威判定，前端不另立一套）；
 *   黄 `callout-warn`   —— `ok` 为真但 `same_filesystem === false`：跨挂载是**确定性**问题，
 *                          行上红、顶上绿会自相矛盾；但它**不是**阻断项（运行时只警告、绝不硬拦），
 *                          所以是黄不是红，正文给后端 `probe.note` 原文 + 「硬链接会全部失败、绝不回退复制」；
 *   绿 `callout-ok`     —— 其余情况，含 `same_filesystem === null`（此刻无法实证，任务开始时还会再判一次，
 *                          不该在预检页制造恐慌）与 `same_filesystem:true` 的「未实证」情形
 *                          （后端已把「未实证」写进 `warnings[]`，由下面的警告区如实展示）。
 */
export function openValidateDialog(lib) {
  const box = modal({
    title: t('val.title', { name: lib.name }),
    body: '<div class="card-body">' + esc(t('common.loading')) + '</div>',
    width: 'lg',
    actions: [{ label: t('common.close'), kind: 'secondary', onClick: function () { box.close(); } }]
  });

  api.validateLibrary(lib.id, { probe: true }).then(function (res) {
    box.body.innerHTML = validateHtml(res);
  }).catch(function (err) {
    box.body.innerHTML = '<div class="error-box">' +
      esc(err && err.detail ? err.detail : (err && err.message ? err.message : t('err.unknown'))) + '</div>';
  });
}

function pathPair(value) {
  if (!value || typeof value !== 'object') return '—';
  const src = value.source_path || '—';
  const dst = value.target_path || '—';
  return src + ' → ' + dst;
}

function boolBadge(value) {
  if (value === null || value === undefined) return badge(t('val.none'), 'muted');
  return badge(value ? t('val.pass') : t('val.error'), value ? 'ok' : 'danger');
}

/**
 * 硬链接可用性的**四级**徽章（`same_filesystem` 由 bool 变 `bool | None`）。
 *
 *   true  + probe.ok === true   → 绿「通过」（实证探针真的建成过一次硬链接）
 *   true  + probe.ok !== true   → 黄「通过（未实证）」——结论来自挂载 ID 或 st_dev 预筛，
 *                                 两者相同只是**必要不充分**条件（btrfs 同挂载跨子卷仍可能 EXDEV），
 *                                 所以它不能冒充「已实证通过」。附 `probe.note` 的中文原文。
 *   false                       → 红「未通过」（跨挂载一定 EXDEV；后端此时必写 errors[]），附 note 原文
 *   null/undefined               → 黄「无法判定」——**绝不能**渲染成 true（那正是本次要修的「假绿」）
 *                                 或 false（那会把「还没法判定」误报成故障）
 *
 * 红**只**留给阻断性错误（后端把 `false` 写进 `errors[]`、顶层 `ok=false`）；其余情形一律黄或绿。
 * 注：`undefined`（字段缺失，例如后端未返回该字段）也走「无法判定」而不是「无」，
 * 这样前端的诚实度不依赖后端版本。note 缺失时退到静态文案，保证每一态都有可执行的下一步。
 */
function fsBadge(value, probe) {
  const note = probe && probe.note ? probe.note : null;
  if (value === null || value === undefined) {
    return badge(t('val.same_fs.unknown'), 'warn') +
      ' <span class="muted">' + esc(note || t('val.same_fs.unknown_hint')) + '</span>';
  }
  if (!value) {
    return badge(t('val.error'), 'danger') +
      ' <span class="muted">' + esc(note || t('val.same_fs.bad_hint')) + '</span>';
  }
  if (!probe || probe.ok !== true) {
    return badge(t('val.same_fs.unverified'), 'warn') +
      ' <span class="muted">' + esc(note || t('val.same_fs.unverified_hint')) + '</span>';
  }
  return badge(t('val.pass'), 'ok');
}

/**
 * 目录存在性的徽章。`source_exists` 为 false 后端会写 `errors[]`（源目录不存在时扫描会直接
 * 报错）⇒ 走 `boolBadge` 的红；而**目标目录**不存在后端只写 `warnings[]`
 * （运行任务时会自动创建）⇒ 这里必须是黄「待创建」，红只留给阻断性错误。
 */
function targetDirBadge(value) {
  if (value === null || value === undefined) return badge(t('val.none'), 'muted');
  if (value) return badge(t('val.pass'), 'ok');
  return badge(t('val.target.pending'), 'warn') +
    ' <span class="muted">' + esc(t('val.target.pending_hint')) + '</span>';
}

function validateRows(res) {
  const rows = [
    { label: t('val.source_dir'), value: '<span class="mono">' + esc(res.source_dir || '—') + '</span>' },
    { label: t('val.target_dir'), value: '<span class="mono">' + esc(res.target_dir || '—') + '</span>' },
    { label: t('val.source_exists'), value: boolBadge(res.source_exists) },
    // 目标目录不存在是**非阻断**的（后端只进 warnings[]）⇒ 黄，不用红。
    { label: t('val.target_exists'), value: targetDirBadge(res.target_exists) },
    { label: t('val.target_dir_exists'), value: targetDirBadge(res.target_dir_exists) },
    // 四级口径 —— 只有 probe.ok === true 才是绿「通过」；其余 true 是黄「通过（未实证）」。
    { label: t('val.same_filesystem'), value: fsBadge(res.same_filesystem, res.probe) },
    { label: t('val.same_filesystem_paths'), value: '<span class="mono">' + esc(pathPair(res.same_filesystem_paths)) + '</span>' },
    {
      label: t('val.source_folders'),
      value: res.source_folders === null || res.source_folders === undefined
        ? badge(t('val.none'), 'muted')
        : '<span class="mono">' + esc(String(res.source_folders)) + '</span>'
    },
    // 后端把这一项从 combination_ok 改名为 processor_ok（值 = 处理器存在 + 日志文件合法）；
    // 容错读取旧键，避免响应缺新键时渲染成「无」。
    {
      label: t('val.combination_ok'),
      value: boolBadge(res.processor_ok === undefined ? res.combination_ok : res.processor_ok)
    }
  ];
  if (res.probe !== undefined) {
    const probe = res.probe;
    if (probe === null) {
      rows.push({ label: t('val.probe'), value: badge(t('val.none'), 'muted') });
    } else {
      // probe.ok 也可能是 null（无法实证）：同样不能用 `!!probe.ok` 折成「未通过」。
      const okBadge = probe.ok === null || probe.ok === undefined
        ? badge(t('val.same_fs.unknown'), 'warn')
        : boolBadge(!!probe.ok);
      rows.push({
        label: t('val.probe'),
        value: okBadge + (probe.note
          ? ' <span class="muted">' + esc(probe.note) + '</span>'
          : '')
      });
    }
  }
  return rows;
}

function strList(title, items, kind) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) return '';
  return '<p class="callout callout-' + kind + '">' + icon(kind === 'danger' ? 'alert' : 'info', 13) +
    '<span class="strong">' + esc(title) + '</span></p>' +
    '<ul class="limit-list">' + list.map(function (item) {
      return '<li>' + esc(typeof item === 'string' ? item : JSON.stringify(item)) + '</li>';
    }).join('') + '</ul>';
}

function validateHtml(res) {
  const errors = Array.isArray(res && res.errors) ? res.errors : [];
  // 红色**只**由后端 ok/errors[] 驱动；前端不把其它字段升级成「预检未通过」。
  const failed = !res || !res.ok || errors.length > 0;
  const rows = validateRows(res || {});

  let cls = 'callout-ok';
  let iconName = 'check';
  let title = t('val.ok');
  let extra = '';
  if (failed) {
    cls = 'callout-danger';
    iconName = 'alert';
    title = t('val.fail');
  } else if (res.same_filesystem === false) {
    // 跨挂载：确定性失败但不是阻断项 ⇒ 顶部降为黄，并给出可执行的下一步（后端 note 原文优先）。
    cls = 'callout-warn';
    iconName = 'info';
    const note = res.probe && res.probe.note ? res.probe.note : t('val.same_fs.bad_hint');
    extra = ' <span class="muted">' + esc(note) + ' ' + esc(t('val.same_fs.false_top')) + '</span>';
  }
  // 注意：`same_filesystem === null` 保持绿色 —— 「此刻无法实证」不是故障。

  return '<p class="callout ' + cls + '">' +
    icon(iconName, 13) +
    '<span>' + esc(title) + '</span>' + extra + '</p>' +
    strList(t('val.errors'), res && res.errors, 'danger') +
    strList(t('val.warnings'), res && res.warnings, 'warn') +
    '<div class="table-wrap"><table class="table checks-table"><thead><tr>' +
    '<th>' + esc(t('val.item')) + '</th><th>' + esc(t('val.value')) + '</th></tr></thead><tbody>' +
    rows.map(function (row) {
      return '<tr><td class="strong">' + esc(row.label) + '</td><td>' + row.value + '</td></tr>';
    }).join('') +
    '</tbody></table></div>';
}

async function doToggle(lib, checked, reload) {
  try {
    await api.updateLibrary(lib.id, Object.assign(toPayload(lib), { enabled: checked }));
    toast(t(checked ? 'lib.toggled_on' : 'lib.toggled_off', { name: lib.name }), 'success');
    reload();
  } catch (err) {
    toast(t('lib.toggle_failed', { msg: err && err.message ? err.message : t('err.unknown') }), 'error');
    reload();
  }
}

async function doDelete(lib, reload) {
  const ok = await confirmDialog({
    title: t('lib.delete.title'),
    message: t('lib.delete.body', { name: lib.name }),
    detail: t('lib.custom_warn'),
    confirmText: t('lib.delete.confirm'),
    danger: true
  });
  if (!ok) return;
  try {
    await api.deleteLibrary(lib.id);
    toast(t('lib.deleted', { name: lib.name }), 'success');
    reload();
  } catch (err) {
    /* apiFetch 已 toast */
  }
}

// ---------------------------------------------------------------- 页面

export async function render(container) {
  let alive = true;
  let pollTimer = null;

  // 「最近任务」里还会继续变化的状态：只在这两种状态下才需要自动刷新。
  const ACTIVE_STATUSES = ['queued', 'running'];

  function hasActiveTask(items) {
    return (items || []).some(function (lib) {
      const last = lastTask(lib);
      return !!last && ACTIVE_STATUSES.indexOf(last.status) !== -1;
    });
  }

  function stopPoll() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  /** 有任务在排队/执行就排一次刷新；任务结束后停表，不需要用户手动刷新页面。 */
  function schedulePoll(items) {
    stopPoll();
    if (!alive || !hasActiveTask(items)) return;
    pollTimer = setTimeout(pollLastTasks, 3000);
  }

  /** 只更新「最近任务」列，不整页重绘（避免滚动位置与弹窗状态被打断）。 */
  async function pollLastTasks() {
    pollTimer = null;
    if (!alive) return;
    let items;
    try {
      const res = await api.listLibraries();
      items = res.items || [];
    } catch (err) {
      // 服务暂时不可用：不打扰用户，稍后再试。
      if (alive) pollTimer = setTimeout(pollLastTasks, 5000);
      return;
    }
    if (!alive) return;
    setLibraries(items);
    const tbody = container.querySelector('#lib-table tbody');
    if (tbody) {
      items.forEach(function (lib) {
        const tr = tbody.querySelector('tr[data-id="' + String(lib.id) + '"]');
        const cell = tr ? tr.querySelector('td[data-cell="last-task"]') : null;
        if (cell) cell.innerHTML = String(lastTaskCell(lib));
      });
    }
    schedulePoll(items);
  }

  async function load() {
    container.innerHTML = '<div class="page-head"><div><h1 class="page-title">' + t('lib.title') + '</h1>' +
      '<p class="page-sub">' + t('lib.subtitle') + '</p></div></div>' + skeletonTable(4, 6);
    try {
      const res = await api.listLibraries();
      if (!alive) return;
      setLibraries(res.items);
      container.innerHTML = listHtml(res.items);
      bind();
      schedulePoll(res.items);
    } catch (err) {
      if (!alive) return;
      container.innerHTML = errorState({
        retryId: 'lib-retry',
        message: err && err.message ? err.message : t('err.unknown')
      });
      const btn = container.querySelector('#lib-retry');
      if (btn) btn.addEventListener('click', load);
    }
  }

  function findLib(id) {
    const num = Number(id);
    return getLibrary(Number.isNaN(num) ? id : num);
  }

  function bind() {
    const refresh = container.querySelector('#lib-refresh');
    if (refresh) refresh.addEventListener('click', load);

    const tbody = container.querySelector('#lib-table tbody');
    if (!tbody) return;

    tbody.addEventListener('click', function (ev) {
      const btn = ev.target.closest('[data-action]');
      if (!btn) return;
      const lib = findLib(btn.dataset.id);
      if (!lib) return;
      const action = btn.dataset.action;
      if (action === 'edit') {
        router.navigate('#/libraries/' + encodeURIComponent(String(lib.id)) + '/edit');
      } else if (action === 'run') {
        openRunDialog(lib, function () { load(); });
      } else if (action === 'validate') {
        openValidateDialog(lib);
      } else if (action === 'works') {
        // 作品记录改成独立页（支持搜索与翻页），这里只负责跳过去
        router.navigate('#/works/' + encodeURIComponent(String(lib.id)));
      } else if (action === 'delete') {
        doDelete(lib, load);
      }
    });

    tbody.addEventListener('change', function (ev) {
      const input = ev.target.closest('input[type="checkbox"]');
      if (!input) return;
      const tr = input.closest('tr');
      if (!tr) return;
      const lib = findLib(tr.dataset.id);
      if (!lib) return;
      doToggle(lib, input.checked, load);
    });
  }

  await load();

  return function cleanup() {
    alive = false;
    stopPoll();
    // 只释放资源，不清空 DOM：内容由每次渲染负责覆盖，
    // 这样即使清理被更晚的渲染延迟调用，也不会抹掉新页面。
  };
}
