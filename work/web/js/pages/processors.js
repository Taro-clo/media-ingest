// processors.js —— 处理器（重命名规则）列表页（hash 路由 #/processors）
//
// 契约：docs/contracts/PROCESSORS.md §3（ProcessorSpec）、§7（GET/POST/DELETE /api/processors）、§8（前端增量）
//   GET    /api/processors        → {items, total, limit, offset}（可用 ?builtin= &kind= 过滤）
//   POST   /api/processors        → 201 + 完整 ProcessorSpec，body {id, name, kind, template}
//   DELETE /api/processors/{id}   → 内置 409；被媒体库引用 409 + 引用清单（中文 detail 原样展示）
//
// 页面结构（自上而下）：
//   1) 页头：标题 + 副标题 + 刷新 / 新建处理器
//   2) 配置同步状态条（config_sync.js）
//   3) 处理器表格：名称 / 标识 / 处理方式 / 来源 / 被引用的媒体库 / 操作（编辑、删除）
//   4) 空状态（内置处理器缺失时给出解释）

import { t } from '../i18n.js';
import * as api from '../api.js';
import * as router from '../router.js';
import {
  raw, esc, icon, toast, modal, confirmDialog, badge, emptyState, errorState, loadingCard,
  field, textInput, selectInput, setFieldError, clearFieldErrors
} from '../ui.js';
import { mountConfigStatus } from '../config_sync.js';
import { state, setProcessors, setLibraries } from '../store.js';
import { validProcessorId, namingSummary } from '../processors_util.js';

/** 后端 detail（字符串 / FastAPI 422 数组 / 对象）→ 中文文本。 */
function detailText(err) {
  const d = err && err.detail;
  if (typeof d === 'string') return d;
  if (d && typeof d.detail === 'string') return d.detail;
  if (d && Array.isArray(d.detail)) {
    return d.detail.map(function (x) {
      return (x && x.msg) ? x.msg : String(x);
    }).join('；');
  }
  return (err && err.message) || t('err.unknown');
}

function refsOf(procId) {
  const libs = Array.isArray(state.libraries) ? state.libraries : [];
  return libs.filter(function (l) { return l && l.processor_id === procId; });
}

function refsCell(proc) {
  const refs = refsOf(proc.id);
  if (!refs.length) return '<span class="muted">' + esc(t('proc.refs.none')) + '</span>';
  return refs.map(function (l) {
    return '<a class="link" href="#/libraries/' + encodeURIComponent(l.id) + '/edit">' + esc(l.name) + '</a>';
  }).join('、');
}

function kindBadge(kind) {
  return badge(kind === 'rename' ? t('proc.kind.rename') : t('proc.kind.hardlink_only'), 'muted');
}

function rowHtml(proc) {
  const summary = namingSummary(proc);
  return '<tr>' +
    '<td data-label="' + esc(t('proc.col.name')) + '">' +
      '<a class="link strong" href="#/processors/' + encodeURIComponent(proc.id) + '">' + esc(proc.name || proc.id) + '</a>' +
      (proc.description ? '<div class="muted">' + esc(proc.description) + '</div>' : '') +
    '</td>' +
    '<td data-label="' + esc(t('proc.col.id')) + '"><span class="mono">' + esc(proc.id) + '</span></td>' +
    '<td data-label="' + esc(t('proc.col.kind')) + '">' + kindBadge(proc.kind) +
      (proc.enabled === false ? ' ' + badge(t('common.disabled'), 'warn') : '') + '</td>' +
    '<td data-label="' + esc(t('proc.col.builtin')) + '">' +
      (proc.builtin ? badge(t('proc.builtin.yes'), 'info') : badge(t('proc.builtin.no'), 'muted')) + '</td>' +
    '<td data-label="' + esc(t('proc.col.refs')) + '">' + refsCell(proc) + '</td>' +
    '<td data-label="' + esc(t('proc.col.actions')) + '" class="col-actions">' +
      '<a class="btn btn-secondary btn-sm" href="#/processors/' + encodeURIComponent(proc.id) + '">' + esc(t('proc.edit')) + '</a>' +
      (proc.builtin
        ? '<span class="muted">' + esc(t('proc.delete.builtin')) + '</span>'
        : '<button type="button" class="btn btn-danger-text btn-sm" data-del="' + esc(proc.id) + '">' + esc(t('common.delete')) + '</button>') +
    '</td>' +
    '</tr>' +
    (summary
      ? '<tr class="row-sub"><td colspan="6" data-label="' + esc(t('proc.col.summary')) + '">' +
          '<span class="muted">' + esc(t('proc.col.summary')) + '：</span><span class="mono">' + esc(summary) + '</span>' +
        '</td></tr>'
      : '');
}

