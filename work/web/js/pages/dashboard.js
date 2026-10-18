// pages/dashboard.js —— 仪表盘
// 布局（区块顺序）：
//   1) 页头：标题 + 副标题 + 刷新按钮
//   2) 统计卡片组：24h 成功 / 24h 失败 / 队列等待 / 正在运行 / 任务总数 / 媒体库(启用/总数)
//   3) 队列状态卡片：当前任务链接 + 等待数 + 串行提示 + 「查看任务」
//   4) 媒体库概览卡片：总数 / 已启用 + 「管理媒体库」
//   5) 近期任务卡片：表格（ID / 媒体库 / 类型 / 触发 / 状态 / 创建时间 / 耗时）+ 空态 + 「全部任务」
//   6) 待办看板卡片：全局唯一的待办清单（不弹窗、不阻断），可按 状态/种类/媒体库/作品 筛选，
//      每条可展开详情、打钩完成、恢复未完成；打钩只改状态、绝不删除数据。

import { t } from '../i18n.js';
import * as api from '../api.js';
import * as router from '../router.js';
import { setLibraries } from '../store.js';
import {
  html, raw, esc, icon, toast, badge, statusBadge, kindLabel, triggerLabel,
  fmtDateTime, taskDuration, skeletonCards, skeletonTable, emptyState, errorState, modal, debounce
} from '../ui.js';

const POLL_MS = 8000;
const BOARD_PAGE_SIZE = 20;

// 待办看板状态放在模块级：仪表盘每次 load() 都会整块重写 container.innerHTML（8s 轮询也走这条路），
// 若把这些存在 DOM 里，用户刚选的筛选、刚展开的详情会被下一次轮询清掉。
const board = {
  status: 'open',
  kind: '',
  libraryId: '',
  // 按作品筛选：服务端做子串匹配（title/detail/dedupe_key），
  // 所以 total 与翻页都跟着它走 —— 不是"只筛当前这一页"。空串 = 不过滤。
  q: '',
  // 分页：查询必须同时带 limit 与 offset，否则只能看到前 20 条。
  page: 0,
  expanded: new Set(),
  // 刚打钩、但**仍要留在屏幕上**的条目（默认筛选"未完成"时，被打钩的行不许立刻消失，
  // 要等用户翻页、手动刷新或重新打开页面时才落下去）。键是待办 id，值是打钩后的那条记录。
  pinned: new Map(),
  summary: null,
  items: null,
  rowsHtml: '',
  filterSig: ''
};

const SEVERITY_BADGE = { error: 'danger', warning: 'warn', info: 'info' };

function severityKind(severity) {
  return SEVERITY_BADGE[severity] || 'muted';
}

function boardQuery() {
  return {
    status: board.status,
    kind: board.kind,
    library_id: board.libraryId,
    q: board.q,
    limit: BOARD_PAGE_SIZE,
    offset: board.page * BOARD_PAGE_SIZE
  };
}

function optionHtml(pairs, current) {
  const want = current === undefined || current === null ? '' : String(current);
  return pairs.map(function (pair) {
    const value = String(pair[0]);
    return '<option value="' + esc(value) + '"' + (value === want ? ' selected' : '') + '>' +
      esc(pair[1]) + '</option>';
  }).join('');
}

function boardFilterOptions() {
  const summary = board.summary || {};
  const kinds = Array.isArray(summary.by_kind) ? summary.by_kind : [];
  const libs = Array.isArray(summary.libraries) ? summary.libraries : [];
  const kindPairs = [['', t('dash.todo.kind_all')]];
  kinds.forEach(function (entry) {
    if (!entry || !entry.kind) return;
    kindPairs.push([entry.kind, (entry.title || entry.kind) + (entry.open ? '（' + entry.open + '）' : '')]);
  });
  // 当前筛选值可能已不在汇总里（例如该库的条目全被打钩）——补一个占位项，别让下拉框跳回「全部」。
  if (board.kind && !kindPairs.some(function (pair) { return String(pair[0]) === String(board.kind); })) {
    kindPairs.push([board.kind, board.kind]);
  }
  const libPairs = [['', t('dash.todo.library_all')]];
  libs.forEach(function (entry) {
    if (!entry) return;
    const id = entry.library_id === undefined || entry.library_id === null ? '' : String(entry.library_id);
    if (!id) return;
    libPairs.push([id, (entry.library_name || t('dash.todo.source_unknown')) + (entry.open ? '（' + entry.open + '）' : '')]);
  });
  if (board.libraryId && !libPairs.some(function (pair) { return String(pair[0]) === String(board.libraryId); })) {
    libPairs.push([String(board.libraryId), t('dash.todo.source_unknown') + ' #' + board.libraryId]);
  }
  return { kindPairs: kindPairs, libPairs: libPairs };
}

