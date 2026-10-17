// pages/works.js —— 作品记录（每库一份「已执行作品」账本，独立成页）
// 布局（区块顺序）：
//   1) 页头：标题 + 副标题 + 刷新按钮
//   2) 筛选条：媒体库下拉 / 搜索框 / 每页条数 / 重置
//   3) 信息行：命中条数 + 两本账（link_records 与重命名缓存）的差集提示
//   4) 作品表格卡片：勾选 / 作品 / 体积 / 目标是否还在 / 是否已记录重命名 / 记录时间 / 目标路径
//   5) 卡片脚：已选条数 + 「立即重命名」+「删除记录」，以及分页条
//   6) 空态 / 错误态
//
// 筛选状态同步进 hash query（#/works/3?q=xxx&page=2&limit=100），可用浏览器前进后退；
// 搜索与翻页都在**服务端**做（`GET /api/libraries/{id}/works?q=&limit=&offset=`），
// 所以几千个作品也不会把浏览器拖住。
//
// 删除是破坏性的（连带 rmtree 目标目录里的整个作品文件夹），**一级确认**：
//   点「删除记录」→ 弹窗摆出路径/文件数/字节数/非硬链接文件数 → 点「确定删除」即执行
//   （弹窗内容由 dry_run=true 预演填出，那只是**读**，不构成一次"确认"）。

import { t } from '../i18n.js';
import * as api from '../api.js';
import * as router from '../router.js';
import { state, setLibraries } from '../store.js';
import {
  html, raw, icon, esc, badge, modal, toast, fmtDateTime, debounce,
  emptyState, errorState, skeletonTable
} from '../ui.js';

const PAGE_SIZES = [25, 50, 100, 200];
const DEFAULT_LIMIT = 50;
const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];

// ---------------------------------------------------------------- 小工具

/** 人类可读体积（后端给的是字节数：link_records.size / folder_stats.bytes）。 */
function fmtBytes(value) {
  const n = Number(value);
  if (!isFinite(n) || n < 0) return '—';
  if (n === 0) return '0 B';
  let v = n;
  let i = 0;
  while (v >= 1024 && i < BYTE_UNITS.length - 1) {
    v = v / 1024;
    i += 1;
  }
  const digits = i === 0 ? 0 : (v < 10 ? 2 : (v < 100 ? 1 : 0));
  return v.toFixed(digits) + ' ' + BYTE_UNITS[i];
}

/** 三态布尔徽章：null/undefined = 未知（不能折成 false，那会误报成故障）。 */
function boolBadgeOf(value, yesKey, noKey) {
  if (value === null || value === undefined) return badge(t('common.unknown'), 'muted');
  return value ? badge(t(yesKey), 'ok') : badge(t(noKey), 'warn');
}

function libraryOptions(libs, current) {
  return (libs || []).map(function (lib) {
    return '<option value="' + esc(String(lib.id)) + '"' +
      (String(lib.id) === String(current) ? ' selected' : '') + '>' +
      esc(lib.name || ('#' + lib.id)) + '</option>';
  }).join('');
}

// ---------------------------------------------------------------- 片段

function filtersHtml(filters, libs) {
  return html`<div class="filter-bar">
    <label class="filter-field">
      <span class="filter-label">${t('works.filter.library')}</span>
      <select class="input select" id="w-library">
        ${raw(libraryOptions(libs, filters.library_id))}
      </select>
    </label>
    <label class="filter-field filter-grow">
      <span class="filter-label">${t('common.search')}</span>
      <span class="input-search">
        ${raw(icon('search', 14))}
        <input class="input" id="w-q" type="search" value="${filters.q || ''}"
          placeholder="${t('works.search.ph')}" aria-label="${t('works.search.ph')}">
      </span>
    </label>
    <label class="filter-field">
      <span class="filter-label">${t('works.per_page')}</span>
      <select class="input select" id="w-size">
        ${raw(PAGE_SIZES.map(function (n) {
          return '<option value="' + n + '"' + (n === filters.limit ? ' selected' : '') + '>' + n + '</option>';
        }).join(''))}
      </select>
    </label>
    <button type="button" class="btn btn-secondary btn-sm" id="w-reset" aria-label="${t('common.reset')}">
      ${raw(icon('close', 13))}<span>${t('common.reset')}</span>
    </button>
  </div>`;
}

