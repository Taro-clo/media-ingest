// pages/tasks.js —— 任务列表
// 布局（区块顺序）：
//   1) 页头：标题 + 副标题 + 刷新按钮
//   2) 筛选条：状态下拉 / 媒体库下拉 / 搜索框 / 每页条数 / 重置
//   3) 任务表格卡片：ID / 媒体库 / 类型 / 触发 / 状态 / 创建时间 / 耗时 / 操作(详情)
//   4) 分页条：上一页 / 第 x / y 页 / 下一页 / 共 N 条
//   5) 空态 / 错误态
// 筛选状态同步进 hash query（#/tasks?status=failed&q=xxx&page=2），可用浏览器前进后退。

import { t } from '../i18n.js';
import * as api from '../api.js';
import * as router from '../router.js';
import { state, setLibraries, getLibrary } from '../store.js';
import {
  html, raw, icon, esc, statusBadge, kindLabel, triggerLabel, statusLabel,
  fmtDateTime, fmtDuration, durationBetween, skeletonTable, emptyState, errorState, debounce
} from '../ui.js';

const STATUSES = ['queued', 'running', 'success', 'failed', 'skipped', 'cancelled'];
const PAGE_SIZES = [20, 50, 100, 200];
const POLL_MS = 8000;

/**
 * 列表里的「耗时」。优先用执行结果里的毫秒数（result.duration_ms，毫秒精度），
 * 只有在没有结果（未跑完 / 旧记录）时才退回 started_at→finished_at ——
 * 那两个时间戳只到秒，短任务会算成 0 ms，与详情页的 21 ms 对不上。
 */
function taskDuration(task) {
  const r = task && task.result;
  if (r && typeof r.duration_ms === 'number' && isFinite(r.duration_ms) && r.duration_ms >= 0) {
    return fmtDuration(r.duration_ms);
  }
  return durationBetween(task && task.started_at, task && task.finished_at);
}

function statusOptions() {
  const out = [{ value: '', label: t('tasks.filter.all_status') }];
  STATUSES.forEach(function (s) {
    out.push({ value: s, label: statusLabel(s) });
  });
  return out;
}

function libraryOptions(libs) {
  const out = [{ value: '', label: t('tasks.filter.all_libs') }];
  (libs || []).forEach(function (lib) {
    out.push({ value: String(lib.id), label: lib.name || ('#' + lib.id) });
  });
  return out;
}

function filtersHtml(filters, libs) {
  return html`<div class="filter-bar">
    <label class="filter-field">
      <span class="filter-label">${t('tasks.filter.status')}</span>
      <select class="input select" id="f-status">
        ${raw(statusOptions().map(function (o) {
          return '<option value="' + esc(o.value) + '"' + (o.value === filters.status ? ' selected' : '') + '>' +
            esc(o.label) + '</option>';
        }).join(''))}
      </select>
    </label>
    <label class="filter-field">
      <span class="filter-label">${t('tasks.filter.library')}</span>
      <select class="input select" id="f-library">
        ${raw(libraryOptions(libs).map(function (o) {
          return '<option value="' + esc(o.value) + '"' + (o.value === String(filters.library_id)) + '>' +
            esc(o.label) + '</option>';
        }).join(''))}
      </select>
    </label>
    <label class="filter-field filter-grow">
      <span class="filter-label">${t('common.search')}</span>
      <span class="input-search">
        ${raw(icon('search', 14))}
        <input class="input" id="f-q" type="search" value="${filters.q || ''}" placeholder="${t('tasks.search.ph')}">
      </span>
    </label>
    <label class="filter-field">
      <span class="filter-label">${t('tasks.per_page')}</span>
      <select class="input select" id="f-size">
        ${raw(PAGE_SIZES.map(function (n) {
          return '<option value="' + n + '"' + (n === filters.limit ? ' selected' : '') + '>' + n + '</option>';
        }).join(''))}
      </select>
    </label>
    <button type="button" class="btn btn-secondary btn-sm" id="f-reset" aria-label="${t('common.reset')}">
      ${raw(icon('close', 13))}<span>${t('common.reset')}</span>
    </button>
  </div>`;
}