function boardFiltersHtml() {
  const opts = boardFilterOptions();
  return html`<div class="filter-bar" id="dash-todos-filters">
    <label class="filter-field">
      <span class="filter-label">${t('dash.todo.filter.status')}</span>
      <select class="input select" id="dash-todo-status">${raw(optionHtml([
        ['open', t('dash.todo.status.open')],
        ['done', t('dash.todo.status.done')],
        ['all', t('dash.todo.status.all')]
      ], board.status))}</select>
    </label>
    <label class="filter-field">
      <span class="filter-label">${t('dash.todo.filter.kind')}</span>
      <select class="input select" id="dash-todo-kind">${raw(optionHtml(opts.kindPairs, board.kind))}</select>
    </label>
    <label class="filter-field">
      <span class="filter-label">${t('dash.todo.filter.library')}</span>
      <select class="input select" id="dash-todo-library">${raw(optionHtml(opts.libPairs, board.libraryId))}</select>
    </label>
    <label class="filter-field filter-grow">
      <span class="filter-label">${t('dash.todo.filter.work')}</span>
      <span class="input-search">
        ${raw(icon('search', 14))}
        <input class="input" id="dash-todo-q" type="search" value="${board.q || ''}"
          placeholder="${t('dash.todo.filter.work_ph')}" aria-label="${t('dash.todo.filter.work_ph')}">
      </span>
    </label>
    <button type="button" class="btn btn-secondary btn-sm" data-act="board-clear-q" id="dash-todo-q-reset">
      ${raw(icon('close', 13))}<span>${t('dash.todo.filter.work_clear')}</span>
    </button>
    <button type="button" class="btn btn-secondary btn-sm" data-act="board-detect">
      ${raw(icon('search', 14))}<span>${t('dash.todo.detect')}</span>
    </button>
    <button type="button" class="btn btn-secondary btn-sm" data-act="board-refresh">
      ${raw(icon('refresh', 14))}<span>${t('common.refresh')}</span>
    </button>
  </div>`;
}

function boardBadgeHtml() {
  const summary = board.summary || {};
  const openCount = typeof summary.open === 'number' ? summary.open : null;
  if (openCount === null) return '';
  return badge(
    openCount > 0 ? t('dash.todo.open_count', { count: openCount }) : t('dash.todo.all_clear'),
    openCount > 0 ? 'danger' : 'ok'
  );
}

function boardExtraHtml() {
  const summary = board.summary || {};
  const disabled = Array.isArray(summary.disabled_kinds) ? summary.disabled_kinds : [];
  if (!disabled.length) return '';
  return String(html`<p class="callout callout-info">${raw(icon('info', 13))}<span>${t('dash.todo.disabled_hint', { kinds: disabled.join('、') })}</span></p>`);
}

function todoItemDetail(item) {
  const meta = [esc(item.library_name || t('dash.todo.source_unknown'))];
  if (item.task_id !== null && item.task_id !== undefined) meta.push(esc(t('dash.todo.task', { id: item.task_id })));
  if (item.task_kind) meta.push(esc(item.task_kind));
  meta.push(esc(t('dash.todo.occurrences', { count: item.occurrences === undefined ? 0 : item.occurrences })));
  meta.push(esc(t('dash.todo.first_seen', { time: item.first_seen_at || '—' })));
  meta.push(esc(t('dash.todo.last_seen', { time: item.last_seen_at || '—' })));
  if (item.resolved_at) meta.push(esc(t('dash.todo.resolved_at', { time: item.resolved_at })));
  return '<tr class="todo-detail-row"><td colspan="7">' +
    '<div class="muted todo-detail-meta">' + meta.join(' · ') + '</div>' +
    '<pre class="mono todo-detail-text">' + esc(item.detail || t('dash.todo.detail_empty')) + '</pre>' +
    '</td></tr>';
}