/** 作品行（返回值直接进 innerHTML，所以每个插值都手工 esc()）。 */
function workRow(work) {
  const folder = String(work.folder || '');
  const files = Number(work.target_files) || 0;
  const loose = Number(work.target_loose) || 0;
  const filesLine = t('works.linked', { n: files }) +
    (loose > 0 ? ' · ' + t('works.linked_loose', { n: loose }) : '');
  return '<tr>' +
    '<td data-label="' + esc(t('works.col.check')) + '">' +
      '<input type="checkbox" class="works-check" value="' + esc(folder) + '" aria-label="' + esc(folder) + '">' +
    '</td>' +
    '<td data-label="' + esc(t('works.col.folder')) + '"><span class="strong">' + esc(folder) + '</span></td>' +
    '<td data-label="' + esc(t('works.col.size')) + '">' + esc(fmtBytes(work.size)) +
      '<div class="muted">' + esc(filesLine) + '</div></td>' +
    '<td data-label="' + esc(t('works.col.exists')) + '">' +
      boolBadgeOf(work.target_exists, 'works.exists.yes', 'works.exists.no') + '</td>' +
    '<td data-label="' + esc(t('works.col.rename')) + '">' +
      boolBadgeOf(work.rename_recorded, 'works.rename.yes', 'works.rename.no') + '</td>' +
    '<td data-label="' + esc(t('works.col.updated')) + '">' + esc(fmtDateTime(work.updated_at)) + '</td>' +
    '<td data-label="' + esc(t('works.col.target')) + '">' +
      '<span class="mono path">' + esc(work.target_path || '—') + '</span></td>' +
    '</tr>';
}

/**
 * 信息行：命中条数 + 两本账的差集。差集（rename_missing / rename_only）**只是提示**，
 * 后端刻意不自动修复，所以这里用黄/绿 callout，绝不用红（红 = 阻断性错误）。
 */
function worksInfoHtml(data) {
  const matched = Number(data.total) || 0;
  const total = Number(data.ledger_total);
  const missing = Array.isArray(data.rename_missing) ? data.rename_missing : [];
  const only = Array.isArray(data.rename_only) ? data.rename_only : [];
  const diff = [];
  if (missing.length) diff.push(t('works.rename_missing', { n: missing.length, names: missing.join('、') }));
  if (only.length) diff.push(t('works.rename_only', { n: only.length, names: only.join('、') }));
  const head = t('works.filtered', {
    matched: matched,
    total: isFinite(total) ? total : matched
  }) + ' ' + (diff.length ? t('works.ledger_diff') : t('works.ledger_ok'));
  return '<p class="callout ' + (diff.length ? 'callout-warn' : 'callout-ok') + '">' +
    icon(diff.length ? 'info' : 'check', 13) +
    '<span>' + esc(head) + (diff.length ? '<br>' + esc(diff.join(' ')) : '') + '</span></p>';
}

/**
 * 删除确认框（**只有这一级**）：先把 dry_run 的结果摆出来，点「确定删除」即执行。
 * dry_run 只是**读**（预演清单），不构成一次确认。
 */