function rowHtml(task) {
  const id = task.id;
  return html`<tr data-id="${id}">
    <td data-label="${t('tasks.col.id')}"><a class="mono link" href="#/tasks/${encodeURIComponent(String(id))}">#${id}</a></td>
    <td data-label="${t('tasks.col.library')}">${task.library_name || '—'}</td>
    <td data-label="${t('tasks.col.kind')}">${kindLabel(task.kind)}</td>
    <td data-label="${t('tasks.col.trigger')}">${triggerLabel(task.trigger)}</td>
    <td data-label="${t('tasks.col.status')}">${raw(statusBadge(task.status))}</td>
    <td data-label="${t('tasks.col.created')}" class="muted">${fmtDateTime(task.created_at)}</td>
    <td data-label="${t('tasks.col.duration')}" class="muted">${taskDuration(task)}</td>
    <td data-label="${t('tasks.col.actions')}">
      <a class="btn btn-ghost btn-sm" href="#/tasks/${encodeURIComponent(String(id))}">
        ${raw(icon('file', 13))}<span>${t('tasks.detail')}</span>
      </a>
    </td>
  </tr>`;
}

function tableHtml(items, filters, total, libs) {
  const pages = Math.max(1, Math.ceil((total || 0) / filters.limit));
  const page = Math.min(filters.page, pages);
  return html`<div class="page-head">
      <div>
        <h1 class="page-title">${t('tasks.title')}</h1>
        <p class="page-sub">${t('tasks.subtitle')}</p>
      </div>
      <div class="page-actions">
        <button type="button" class="btn btn-secondary btn-sm" id="t-refresh" aria-label="${t('common.refresh')}">
          ${raw(icon('refresh', 14))}<span>${t('common.refresh')}</span>
        </button>
      </div>
    </div>

    ${raw(filtersHtml(filters, libs))}

    ${items.length
      ? html`<section class="card">
          <div class="card-body card-body-flush">
            <div class="table-wrap">
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
                    <th>${t('tasks.col.actions')}</th>
                  </tr>
                </thead>
                <tbody>${raw(items.map(rowHtml).join(''))}</tbody>
              </table>
            </div>
          </div>
          <div class="card-foot pagination">
            <span class="muted">${t('common.total', { n: total })}</span>
            <div class="pager">
              <button type="button" class="btn btn-secondary btn-sm" id="p-prev"${page <= 1 ? ' disabled' : ''}>
                ${raw(icon('back', 13))}<span>${t('common.prev')}</span>
              </button>
              <span class="pager-info">${t('common.page_info', { page: page, pages: pages })}</span>
              <button type="button" class="btn btn-secondary btn-sm" id="p-next"${page >= pages ? ' disabled' : ''}>
                <span>${t('common.next')}</span>${raw(icon('arrow_right', 13))}
              </button>
            </div>
          </div>
        </section>`
      : raw(emptyState({
          icon: 'tasks',
          title: t('tasks.empty.title'),
          desc: t('tasks.empty.desc'),
          actionLabel: t('tasks.empty.action'),
          actionHref: '#/libraries'
        }))}`;
}

function readFilters(query, libs) {
  const q = query || {};
  let libraryId = q.library_id || '';
  if (libraryId && libs.length && !getLibrary(Number(libraryId))) libraryId = '';
  return {
    status: q.status && STATUSES.indexOf(q.status) >= 0 ? q.status : '',
    library_id: libraryId,
    q: q.q || '',
    limit: PAGE_SIZES.indexOf(Number(q.limit)) >= 0 ? Number(q.limit) : 50,
    page: Math.max(1, Number(q.page) || 1)
  };
}

function filtersToHash(filters) {
  const parts = [];
  if (filters.status) parts.push('status=' + encodeURIComponent(filters.status));
  if (filters.library_id) parts.push('library_id=' + encodeURIComponent(filters.library_id));
  if (filters.q) parts.push('q=' + encodeURIComponent(filters.q));
  if (filters.limit !== 50) parts.push('limit=' + filters.limit);
  if (filters.page > 1) parts.push('page=' + filters.page);
  return '#/tasks' + (parts.length ? '?' + parts.join('&') : '');
}