/** 行首圆圈：未完成 = 空圈；已完成 = 圈里一个绿勾。打钩不再用汉字按钮。 */
function todoCheckHtml(item) {
  const open = item.status === 'open';
  const label = open ? t('dash.todo.check') : t('dash.todo.uncheck');
  return '<button type="button" class="todo-check' + (open ? '' : ' is-done') + '"' +
    ' data-act="toggle" data-id="' + esc(item.id) + '"' +
    ' aria-pressed="' + (open ? 'false' : 'true') + '"' +
    ' aria-label="' + esc(label) + '" title="' + esc(label) + '">' +
    icon(open ? 'circle' : 'check_circle', 18) + '</button>';
}

/**
 * 当前筛选下"该显示"的条目 = 服务端返回的那一页 + 刚打钩但仍要留在屏幕上的（``board.pinned``）。
 * 同一 id 以 pinned 的那份为准 —— 这样打钩后那一行会**变灰**而不是继续显示成未完成。
 */
function boardVisibleItems(data) {
  const fetched = data && Array.isArray(data.items) ? data.items : [];
  if (board.status !== 'open' || !board.pinned.size) return fetched;
  const merged = fetched.map(function (item) {
    return board.pinned.get(item.id) || item;
  });
  const seen = new Set(merged.map(function (item) { return item.id; }));
  const pinned = [];
  board.pinned.forEach(function (item, id) {
    if (seen.has(id)) return;
    if (board.kind && item.kind !== board.kind) return;
    if (board.libraryId && String(item.library_id) !== String(board.libraryId)) return;
    pinned.push(item);
  });
  return pinned.concat(merged);
}

/** 分页条：上一页 / 下一页 + "第 X / Y 页（共 N 条）"。 */
function boardPagerHtml(data) {
  const total = data && typeof data.total === 'number' ? data.total : 0;
  const pages = Math.max(1, Math.ceil(total / BOARD_PAGE_SIZE));
  const page = Math.min(board.page, pages - 1);
  return '<div class="board-pager">' +
    '<button type="button" class="btn btn-secondary btn-sm" data-act="board-prev"' +
      (page <= 0 ? ' disabled' : '') + '>' + icon('back', 14) +
      '<span>' + esc(t('dash.todo.page.prev')) + '</span></button>' +
    '<span class="muted" id="dash-todos-page">' +
      esc(t('dash.todo.page.info', { page: page + 1, pages: pages, total: total })) + '</span>' +
    '<button type="button" class="btn btn-secondary btn-sm" data-act="board-next"' +
      (page >= pages - 1 ? ' disabled' : '') + '><span>' + esc(t('dash.todo.page.next')) + '</span>' +
      icon('arrow_right', 14) + '</button>' +
    '</div>';
}

/** 在"当前显示的那些条目"里按 id 找一条（行首圆圈要拿它的 kind / library_id / 当前状态）。 */
function findBoardItem(id) {
  const pin = board.pinned.get(id);
  if (pin) return pin;
  const items = board.items && Array.isArray(board.items.items) ? board.items.items : [];
  for (let index = 0; index < items.length; index += 1) {
    if (items[index].id === id) return items[index];
  }
  return null;
}

function todoItemRow(item) {
  const open = item.status === 'open';
  const id = item.id;
  const expanded = board.expanded.has(id);
  const kindBadge = badge(item.kind_title || item.kind || '', open ? severityKind(item.severity) : 'muted');
  const source = [];
  if (item.library_name) source.push(esc(item.library_name));
  if (item.task_id !== null && item.task_id !== undefined) {
    source.push('<a class="mono link" href="#/tasks/' + encodeURIComponent(String(item.task_id)) + '">' +
      esc(t('dash.todo.task', { id: item.task_id })) + '</a>');
  }
  if (!source.length) source.push(esc(t('dash.todo.source_unknown')));
  const seen = item.occurrences === undefined ? 0 : item.occurrences;
  return '<tr class="todo-row' + (open ? '' : ' todo-row-done') + '">' +
    '<td data-label="' + esc(t('dash.todo.col.kind')) + '"><div class="todo-kind-cell">' +
      todoCheckHtml(item) + kindBadge +
      (open ? '' : ' ' + badge(t('dash.todo.status.done'), 'ok')) + '</div></td>' +
    '<td data-label="' + esc(t('dash.todo.col.issue')) + '"><span class="mono">' + esc(item.title || '') + '</span></td>' +
    '<td data-label="' + esc(t('dash.todo.col.source')) + '" class="muted">' + source.join(' · ') + '</td>' +
    '<td data-label="' + esc(t('dash.todo.col.occurrences')) + '">' + esc(t('dash.todo.occurrences', { count: seen })) + '</td>' +
    '<td data-label="' + esc(t('dash.todo.col.first')) + '" class="muted">' + esc(item.first_seen_at || '—') + '</td>' +
    '<td data-label="' + esc(t('dash.todo.col.last')) + '" class="muted">' + esc(item.last_seen_at || '—') + '</td>' +
    '<td data-label="' + esc(t('dash.todo.col.actions')) + '" class="row-actions">' +
      '<button type="button" class="btn btn-secondary btn-sm" data-act="detail" data-id="' + esc(id) + '">' +
        esc(expanded ? t('dash.todo.detail_hide') : t('dash.todo.detail')) + '</button>' +
    '</td></tr>' +
    (expanded ? todoItemDetail(item) : '');
}