function openDeleteConfirm(lib, folders, preview, onDeleted, onCancel) {
  const items = Array.isArray(preview && preview.items) ? preview.items : [];

  const previewHtml = items.map(function (item) {
    const loose = Number(item.loose_files) || 0;
    const notes = [];
    if (item.exists === false) notes.push(t('works.del.not_found'));
    if (item.in_ledger === false) notes.push(t('works.del.not_in_ledger'));
    return '<div class="card-body">' +
      '<div class="strong mono path">' + esc(item.target_path || item.folder) + '</div>' +
      '<div class="muted">' + esc(item.folder) + ' · ' +
        esc(t('works.del.files', { n: Number(item.files) || 0 })) + ' · ' +
        esc(t('works.del.size', { bytes: fmtBytes(item.bytes) })) + ' · ' +
        esc(t('works.del.loose', { n: loose })) + '</div>' +
      (notes.length ? '<div class="muted">' + esc(notes.join(' ')) + '</div>' : '') +
      (loose > 0
        ? '<p class="callout callout-danger">' + icon('alert', 13) + '<span class="strong">' +
          esc(t('works.del.loose_warning', { n: loose })) + '</span></p>'
        : '') +
      '</div>';
  }).join('');

  const body =
    '<p class="callout callout-danger">' + icon('alert', 13) + '<span class="strong">' +
      esc(t('works.del.statement')) + '</span></p>' +
    '<h4 class="strong">' + esc(t('works.del.preview_title')) + '</h4>' +
    previewHtml;

  const box = modal({
    title: t('works.del.title'),
    body: body,
    width: 'lg',
    closeOnBackdrop: false,
    onClose: function () { if (typeof onCancel === 'function') onCancel(); },
    actions: [
      { label: t('common.cancel'), kind: 'secondary', onClick: function () { box.close(); } },
      {
        label: t('works.del.confirm'),
        kind: 'danger',
        id: 'works-del-confirm',
        close: false,
        onClick: async function () {
          const btn = box.root.querySelector('#works-del-confirm');
          if (btn) btn.disabled = true;
          try {
            const res = await api.deleteLibraryWorks(lib.id, folders, { dryRun: false, confirm: true });
            showDeleteResult(box, res, onDeleted);
          } catch (err) {
            /* apiFetch 已 toast；保持确认框，允许用户重试 */
            if (btn) btn.disabled = false;
          }
          return false;
        }
      }
    ]
  });

  return box;
}

/** 真删之后的回执：逐条成功/失败 + 后端 next_run_note，并让作品清单重新加载。 */
function showDeleteResult(box, res, onDeleted) {
  const items = Array.isArray(res && res.items) ? res.items : [];
  const removed = items.filter(function (item) { return item.target_removed; }).length;
  const renameRemoved = Array.isArray(res && res.rename_removed) ? res.rename_removed.length : 0;

  box.body.innerHTML =
    '<p class="callout callout-ok">' + icon('check', 13) + '<span class="strong">' +
      esc(t('works.del.result_title')) + '</span> ' +
      esc(t('works.del.done', { n: removed, m: renameRemoved })) + '</p>' +
    (res && res.next_run_note ? '<p class="muted">' + esc(res.next_run_note) + '</p>' : '') +
    '<div class="table-wrap"><table class="table"><thead><tr><th>' + esc(t('works.col.folder')) +
    '</th><th>' + esc(t('works.col.target')) + '</th><th>' + esc(t('common.actions')) +
    '</th></tr></thead><tbody>' + items.map(function (item) {
      let cell;
      if (item.error) {
        cell = badge(t('works.del.result_error'), 'danger') +
          ' <span class="muted">' + esc(item.error) + '</span>';
      } else if (item.target_removed) {
        cell = badge(t('works.del.result_ok'), 'ok');
      } else {
        cell = badge(t('works.del.result_skipped'), 'muted');
      }
      return '<tr><td class="strong">' + esc(item.folder) + '</td>' +
        '<td><span class="mono path">' + esc(item.target_path || '—') + '</span></td>' +
        '<td>' + cell + '</td></tr>';
    }).join('') + '</tbody></table></div>';

  box.footer.innerHTML = '';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-primary';
  btn.textContent = t('common.close');
  btn.addEventListener('click', function () { box.close(); });
  box.footer.appendChild(btn);

  if (typeof onDeleted === 'function') onDeleted();
}