function tableHtml(items) {
  return '<div class="table-wrap"><table class="table responsive">' +
    '<thead><tr>' +
      '<th>' + esc(t('proc.col.name')) + '</th>' +
      '<th>' + esc(t('proc.col.id')) + '</th>' +
      '<th>' + esc(t('proc.col.kind')) + '</th>' +
      '<th>' + esc(t('proc.col.builtin')) + '</th>' +
      '<th>' + esc(t('proc.col.refs')) + '</th>' +
      '<th>' + esc(t('proc.col.actions')) + '</th>' +
    '</tr></thead><tbody>' + items.map(rowHtml).join('') + '</tbody>' +
    '</table></div>';
}

// ---------------------------------------------------------------- 新建弹窗

function openCreateDialog(onDone) {
  const kindOfTemplate = { bangumi: 'rename', movie: 'rename', hardlink_only: 'hardlink_only', blank: '' };
  const templateOptions = ['bangumi', 'movie', 'hardlink_only', 'blank'].map(function (v) {
    return { value: v, label: t('proc.create.template.' + v) };
  });
  const body =
    '<div class="form-grid">' +
      field({
        id: 'np-id',
        label: t('proc.create.id'),
        required: true,
        hint: t('proc.create.id.hint'),
        control: textInput({ id: 'np-id', placeholder: t('proc.create.id.ph') })
      }) +
      field({
        id: 'np-name',
        label: t('proc.create.name'),
        required: true,
        control: textInput({ id: 'np-name', placeholder: t('proc.create.name.ph') })
      }) +
    '</div>' +
    field({
      id: 'np-template',
      label: t('proc.create.template'),
      control: selectInput({ id: 'np-template', value: 'bangumi', options: templateOptions })
    }) +
    '<p class="field-hint" id="np-template-desc"></p>' +
    field({
      id: 'np-kind',
      label: t('proc.create.kind'),
      hint: t('proc.create.kind.hint'),
      control: selectInput({
        id: 'np-kind',
        value: 'rename',
        options: [
          { value: 'rename', label: t('proc.kind.rename') },
          { value: 'hardlink_only', label: t('proc.kind.hardlink_only') }
        ]
      })
    });

  const m = modal({
    title: t('proc.create.title'),
    body: body,
    width: 'md',
    actions: [
      { label: t('common.cancel'), kind: 'secondary' },
      { label: t('proc.create.submit'), kind: 'primary', close: false, onClick: submit }
    ]
  });

  const idEl = m.root.querySelector('#np-id');
  const nameEl = m.root.querySelector('#np-name');
  const kindEl = m.root.querySelector('#np-kind');
  const tplEl = m.root.querySelector('#np-template');
  const descEl = m.root.querySelector('#np-template-desc');

  function syncTemplate() {
    const tpl = tplEl.value;
    const kind = kindOfTemplate[tpl];
    if (kind) {
      kindEl.value = kind;
      kindEl.disabled = true;
    } else {
      kindEl.disabled = false;
    }
    descEl.textContent = t('proc.create.template.' + tpl + '.desc');
  }
  tplEl.addEventListener('change', syncTemplate);
  syncTemplate();

  async function submit(close) {
    clearFieldErrors(m.root);
    const id = idEl.value.trim();
    const name = nameEl.value.trim();
    // 乐观提交：客户端只给即时提示，不阻断；后端 422/409 才是真源（同 editor 的硬要求）。
    if (!validProcessorId(id)) setFieldError(m.root, 'np-id', t('proc.create.id_bad'));
    if (!name) setFieldError(m.root, 'np-name', t('proc.create.name_required'));
    try {
      await api.createProcessor({ id: id, name: name, kind: kindEl.value, template: tplEl.value });
      toast(t('proc.create.ok', { name: name }), 'success');
      close(true);
      if (onDone) onDone(id);
    } catch (e) {
      const text = detailText(e);
      const items = (e && e.detail && Array.isArray(e.detail.detail)) ? e.detail.detail
        : (e && e.detail && Array.isArray(e.detail)) ? e.detail : [];
      let mapped = false;
      items.forEach(function (item) {
        if (!item) return;
        const field = String(item.field !== undefined
          ? item.field
          : (Array.isArray(item.loc) ? item.loc.filter(function (p) { return p !== 'body'; }).join('.') : ''));
        const msg = String(item.message !== undefined ? item.message : (item.msg || ''));
        if (field === 'id') { setFieldError(m.root, 'np-id', msg); mapped = true; }
        else if (field === 'name') { setFieldError(m.root, 'np-name', msg); mapped = true; }
        else if (field === 'kind' || field === 'template') { setFieldError(m.root, 'np-kind', msg); mapped = true; }
        else toast(text, 'error', 8000);
      });
      if (e && e.status === 409 && !mapped) setFieldError(m.root, 'np-id', text);
      else if (!mapped) toast(t('proc.create.failed') + '：' + text, 'error', 8000);
    }
  }
}