function todoRowsHtml(data) {
  const items = boardVisibleItems(data);
  const doneView = board.status === 'done';
  if (!items.length) {
    // 用作品筛选筛空了：说清是"筛出来的空"而不是"本来就没有待办"，否则用户会以为看板坏了。
    const searching = !!board.q;
    return String(emptyState({
      icon: searching ? 'search' : (doneView ? 'check' : 'folder'),
      title: searching
        ? t('dash.todo.empty_search')
        : (doneView ? t('dash.todo.empty_done') : t('dash.todo.empty')),
      desc: searching
        ? t('dash.todo.empty_search_desc', { q: board.q })
        : (doneView ? t('dash.todo.empty_done_desc') : t('dash.todo.empty_desc'))
    })) + boardPagerHtml(data);
  }
  const head = ['kind', 'issue', 'source', 'occurrences', 'first', 'last', 'actions']
    .map(function (key) { return '<th>' + esc(t('dash.todo.col.' + key)) + '</th>'; }).join('');
  const rows = items.map(todoItemRow).join('');
  return '<div class="table-wrap"><table class="table responsive">' +
    '<thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
    boardPagerHtml(data);
}

function boardSectionHtml() {
  const opts = boardFilterOptions();
  board.filterSig = JSON.stringify([opts.kindPairs, opts.libPairs, board.status]);
  return html`<section class="card" id="dash-todos">
    <div class="card-head">
      <h2 class="card-title">${t('dash.todo.title')} <span id="dash-todos-badge">${raw(boardBadgeHtml())}</span></h2>
      <span class="muted">${t('dash.todo.subtitle')}</span>
    </div>
    <div class="card-body">
      ${raw(String(boardFiltersHtml()))}
      <div id="dash-todos-extra">${raw(boardExtraHtml())}</div>
      <div id="dash-todos-list">${raw(board.rowsHtml || skeletonTable(3, 7))}</div>
    </div>
  </section>`;
}

function statCard(opts) {
  const o = opts || {};
  return html`<div class="stat-card${o.kind ? ' stat-' + o.kind : ''}">
    <div class="stat-head">
      <span class="stat-label">${o.label}</span>
      <span class="stat-icon">${raw(icon(o.icon || 'info', 15))}</span>
    </div>
    <div class="stat-value">${o.value === undefined || o.value === null ? '—' : o.value}</div>
    ${o.hint ? html`<div class="stat-hint">${o.hint}</div>` : raw('')}
  </div>`;
}

function recentRow(task) {
  const id = task.id;
  return html`<tr>
    <td data-label="${t('tasks.col.id')}"><a class="mono link" href="#/tasks/${encodeURIComponent(String(id))}">#${id}</a></td>
    <td data-label="${t('tasks.col.library')}">${task.library_name || '—'}</td>
    <td data-label="${t('tasks.col.kind')}">${kindLabel(task.kind)}</td>
    <td data-label="${t('tasks.col.trigger')}">${triggerLabel(task.trigger)}</td>
    <td data-label="${t('tasks.col.status')}">${raw(statusBadge(task.status))}</td>
    <td data-label="${t('tasks.col.created')}" class="muted">${fmtDateTime(task.created_at)}</td>
    <td data-label="${t('tasks.col.duration')}" class="muted">${taskDuration(task)}</td>
  </tr>`;
}