// ---------------------------------------------------------------- 页面

function readFilters(query, libs, fallbackId) {
  const q = query || {};
  let libraryId = q.library_id || q.id || '';
  const known = (libs || []).some(function (lib) { return String(lib.id) === String(libraryId); });
  if (!known) libraryId = fallbackId ? String(fallbackId) : '';
  return {
    library_id: libraryId,
    q: q.q || '',
    limit: PAGE_SIZES.indexOf(Number(q.limit)) >= 0 ? Number(q.limit) : DEFAULT_LIMIT,
    page: Math.max(1, Number(q.page) || 1)
  };
}

function filtersToHash(filters) {
  const parts = [];
  if (filters.q) parts.push('q=' + encodeURIComponent(filters.q));
  if (filters.limit !== DEFAULT_LIMIT) parts.push('limit=' + filters.limit);
  if (filters.page > 1) parts.push('page=' + filters.page);
  const base = '#/works' + (filters.library_id ? '/' + encodeURIComponent(filters.library_id) : '');
  return base + (parts.length ? '?' + parts.join('&') : '');
}

export async function render(container, params) {
  let alive = true;
  let selected = [];
  let data = null;
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

  if (!libs.length) {
    container.innerHTML = emptyState({
      icon: 'library',
      title: t('works.empty.title'),
      desc: t('works.no_library'),
      actionLabel: t('works.empty.action'),
      actionHref: '#/libraries/new'
    });
    return null;
  }

  const routeId = params && params.id ? decodeURIComponent(String(params.id)) : '';
  let filters = readFilters(params && params.query, libs, routeId || (libs[0] && libs[0].id));

  function rowCheckboxes() {
    return Array.prototype.slice.call(container.querySelectorAll('.works-check'));
  }

  function syncSelection() {
    const boxes = rowCheckboxes();
    const onPage = boxes.filter(function (cb) { return cb.checked; }).map(function (cb) { return cb.value; });
    // 勾选状态跨页保留：本页的勾选结果替换掉同名项，其他页选中的仍然保留
    const pageNames = boxes.map(function (cb) { return cb.value; });
    const kept = selected.filter(function (name) { return pageNames.indexOf(name) < 0; });
    selected = kept.concat(onPage);

    const count = container.querySelector('#works-count');
    if (count) count.textContent = t('works.selected', { n: selected.length });
    const del = container.querySelector('#works-delete');
    const ren = container.querySelector('#works-rename');
    if (del) del.disabled = selected.length === 0;
    if (ren) ren.disabled = selected.length === 0;
    const all = container.querySelector('#works-check-all');
    if (all) {
      all.checked = boxes.length > 0 && onPage.length === boxes.length;
      all.indeterminate = onPage.length > 0 && onPage.length < boxes.length;
    }
  }

  function pageHtml(res) {
    const pages = Math.max(1, Math.ceil((res.total || 0) / filters.limit));
    const page = Math.min(filters.page, pages);
    const from = res.total ? (page - 1) * filters.limit + 1 : 0;
    const to = Math.min(page * filters.limit, res.total || 0);
    const items = res.items || [];
    const lib = { id: filters.library_id, name: res.library_name || ('#' + filters.library_id) };

    return html`<div class="page-head">
      <div>
        <h1 class="page-title">${t('works.page.title')}</h1>
        <p class="page-sub">${t('works.page.subtitle')}</p>
      </div>
      <div class="page-actions">
        <button type="button" class="btn btn-secondary btn-sm" id="w-refresh" aria-label="${t('common.refresh')}">
          ${raw(icon('refresh', 14))}<span>${t('common.refresh')}</span>
        </button>
      </div>
    </div>

    ${raw(filtersHtml(filters, libs))}

    <section class="card">
      <div class="card-body">
        ${raw(worksInfoHtml(res))}
        <div class="muted">${t('works.library_meta', {
          source: res.source_dir || '—',
          target: res.target_dir || '—'
        })}</div>
      </div>
      ${items.length
        ? html`<div class="card-body card-body-flush">
            <div class="table-wrap">
              <table class="table responsive" id="works-table">
                <thead>
                  <tr>
                    <th><input type="checkbox" id="works-check-all" title="${t('works.select_all')}"
                      aria-label="${t('works.select_all')}"></th>
                    <th>${t('works.col.folder')}</th>
                    <th>${t('works.col.size')}</th>
                    <th>${t('works.col.exists')}</th>
                    <th>${t('works.col.rename')}</th>
                    <th>${t('works.col.updated')}</th>
                    <th>${t('works.col.target')}</th>
                  </tr>
                </thead>
                <tbody>${raw(items.map(workRow).join(''))}</tbody>
              </table>
            </div>
          </div>`
        : html`<div class="card-body">
            <p class="muted">${filters.q ? t('works.empty_search', { q: filters.q }) : t('works.empty')}</p>
          </div>`}
      <div class="card-foot pagination">
        <div class="row-actions">
          <span class="muted" id="works-count"></span>
          <button type="button" class="btn btn-secondary btn-sm" id="works-rename" disabled>
            ${raw(icon('refresh', 13))}<span>${t('works.action.rename')}</span>
          </button>
          <button type="button" class="btn btn-danger btn-sm" id="works-delete" disabled>
            ${raw(icon('trash', 13))}<span>${t('works.action.delete')}</span>
          </button>
        </div>
        <div class="pager">
          <span class="muted">${res.total
            ? t('works.range', { from: from, to: to, total: res.total })
            : t('common.total', { n: 0 })}</span>
          <button type="button" class="btn btn-secondary btn-sm" id="w-prev"${page <= 1 ? ' disabled' : ''}>
            ${raw(icon('back', 13))}<span>${t('common.prev')}</span>
          </button>
          <span class="pager-info">${t('common.page_info', { page: page, pages: pages })}</span>
          <button type="button" class="btn btn-secondary btn-sm" id="w-next"${page >= pages ? ' disabled' : ''}>
            <span>${t('common.next')}</span>${raw(icon('arrow_right', 13))}
          </button>
        </div>
      </div>
    </section>`;
  }

  function syncHash() {
    const target = filtersToHash(filters);
    if (window.location.hash !== target) {
      window.history.replaceState(null, '', target);
    }
  }

  function bind(res) {
    const libSel = container.querySelector('#w-library');
    const qInput = container.querySelector('#w-q');
    const sizeSel = container.querySelector('#w-size');
    const all = container.querySelector('#works-check-all');

    if (libSel) {
      libSel.addEventListener('change', function () {
        selected = [];
        filters.library_id = libSel.value;
        filters.page = 1;
        hardLoad();
      });
    }
    if (qInput) qInput.addEventListener('input', function () { onSearch(qInput.value.trim()); });
    if (sizeSel) {
      sizeSel.addEventListener('change', function () { applyFilter({ limit: Number(sizeSel.value) }, true); });
    }
    const reset = container.querySelector('#w-reset');
    if (reset) {
      reset.addEventListener('click', function () {
        filters = { library_id: filters.library_id, q: '', limit: DEFAULT_LIMIT, page: 1 };
        selected = [];
        hardLoad();
      });
    }
    const refresh = container.querySelector('#w-refresh');
    if (refresh) refresh.addEventListener('click', function () { hardLoad(); });

    if (all) {
      all.addEventListener('change', function () {
        rowCheckboxes().forEach(function (cb) { cb.checked = all.checked; });
        syncSelection();
      });
    }
    rowCheckboxes().forEach(function (cb) { cb.addEventListener('change', syncSelection); });
    // 翻页/搜索后恢复跨页保留的勾选
    rowCheckboxes().forEach(function (cb) { cb.checked = selected.indexOf(cb.value) >= 0; });

    const del = container.querySelector('#works-delete');
    if (del) del.addEventListener('click', startDelete);
    const ren = container.querySelector('#works-rename');
    if (ren) ren.addEventListener('click', startRename);

    const prev = container.querySelector('#w-prev');
    if (prev) {
      prev.addEventListener('click', function () {
        if (filters.page > 1) applyFilter({ page: filters.page - 1 });
      });
    }
    const next = container.querySelector('#w-next');
    if (next) {
      next.addEventListener('click', function () {
        const pages = Math.max(1, Math.ceil((res.total || 0) / filters.limit));
        if (filters.page < pages) applyFilter({ page: filters.page + 1 });
      });
    }
    syncSelection();
  }

  async function load(opts) {
    const silent = !!(opts && opts.silent);
    if (!filters.library_id) {
      container.innerHTML = errorState({ message: t('works.need_library') });
      return;
    }
    try {
      const res = await api.listLibraryWorks(filters.library_id, {
        q: filters.q || undefined,
        limit: filters.limit,
        offset: (filters.page - 1) * filters.limit
      });
      if (!alive) return;
      // 页号越界（例如筛选变化、或删掉了本页最后几条）时回到最后一页
      if (!res.items.length && res.total > 0 && filters.page > 1) {
        filters.page = Math.max(1, Math.ceil(res.total / filters.limit));
        load(opts);
        return;
      }
      data = res;
      const sig = (res.items || []).map(function (x) { return x.folder + ':' + x.target_files; }).join(',') +
        '|' + res.total + '|' + filters.page + '|' + filters.limit + '|' + filters.q;
      if (silent && sig === signature) return;
      signature = sig;
      container.innerHTML = pageHtml(res);
      bind(res);
      syncHash();
    } catch (err) {
      if (!alive || silent) return;
      container.innerHTML = errorState({
        retryId: 'w-retry',
        message: err && err.message ? err.message : t('err.unknown')
      });
      const btn = container.querySelector('#w-retry');
      if (btn) btn.addEventListener('click', function () { hardLoad(); });
    }
  }

  function hardLoad() {
    container.innerHTML = skeletonTable(6, 7);
    signature = null;
    load();
  }

  function applyFilter(patch, resetPage) {
    filters = Object.assign({}, filters, patch);
    if (resetPage) filters.page = 1;
    signature = null;
    load();
  }

  const onSearch = debounce(function (value) {
    applyFilter({ q: value }, true);
  }, 400);

  async function startRename() {
    if (!selected.length) {
      toast(t('works.action.none'), 'warning');
      return;
    }
    const btn = container.querySelector('#works-rename');
    if (btn) btn.disabled = true;
    try {
      const task = await api.renameLibraryWorks(filters.library_id, selected.slice());
      toast(t('works.rename.done', { id: task && task.id !== undefined ? task.id : '?' }), 'success');
    } catch (err) {
      /* apiFetch 已 toast */
    }
    syncSelection();
  }

  async function startDelete() {
    if (!selected.length) {
      toast(t('works.action.none'), 'warning');
      return;
    }
    const folders = selected.slice();
    const lib = { id: filters.library_id, name: (data && data.library_name) || ('#' + filters.library_id) };
    const btn = container.querySelector('#works-delete');
    if (btn) btn.disabled = true;
    let preview;
    try {
      // 第一级：只预演，一个字节都不动。确认框里的数字全部来自这次回执。
      preview = await api.deleteLibraryWorks(filters.library_id, folders, { dryRun: true });
    } catch (err) {
      syncSelection();
      return;
    }
    if (btn) btn.disabled = false;
    const restore = function () { syncSelection(); };
    openDeleteConfirm(lib, folders, preview, function () {
      selected = [];
      hardLoad();
    }, restore);
  }

  container.innerHTML = skeletonTable(6, 7);
  await load();

  return function cleanup() {
    alive = false;
    // 只释放资源，不清空 DOM：内容由每次渲染负责覆盖，
    // 这样即使清理被更晚的渲染延迟调用，也不会抹掉新页面。
  };
}