export async function render(container, params) {
  let alive = true;
  let timer = null;
  let signature = null;

  let libs = state.libraries || [];
  if (!libs.length) {
    try {
      const res = await api.listLibraries();
      libs = res.items;
      setLibraries(libs);
    } catch (e) {
      libs = [];
    }
  }
  if (!alive) return null;

  let filters = readFilters(params && params.query, libs);

  function syncHash() {
    const target = filtersToHash(filters);
    if (window.location.hash !== target) {
      window.history.replaceState(null, '', target);
    }
  }

  function sigOf(items) {
    return items.map(function (x) { return x.id + ':' + x.status; }).join(',');
  }

  async function load(opts) {
    const silent = !!(opts && opts.silent);
    try {
      const res = await api.listTasks({
        status: filters.status || undefined,
        library_id: filters.library_id || undefined,
        q: filters.q || undefined,
        limit: filters.limit,
        offset: (filters.page - 1) * filters.limit
      });
      if (!alive) return;
      // 页号越界（例如筛选变化后）时回到最后一页
      if (!res.items.length && res.total > 0 && filters.page > 1) {
        filters.page = Math.max(1, Math.ceil(res.total / filters.limit));
        load(opts);
        return;
      }
      const sig = sigOf(res.items) + '|' + res.total + '|' + filters.page;
      if (silent && sig === signature) return;
      signature = sig;
      container.innerHTML = tableHtml(res.items, filters, res.total, libs);
      bind();
      syncHash();
    } catch (err) {
      if (!alive || silent) return;
      container.innerHTML = errorState({
        retryId: 't-retry',
        message: err && err.message ? err.message : t('err.unknown')
      });
      const btn = container.querySelector('#t-retry');
      if (btn) btn.addEventListener('click', function () { hardLoad(); });
    }
  }

  function hardLoad() {
    container.innerHTML = skeletonTable(6, 8);
    signature = null;
    load();
  }

  function applyFilter(patch, resetPage) {
    filters = Object.assign({}, filters, patch);
    if (resetPage) filters.page = 1;
    signature = null;
    container.innerHTML = skeletonTable(6, 8);
    load();
  }

  const onSearch = debounce(function (value) {
    applyFilter({ q: value }, true);
  }, 400);

  function bind() {
    const statusSel = container.querySelector('#f-status');
    const libSel = container.querySelector('#f-library');
    const qInput = container.querySelector('#f-q');
    const sizeSel = container.querySelector('#f-size');

    if (statusSel) statusSel.addEventListener('change', function () { applyFilter({ status: statusSel.value }, true); });
    if (libSel) libSel.addEventListener('change', function () { applyFilter({ library_id: libSel.value }, true); });
    if (qInput) qInput.addEventListener('input', function () { onSearch(qInput.value.trim()); });
    if (sizeSel) sizeSel.addEventListener('change', function () { applyFilter({ limit: Number(sizeSel.value) }, true); });

    const reset = container.querySelector('#f-reset');
    if (reset) {
      reset.addEventListener('click', function () {
        filters = { status: '', library_id: '', q: '', limit: 50, page: 1 };
        hardLoad();
      });
    }
    const refresh = container.querySelector('#t-refresh');
    if (refresh) refresh.addEventListener('click', function () { hardLoad(); });

    const prev = container.querySelector('#p-prev');
    if (prev) {
      prev.addEventListener('click', function () {
        if (filters.page > 1) applyFilter({ page: filters.page - 1 });
      });
    }
    const next = container.querySelector('#p-next');
    if (next) {
      next.addEventListener('click', function () {
        applyFilter({ page: filters.page + 1 });
      });
    }
  }

  function startPoll() {
    timer = window.setInterval(function () {
      if (document.hidden) return;
      load({ silent: true });
    }, POLL_MS);
  }

  container.innerHTML = skeletonTable(6, 8);
  await load();
  startPoll();

  return function cleanup() {
    alive = false;
    if (timer) window.clearInterval(timer);
    // 只释放资源，不清空 DOM：内容由每次渲染负责覆盖，
    // 这样即使清理被更晚的渲染延迟调用，也不会抹掉新页面。
  };
}