function bodyHtml(data) {
  const tasks = data.tasks || {};
  const libs = data.libraries || {};
  const queue = data.queue || {};
  const recent = Array.isArray(data.recent) ? data.recent : [];
  const runningId = queue.running_task_id;

  return html`<div class="page-head">
    <div>
      <h1 class="page-title">${t('dash.title')}</h1>
      <p class="page-sub">${t('dash.subtitle')}</p>
    </div>
    <div class="page-actions">
      <button type="button" class="btn btn-secondary btn-sm" id="dash-refresh">
        ${raw(icon('refresh', 14))}<span>${t('common.refresh')}</span>
      </button>
    </div>
  </div>

  <div class="stat-grid">
    ${raw(statCard({ label: t('dash.card.success_24h'), value: tasks.success_24h, kind: 'ok', icon: 'check' }))}
    ${raw(statCard({ label: t('dash.card.failed_24h'), value: tasks.failed_24h, kind: 'danger', icon: 'alert' }))}
    ${raw(statCard({ label: t('dash.card.queued'), value: tasks.queued, icon: 'clock' }))}
    ${raw(statCard({ label: t('dash.card.running'), value: tasks.running, kind: 'info', icon: 'play' }))}
    ${raw(statCard({ label: t('dash.card.total'), value: tasks.total, icon: 'tasks' }))}
    ${raw(statCard({
      label: t('dash.libs.title'),
      value: (libs.enabled === undefined ? '—' : libs.enabled) + ' / ' + (libs.total === undefined ? '—' : libs.total),
      hint: t('dash.libs.enabled') + ' / ' + t('dash.libs.total'),
      icon: 'library'
    }))}
  </div>

  <div class="grid-2">
    <section class="card">
      <div class="card-head">
        <h2 class="card-title">${t('dash.queue.title')}</h2>
        <a class="link" href="#/tasks">${t('dash.queue.go')}</a>
      </div>
      <div class="card-body">
        <dl class="kv">
          <dt>${t('dash.queue.running_label')}</dt>
          <dd>${runningId
            ? html`<a class="mono link" href="#/tasks/${encodeURIComponent(String(runningId))}">#${runningId}</a>`
            : html`<span class="muted">${t('dash.queue.idle')}</span>`}</dd>
          <dt>${t('dash.queue.queued_label')}</dt>
          <dd>${queue.queued_count === undefined ? '—' : queue.queued_count}</dd>
        </dl>
        <p class="callout callout-info">${raw(icon('info', 13))}<span>${t('dash.queue.serial_hint')}</span></p>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2 class="card-title">${t('dash.libs.title')}</h2>
        <a class="link" href="#/libraries">${t('dash.libs.manage')}</a>
      </div>
      <div class="card-body">
        <dl class="kv">
          <dt>${t('dash.libs.total')}</dt>
          <dd>${libs.total === undefined ? '—' : libs.total}</dd>
          <dt>${t('dash.libs.enabled')}</dt>
          <dd>${libs.enabled === undefined ? '—' : libs.enabled}</dd>
        </dl>
      </div>
    </section>
  </div>

  <section class="card">
    <div class="card-head">
      <h2 class="card-title">${t('dash.recent.title')}</h2>
      <a class="link" href="#/tasks">${t('dash.recent.view_all')}</a>
    </div>
    <div class="card-body card-body-flush">
      ${recent.length
        ? html`<div class="table-wrap">
            <table class="table responsive">
              <thead>
                <tr>
                  <th>${t('tasks.col.id')}</th>
                  <th>${t('tasks.col.library')}</th>
                  <th>${t('tasks.col.kind')}</th>
                  <th>${t('tasks.col.trigger')}</th>
                  <th>${t('tasks.col.status')}</th>
                  <th>${t('tasks.col.created')}</th>
                  <th>${t('tasks.col.duration')}</th>
                </tr>
              </thead>
              <tbody>${raw(recent.map(recentRow).join(''))}</tbody>
            </table>
          </div>`
        : raw(emptyState({
            icon: 'tasks',
            title: t('dash.recent.empty'),
            desc: t('dash.recent.empty_desc'),
            actionLabel: t('dash.recent.go_libraries'),
            actionHref: '#/libraries'
          }))}
    </div>
  </section>

  ${raw(String(boardSectionHtml()))}`;
}

export async function render(container) {
  let timer = null;
  let alive = true;
  let loading = true;

  container.innerHTML =
    '<div class="page-head"><div><h1 class="page-title">' + t('dash.title') + '</h1>' +
    '<p class="page-sub">' + t('dash.subtitle') + '</p></div></div>' +
    skeletonCards(6) + '<div class="grid-2">' + skeletonTable(3, 2) + skeletonTable(3, 2) + '</div>' +
    skeletonTable(5, 7);

  async function load(opts) {
    const silent = !!(opts && opts.silent);
    try {
      const data = await api.statsOverview();
      if (!alive) return;
      loading = false;
      container.innerHTML = bodyHtml(data);
      bind();
      refreshBoard();
      // 顺带缓存媒体库列表，供任务页筛选使用
      try {
        const libs = await api.listLibraries();
        if (alive) setLibraries(libs.items);
      } catch (e) {
        /* 缓存失败不影响仪表盘 */
      }
    } catch (err) {
      if (!alive) return;
      if (loading || !silent) {
        container.innerHTML = errorState({
          retryId: 'dash-retry',
          message: err && err.message ? err.message : t('err.unknown')
        });
        const btn = container.querySelector('#dash-retry');
        if (btn) btn.addEventListener('click', function () { hardReload(); });
      }
    }
  }

  function hardReload() {
    container.innerHTML = skeletonCards(6) + skeletonTable(5, 7);
    load();
  }

  function bind() {
    const refresh = container.querySelector('#dash-refresh');
    if (refresh) {
      refresh.addEventListener('click', function () {
        toast(t('common.loading'), 'info', 1200);
        load({ silent: true });
      });
    }
    bindBoard();
  }

  let boardSeq = 0;

  // 只重画看板内部（徽标 / 提示 / 筛选栏 / 列表），页面其它部分与整页轮询互不干扰。
  function paintBoard() {
    const section = container.querySelector('#dash-todos');
    if (!section) return;
    const badgeEl = section.querySelector('#dash-todos-badge');
    if (badgeEl) badgeEl.innerHTML = boardBadgeHtml();
    const extraEl = section.querySelector('#dash-todos-extra');
    if (extraEl) extraEl.innerHTML = boardExtraHtml();
    const filtersEl = section.querySelector('#dash-todos-filters');
    if (filtersEl) {
      const opts = boardFilterOptions();
      const sig = JSON.stringify([opts.kindPairs, opts.libPairs, board.status]);
      // 只有选项集真的变了才重建筛选栏，免得每次轮询都把用户正在操作的下拉框换掉。
      if (sig !== board.filterSig) {
        board.filterSig = sig;
        filtersEl.innerHTML = String(boardFiltersHtml());
      }
    }
    paintBoardList();
  }

  function paintBoardList() {
    const listEl = container.querySelector('#dash-todos-list');
    if (!listEl || !board.items) return;
    board.rowsHtml = todoRowsHtml(board.items);
    listEl.innerHTML = board.rowsHtml;
  }

  async function refreshBoard() {
    const section = container.querySelector('#dash-todos');
    if (!section) return;
    const seq = ++boardSeq;
    try {
      const results = await Promise.all([api.listTodos(boardQuery()), api.todoSummary()]);
      if (!alive || seq !== boardSeq || !container.contains(section)) return;
      board.summary = results[1] || null;
      board.items = results[0] || { items: [], total: 0 };
      // 删到最后一页之后（或筛选后页数变少）⇒ 退回最后一页再拉一次，别把用户留在空页上。
      const total = typeof board.items.total === 'number' ? board.items.total : 0;
      const maxPage = Math.max(0, Math.ceil(total / BOARD_PAGE_SIZE) - 1);
      if (board.page > maxPage) {
        board.page = maxPage;
        refreshBoard();
        return;
      }
      paintBoard();
    } catch (err) {
      if (!alive || seq !== boardSeq || !container.contains(section)) return;
      const listEl = container.querySelector('#dash-todos-list');
      if (!listEl) return;
      listEl.innerHTML = String(errorState({
        retryId: 'dash-todos-retry',
        message: err && err.message ? err.message : t('err.unknown')
      }));
      const btn = listEl.querySelector('#dash-todos-retry');
      if (btn) btn.addEventListener('click', function () { refreshBoard(); });
    }
  }

  /**
   * 一键检测：先**只读**地问服务端"哪些未完成待办的实况已经说明不用再管"，
   * 再弹窗让用户逐条勾选确认 —— 默认全选，但用户可以取消勾选（避免把还没解决的问题标掉）。
   * 服务端只支持有"可核对文件路径"的种类；剩下的在弹窗里如实说明，转人工核对。
   */
  function openDetectModal() {
    const query = { limit: 200 };
    if (board.kind) query.kind = board.kind;
    if (board.libraryId) query.library_id = board.libraryId;
    api.detectTodos(query).then(function (report) {
      if (!alive) return;
      const candidates = report && Array.isArray(report.candidates) ? report.candidates : [];
      const checked = report && typeof report.checked === 'number' ? report.checked : 0;
      const manual = report && Array.isArray(report.undetectable_kinds) ? report.undetectable_kinds : [];
      const manualHint = manual.length
        ? t('dash.todo.detect.manual', { kinds: manual.join('、') })
        : '';
      if (!candidates.length) {
        modal({
          title: t('dash.todo.detect.title'),
          body: '<p>' + esc(t('dash.todo.detect.none', { checked: checked })) + '</p>' +
            (manualHint ? '<p class="muted">' + esc(manualHint) + '</p>' : '')
        });
        return;
      }
      const body = document.createElement('div');
      body.className = 'detect-body';
      body.innerHTML =
        '<p class="muted">' + esc(t('dash.todo.detect.summary', { checked: checked, found: candidates.length })) + '</p>' +
        (manualHint ? '<p class="muted">' + esc(manualHint) + '</p>' : '') +
        '<div class="detect-tools">' +
          '<button type="button" class="btn btn-ghost btn-sm" data-detect="all">' +
            esc(t('dash.todo.detect.select_all')) + '</button>' +
          '<button type="button" class="btn btn-ghost btn-sm" data-detect="none">' +
            esc(t('dash.todo.detect.select_none')) + '</button>' +
        '</div>' +
        '<ul class="detect-list">' + candidates.map(function (item) {
          return '<li class="detect-item">' +
            '<label class="detect-label"><input type="checkbox" class="detect-check" value="' +
              esc(item.id) + '" checked><span>' + badge(item.kind_title || item.kind || '', severityKind(item.severity)) +
              '</span><span class="mono detect-title">' + esc(item.title || '') + '</span></label>' +
            '<div class="muted detect-evidence">' + esc(item.detect_evidence || '') + '</div>' +
            '</li>';
        }).join('') + '</ul>';

      const boxes = function () {
        return Array.prototype.slice.call(body.querySelectorAll('.detect-check'));
      };
      const syncConfirm = function () {
        const button = document.getElementById('detect-confirm');
        if (!button) return;
        const selected = boxes().filter(function (box) { return box.checked; }).length;
        button.textContent = t('dash.todo.detect.confirm', { n: selected });
        button.disabled = selected === 0;
      };
      body.addEventListener('click', function (event) {
        const tool = event.target.closest ? event.target.closest('[data-detect]') : null;
        if (!tool) return;
        const want = tool.getAttribute('data-detect') === 'all';
        boxes().forEach(function (box) { box.checked = want; });
        syncConfirm();
      });
      body.addEventListener('change', syncConfirm);

      modal({
        title: t('dash.todo.detect.title'),
        body: body,
        actions: [
          { label: t('common.cancel'), kind: 'secondary' },
          {
            id: 'detect-confirm',
            label: t('dash.todo.detect.confirm', { n: candidates.length }),
            kind: 'primary',
            onClick: function (close, bodyEl) {
              const ids = Array.prototype.slice.call(bodyEl.querySelectorAll('.detect-check'))
                .filter(function (box) { return box.checked; })
                .map(function (box) { return Number(box.value); });
              if (!ids.length) {
                toast(t('dash.todo.detect.none_selected'), 'warning');
                return false;
              }
              api.resolveDetectedTodos(ids).then(function (result) {
                const done = result && Array.isArray(result.resolved) ? result.resolved : [];
                const skipped = result && Array.isArray(result.skipped) ? result.skipped.length : 0;
                done.forEach(function (item) { board.pinned.set(Number(item.id), item); });
                toast(t('dash.todo.detect.done', { done: done.length, skipped: skipped }),
                  done.length ? 'success' : 'warning');
                close();
                refreshBoard();
              }).catch(function () {
                // 失败原因已由 apiFetch 统一 toast；弹窗留着，用户可以直接重试。
              });
              return false;   // 等请求回来再关（onClick 返回 false = 先别关）
            }
          }
        ]
      });
    }).catch(function () {
      // 检测接口失败的原因已由 apiFetch 统一 toast 过。
    });
  }

  function bindBoard() {
    const section = container.querySelector('#dash-todos');
    if (!section) return;
    // 输入即查（400ms 防抖，与「作品记录」页同一手感）。筛选栏在 8s 轮询里可能被整块重画，
    // 但那只在 sig 变化时发生（sig 不含 board.q），所以正常打字不会把输入框重建掉。
    const onBoardSearch = debounce(function () {
      board.expanded.clear();
      board.pinned.clear();
      board.page = 0;
      refreshBoard();
    }, 400);
    section.addEventListener('input', function (event) {
      const target = event.target;
      if (!target || target.id !== 'dash-todo-q') return;
      board.q = target.value.trim();
      onBoardSearch();
    });
    // 事件委托：看板内部每 8s 会整体重画，逐个按钮绑定会失效。
    section.addEventListener('change', function (event) {
      const target = event.target;
      if (!target || !target.id) return;
      if (target.id === 'dash-todo-status') board.status = target.value || 'open';
      else if (target.id === 'dash-todo-kind') board.kind = target.value || '';
      else if (target.id === 'dash-todo-library') board.libraryId = target.value || '';
      else return;
      board.expanded.clear();
      // 换筛选 = 重新拉一份，这时候才把打钩的行放下去
      board.pinned.clear();
      board.page = 0;
      refreshBoard();
    });
    section.addEventListener('click', function (event) {
      const btn = event.target.closest ? event.target.closest('[data-act]') : null;
      if (!btn) return;
      const act = btn.getAttribute('data-act');
      if (act === 'board-refresh') {
        board.pinned.clear();   // 手动刷新 = 明确要求"照实况重画"，这时候把打钩的行放下去
        refreshBoard();
        return;
      }
      if (act === 'board-clear-q') {
        // 只清"作品"这一个筛选（另外三个下拉框各自的空选项就是"全部"，不需要额外的重置）。
        board.q = '';
        const input = section.querySelector('#dash-todo-q');
        if (input) {
          input.value = '';
          input.focus();
        }
        board.expanded.clear();
        board.pinned.clear();
        board.page = 0;
        refreshBoard();
        return;
      }
      if (act === 'board-prev' || act === 'board-next') {
        const next = board.page + (act === 'board-next' ? 1 : -1);
        if (next < 0 || next === board.page) return;
        board.page = next;
        board.expanded.clear();
        // 翻页时把打钩的行放下去
        board.pinned.clear();
        refreshBoard();
        return;
      }
      if (act === 'board-detect') {
        openDetectModal();
        return;
      }
      const id = Number(btn.getAttribute('data-id'));
      if (!id) return;
      if (act === 'detail') {
        if (board.expanded.has(id)) board.expanded.delete(id); else board.expanded.add(id);
        paintBoardList();
        return;
      }
      if (act === 'toggle') {
        const current = findBoardItem(id);
        if (!current) return;
        const wasOpen = current.status === 'open';
        btn.disabled = true;
        const call = wasOpen ? api.resolveTodo(id) : api.reopenTodo(id);
        call.then(function (updated) {
          if (wasOpen) {
            // 打钩后钉在屏幕上：这一行立刻变灰，但**先不消失**（翻页/刷新时才落下去）。
            board.pinned.set(id, updated && updated.status
              ? updated
              : Object.assign({}, current, { status: 'done' }));
          } else {
            board.pinned.delete(id);
          }
          toast(wasOpen ? t('dash.todo.resolve_ok') : t('dash.todo.reopen_ok'), 'success');
          return refreshBoard();
        }).catch(function () {
          // 失败原因已由 apiFetch 统一 toast，这里只恢复按钮，让用户能重试。
          btn.disabled = false;
        });
      }
    });
  }

  function startPoll() {
    stopPoll();
    timer = window.setInterval(function () {
      if (document.hidden) return;
      load({ silent: true });
    }, POLL_MS);
  }

  function stopPoll() {
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  await load();
  startPoll();

  return function cleanup() {
    alive = false;
    stopPoll();
    // 只释放资源，不清空 DOM：内容由每次渲染负责覆盖，
    // 这样即使清理被更晚的渲染延迟调用，也不会抹掉新页面。
  };
}