// ---------------------------------------------------------------- 删除

function openDeleteDialog(proc, onDone) {
  confirmDialog({
    title: t('proc.delete.title', { name: proc.name || proc.id }),
    message: t('proc.delete.msg'),
    detail: t('proc.delete.detail'),
    confirmText: t('common.delete'),
    cancelText: t('common.cancel'),
    danger: true
  }).then(async function (yes) {
    if (!yes) return;
    try {
      await api.deleteProcessor(proc.id);
      toast(t('proc.delete.ok', { name: proc.name || proc.id }), 'success');
      if (onDone) onDone();
    } catch (e) {
      // 409（被引用 / 内置）：把后端的中文原因原样展示出来
      modal({
        title: t('proc.delete.failed'),
        body: '<p>' + esc(t('proc.delete.title', { name: proc.name || proc.id })) + '</p>' +
          '<pre class="error-box">' + esc(detailText(e)) + '</pre>',
        actions: [{ label: t('common.close'), kind: 'primary' }]
      });
    }
  });
}

// ---------------------------------------------------------------- 页面

export async function render(container, params) {
  const alive = { value: true };
  let items = [];

  container.innerHTML = loadingCard(t('common.loading'));

  async function load() {
    const [procs, libs] = await Promise.all([api.listProcessors(), api.listLibraries()]);
    items = Array.isArray(procs.items) ? procs.items : [];
    setProcessors(items);
    setLibraries(libs.items);
  }

  try {
    await load();
  } catch (e) {
    if (!alive.value) return function () {};
    container.innerHTML = errorState({
      title: t('proc.title'),
      message: t('proc.load_failed', { msg: detailText(e) }),
      retryId: 'proc-retry'
    });
    const retry = container.querySelector('#proc-retry');
    if (retry) retry.addEventListener('click', function () { router.reload(); });
    return function () { alive.value = false; };
  }
  if (!alive.value) return function () {};

  function paint() {
    container.innerHTML =
      '<div class="page-head">' +
        '<div>' +
          '<h1>' + esc(t('proc.title')) + '</h1>' +
          '<p class="page-sub">' + esc(t('proc.subtitle')) + '</p>' +
        '</div>' +
        '<div class="page-actions">' +
          '<button type="button" class="btn btn-secondary btn-sm" id="proc-refresh" aria-label="' + esc(t('common.refresh')) + '">' +
            raw(icon('refresh', 13)) + '<span>' + esc(t('common.refresh')) + '</span></button>' +
          '<button type="button" class="btn btn-primary btn-sm" id="proc-new">' +
            raw(icon('plus', 13)) + '<span>' + esc(t('proc.new')) + '</span></button>' +
        '</div>' +
      '</div>' +
      '<div id="proc-cfg"></div>' +
      '<div class="card">' +
        '<div class="card-head"><h2>' + esc(t('proc.title')) + '</h2>' +
          '<div class="card-actions"><span class="muted">' + esc(t('proc.count', { n: items.length })) + '</span></div>' +
        '</div>' +
        (items.length
          ? '<div class="card-body-flush">' + tableHtml(items) + '</div>'
          : '<div class="card-body">' + emptyState({
              icon: 'chip',
              title: t('proc.empty.title'),
              desc: t('proc.empty.desc')
            }) + '</div>') +
      '</div>';

    mountConfigStatus(container.querySelector('#proc-cfg'), { compact: true });

    const refresh = container.querySelector('#proc-refresh');
    if (refresh) {
      refresh.addEventListener('click', async function () {
        refresh.disabled = true;
        try {
          await load();
          paint();
        } catch (e) {
          toast(t('proc.load_failed', { msg: detailText(e) }), 'error');
        }
      });
    }
    const addBtn = container.querySelector('#proc-new');
    if (addBtn) {
      addBtn.addEventListener('click', function () {
        openCreateDialog(function (newId) {
          router.navigate('#/processors/' + encodeURIComponent(newId), true);
        });
      });
    }
    Array.prototype.forEach.call(container.querySelectorAll('[data-del]'), function (btn) {
      const id = btn.getAttribute('data-del');
      const proc = items.filter(function (p) { return p.id === id; })[0];
      if (!proc) return;
      btn.addEventListener('click', function () {
        openDeleteDialog(proc, async function () {
          await load();
          paint();
        });
      });
    });
  }

  paint();

  return function cleanup() {
    alive.value = false;
  };
}
