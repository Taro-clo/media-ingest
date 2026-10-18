// pages/task_detail.js —— 任务详情 + 日志增量轮询
// 布局（区块顺序）：
//   1) 页头：返回链接 + 标题「任务 #id」+ 状态徽章 + 操作（重试 / 取消 / 删除）
//   2) 并排两卡（grid-2）：元信息（媒体库 / 类型 / 触发方式 / 状态 / 执行范围 / 创建·开始·结束 / 耗时）
//      与 执行结果（linked/skipped/link_failed/renamed/rename_failed/orphans_removed/folders_scanned/
//      rename_folders_scanned/kept_unresolved_episode/name_collisions/renamed_with_suffix/
//      displaced_targets/episode_unresolved 计数卡
//      + bytes_linked / duration_ms + samples 折叠明细
//      + samples_truncated 截断提示 + warnings 提示行）
//   3) 失败原因卡片（error 非空时显示）
//   4) 日志卡片：日志筛选下拉 + 暂停滚动 + 「已加载 N 行」+ 日志面板（等宽、级别着色）
// 轮询：运行中每 1.5s 用 after_id 增量拉日志，每 6s 刷新任务本体；结束即停止。

import { t } from '../i18n.js';
import * as api from '../api.js';
import * as router from '../router.js';
import {
  html, raw, icon, esc, toast, statusBadge, kindLabel, triggerLabel, scopeLabel,
  fmtDateTime, fmtDuration, durationBetween, confirmDialog, skeletonBlock, errorState
} from '../ui.js';

const ACTIVE = ['queued', 'running'];

// 契约（ARCHITECTURE §5.0）：Task.result 是「整数计数 + 限量样本」，不是全量数组。
const COUNT_ROWS = [
  { key: 'linked', label: 'td.result.linked', kind: 'ok' },
  { key: 'skipped', label: 'td.result.skipped', kind: 'warn' },
  { key: 'link_failed', label: 'td.result.link_failed', kind: 'danger' },
  { key: 'renamed', label: 'td.result.renamed', kind: 'ok' },
  { key: 'rename_failed', label: 'td.result.rename_failed', kind: 'danger' },
  // 命名引用了集数、但没解析出集数的文件数（保留原文件名、未改名；不是失败）
  { key: 'kept_unresolved_episode', label: 'td.result.kept_unresolved_episode', kind: 'warn' },
  // L4「可见性」+ L2「防丢」：改名时目标文件名已被占用的文件数。
  // **不再覆盖**：体积最大的拿干净名，其余加「 (2)」「 (3)」保留（见下面两行）。
  { key: 'name_collisions', label: 'td.result.name_collisions', kind: 'warn' },
  // L2「防丢」：因为目标名被占用而加了「 (N)」后缀保留的文件数（一个都不丢）。
  { key: 'renamed_with_suffix', label: 'td.result.renamed_with_suffix', kind: 'warn' },
  // L2「防丢」：新来的更大时，目标盘上原有的同名文件被改到「 (N)」让位的文件数。
  { key: 'displaced_targets', label: 'td.result.displaced_targets', kind: 'info' },
  // L4「可见性」：所有没解析出集数的文件数 —— **不论是否因此保留了原名**
  // （命名不引用集数时它们照旧改名）。与 kept_unresolved_episode 是两个不同的口径。
  { key: 'episode_unresolved', label: 'td.result.episode_unresolved', kind: 'warn' },
  { key: 'orphans_removed', label: 'td.result.orphans', kind: 'info' },
  { key: 'folders_scanned', label: 'td.result.folders', kind: 'muted' },
  // v2 起任务结果还带重命名阶段自己扫过的文件夹数（rename_hardlink / rename 任务才有意义）
  { key: 'rename_folders_scanned', label: 'td.result.rename_folders', kind: 'muted' }
];

// samples 的子键 → 明细渲染方式
const SAMPLE_ROWS = [
  { key: 'renamed', label: 'td.result.renamed', format: sampleRenamed },
  { key: 'link_failed', label: 'td.result.link_failed', format: sampleLinkFailed },
  { key: 'orphans_removed', label: 'td.result.orphans', format: sampleOrphan },
  // 明细项本身就是一个"相对媒体库根目录"的路径字符串
  { key: 'kept_unresolved_episode', label: 'td.result.kept_unresolved_episode', format: samplePath },
  // L4「可见性」：冲突样本（对象 {dir,target,previous,current,kind}）与解析失败样本
  // （与上面同样是"相对媒体库根目录"的路径字符串）。
  { key: 'name_collisions', label: 'td.result.name_collisions', format: sampleCollision },
  { key: 'episode_unresolved', label: 'td.result.episode_unresolved', format: samplePath },
  // L2「防丢」：加后缀保留 / 原有文件让位的明细（对象，见下面的格式化函数）。
  { key: 'renamed_with_suffix', label: 'td.result.renamed_with_suffix', format: sampleSuffixed },
  { key: 'displaced_targets', label: 'td.result.displaced_targets', format: sampleDisplaced }
];

// 服务端的样本数组不再截断 —— 一次全量扫描可能
// 带回来 8 000+ 条 link_failed。**待办看板**要逐条列全（那是"问题清单"），但**任务详情页**
// 是渲染层：一次铺 8 000 个 <li> 会让页面卡住 ⇒ 这里只渲染前 N 条，并在折叠块里注明
// "只显示前 N 条（共 M 条）"。这只是**显示**上限，不影响落账条数（看板/API 仍是全量）。
const SAMPLE_RENDER_LIMIT = 200;

function sampleRenamed(item) {
  if (item && typeof item === 'object') return (item.from || '') + '  →  ' + (item.to || '');
  return String(item);
}

function sampleLinkFailed(item) {
  if (item && typeof item === 'object') {
    const pair = (item.src || '') + '  →  ' + (item.dst || '');
    return item.error ? pair + '  （' + item.error + '）' : pair;
  }
  return String(item);
}

function sampleOrphan(item) {
  return String(item);
}

// `samples.kept_unresolved_episode` 的每一项就是一条「相对媒体库根目录」的路径字符串
function samplePath(item) {
  return String(item);
}

// L4「可见性」+ L2「防丢」：`samples.name_collisions` 的每一项是
// `{dir, target, previous, current, kind}`。渲染成一行可读文本：
//   `作品名/Season 1 · 目标名 ← 本次源名（与 先前源名/原有文件 争名）  [kind]`
// `kind`：`within_run` = 同一次运行里有两个文件要同一个目标名（后者的体积不更大 ⇒ 它加后缀）；
// `existing_target` = 目标盘上原本就有这个文件名。都不再覆盖 —— 这一行是"谁在争名"的事实清单，
// 真正落地成什么名字看 `samples.renamed_with_suffix` / `samples.displaced_targets`。
// 标题模式（file_scope=title）没有 `dir` 时就不渲染那一段。
function sampleCollision(item) {
  if (item && typeof item === 'object') {
    const dir = String(item.dir || '');
    const target = String(item.target || '');
    const current = String(item.current || '');
    const previous = String(item.previous || '');
    const kind = String(item.kind || '');
    const head = dir ? dir + ' · ' : '';
    const tail = previous ? '（与 ' + previous + ' 争名）' : '';
    return head + target + ' ← ' + current + tail + (kind ? '  [' + kind + ']' : '');
  }
  return String(item);
}

// L2「防丢」：`samples.renamed_with_suffix` 的每一项是
// `{dir, old, target, clean, size, reason[, displaced]}`。渲染成：
//   `作品名/Season 1 · 原名  →  最终名（本来想要 clean，体积 X，reason: …）`
// `old` = 源文件名、`target` = 实际落地的名字（带 ` (N)`）、`clean` = 它想要但被占用的干净名。
function sampleSuffixed(item) {
  if (item && typeof item === 'object') {
    const dir = String(item.dir || '');
    const old = String(item.old || '');
    const target = String(item.target || '');
    const clean = String(item.clean || '');
    const reason = String(item.reason || '');
    const size = Number(item.size);
    const displaced = String(item.displaced || '');
    const bits = [];
    if (clean) bits.push('目标名 ' + clean + ' 已被占用');
    if (isFinite(size) && size > 0) bits.push('体积 ' + bytesText(size));
    if (reason) bits.push('reason: ' + reason);
    if (displaced) bits.push('让位者 ' + displaced);
    const head = dir ? dir + ' · ' : '';
    return head + old + '  →  ' + target + (bits.length ? '（' + bits.join('，') + '）' : '');
  }
  return String(item);
}

// L2「防丢」：`samples.displaced_targets` 的每一项是 `{dir, old, target, clean, size}`。
// `old` = 目标盘上原本占着这个名字的文件，`target` = 它被改到的新名字（`clean` 与 `old` 同值，
// 保留在样本里只为与上面的形状对齐）。
function sampleDisplaced(item) {
  if (item && typeof item === 'object') {
    const dir = String(item.dir || '');
    const old = String(item.old || '');
    const target = String(item.target || '');
    const head = dir ? dir + ' · ' : '';
    return head + old + '  →  ' + target + '（让出原名给体积更大的新文件）';
  }
  return String(item);
}

function itemText(value) {
  if (Array.isArray(value)) return value.map(itemText).join(' → ');
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch (e) {
      return String(value);
    }
  }
  return String(value);
}

function bytesText(n) {
  const v = Number(n);
  if (!isFinite(v) || v <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < units.length - 1) {
    x = x / 1024;
    i += 1;
  }
  return (i === 0 ? String(Math.round(x)) : x.toFixed(x < 10 ? 1 : 0)) + ' ' + units[i];
}

function countCards(r) {
  return COUNT_ROWS.map(function (row) {
    const value = Number(r[row.key]);
    const shown = isFinite(value) ? value : 0;
    const emphasis = row.kind === 'danger' && shown > 0 ? ' stat-danger'
      : (row.kind === 'ok' && shown > 0 ? ' stat-ok' : '');
    return html`<div class="stat-card${raw(emphasis)}">
      <div class="stat-head"><span class="stat-label">${t(row.label)}</span></div>
      <div class="stat-value">${shown}</div>
    </div>`;
  }).join('');
}

function samplesHtml(r) {
  const samples = r.samples && typeof r.samples === 'object' ? r.samples : {};
  const blocks = SAMPLE_ROWS.map(function (row) {
    const list = Array.isArray(samples[row.key]) ? samples[row.key] : [];
    if (!list.length) return '';
    // 服务端不再截断样本，这里只限**渲染**条数（见 SAMPLE_RENDER_LIMIT 的说明）。
    const shown = list.length > SAMPLE_RENDER_LIMIT ? list.slice(0, SAMPLE_RENDER_LIMIT) : list;
    const items = shown.map(function (item) {
      return '<li class="mono">' + esc(row.format(item)) + '</li>';
    }).join('');
    const more = list.length > shown.length
      ? '<li class="field-hint">' +
          esc(t('td.result.render_cap', { shown: shown.length, total: list.length })) + '</li>'
      : '';
    return '<details class="disclosure result-details">' +
      '<summary>' + esc(t(row.label)) + '（' + list.length + '）</summary>' +
      '<ul class="result-list">' + items + more + '</ul>' +
      '</details>';
  }).filter(function (s) { return s !== ''; }).join('');

  return html`${raw(blocks)}
    ${blocks && r.samples_truncated === true
      ? html`<p class="callout callout-warn">${raw(icon('alert', 13))}<span>${t('td.result.truncated')}</span></p>`
      : raw('')}`;
}

function resultHtml(result) {
  if (!result || typeof result !== 'object') {
    return html`<p class="empty-desc">${t('td.result.empty')}</p>`;
  }
  const r = result;
  const warnings = Array.isArray(r.warnings) ? r.warnings : [];
  // 新增 `result.source_integrity = {ok, changed:[相对路径…]}` —— 单挂载形态下
  // `/downloads:ro` 退场，「源目录没被动过」改由任务前后的逐文件核对来证明，所以核对失败是**严重事件**
  // （比普通 warning 更醒目），用 danger callout + 逐条列出差异路径。`ok` 为 true 或字段缺失时**不占位**
  // （老任务没有这个字段，不该凭空显示一条空告警）。硬链接只改 nlink/ctime，不会触发这里。
  const si = r.source_integrity && typeof r.source_integrity === 'object' ? r.source_integrity : null;
  const siChanged = si && Array.isArray(si.changed) ? si.changed : [];
  const sourceIntegrityHtml = si && si.ok === false
    ? '<p class="callout callout-danger">' + icon('alert', 13) +
        '<span class="strong">' + esc(t('td.result.source_integrity')) + '</span></p>' +
      '<p class="field-hint">' + esc(t('td.result.source_integrity.hint')) + '</p>' +
      (siChanged.length
        ? '<p class="strong">' + esc(t('td.result.source_integrity.paths')) + '</p>' +
          '<ul class="result-list">' + siChanged.map(function (p) {
            return '<li class="mono">' + esc(String(p)) + '</li>';
          }).join('') + '</ul>'
        : '')
    : '';
  return html`<div class="stat-grid result-grid">${raw(countCards(r))}</div>
    <dl class="kv">
      <dt>${t('td.result.bytes')}</dt><dd class="mono">${bytesText(r.bytes_linked)}</dd>
      <dt>${t('td.result.duration')}</dt><dd class="mono">${fmtDuration(r.duration_ms)}</dd>
    </dl>
    ${raw(samplesHtml(r))}
    ${raw(sourceIntegrityHtml)}
    ${warnings.length
      ? html`${raw(warnings.map(function (w) {
          return '<p class="callout callout-warn">' + icon('alert', 13) + '<span>' + esc(itemText(w)) + '</span></p>';
        }).join(''))}`
      : raw('')}`;
}

function metaHtml(task) {
  const rows = [
    ['td.meta.library', task.library_name || (task.library_id ? '#' + task.library_id : '—')],
    ['td.meta.kind', kindLabel(task.kind)],
    ['td.meta.trigger', triggerLabel(task.trigger)],
    ['td.meta.scope', scopeLabel(task.scope)],
    ['td.meta.created', fmtDateTime(task.created_at)],
    ['td.meta.started', fmtDateTime(task.started_at)],
    ['td.meta.finished', fmtDateTime(task.finished_at)],
    // 与「执行结果」里的耗时保持一致：优先用毫秒精度的 result.duration_ms
    //（started_at/finished_at 只到秒，短任务会显示成 0 ms）。
    ['td.meta.duration', (task.result && typeof task.result.duration_ms === 'number' &&
      isFinite(task.result.duration_ms) && task.result.duration_ms >= 0)
      ? fmtDuration(task.result.duration_ms)
      : durationBetween(task.started_at, task.finished_at)]
  ];
  return '<dl class="kv">' + rows.map(function (row) {
    return '<dt>' + esc(t(row[0])) + '</dt><dd>' + esc(row[1]) + '</dd>';
  }).join('') + '</dl>';
}

function pageHtml(task) {
  const isActive = ACTIVE.indexOf(task.status) >= 0;
  const canRetry = task.status === 'failed' || task.status === 'cancelled';
  const canCancel = task.status === 'queued';
  return html`<div class="page-head">
      <div>
        <a class="back-link" href="#/tasks">${raw(icon('back', 13))}<span>${t('td.back')}</span></a>
        <h1 class="page-title">${t('td.title', { id: task.id })}</h1>
      </div>
      <div class="page-actions">
        <span id="td-status">${raw(statusBadge(task.status))}</span>
        <button type="button" class="btn btn-primary btn-sm" id="td-retry"${canRetry ? '' : ' disabled'}
          title="${t('td.retry_hint')}">${raw(icon('refresh', 13))}<span>${t('td.retry')}</span></button>
        <button type="button" class="btn btn-secondary btn-sm" id="td-cancel"${canCancel ? '' : ' disabled'}
          title="${t('td.cancel_hint')}">${raw(icon('close', 13))}<span>${t('td.cancel')}</span></button>
        <button type="button" class="btn btn-danger btn-sm" id="td-delete">
          ${raw(icon('trash', 13))}<span>${t('td.delete')}</span>
        </button>
      </div>
    </div>

    <div class="grid-2">
      <section class="card">
        <div class="card-head"><h2 class="card-title">${t('td.meta')}</h2></div>
        <div class="card-body">${raw(metaHtml(task))}</div>
      </section>

      <section class="card">
        <div class="card-head"><h2 class="card-title">${t('td.result.title')}</h2></div>
        <div class="card-body" id="td-result">${raw(resultHtml(task.result))}</div>
      </section>
    </div>

    <section class="card" id="td-error-card"${task.error ? '' : ' hidden'}>
      <div class="card-head"><h2 class="card-title">${t('td.error.title')}</h2></div>
      <div class="card-body"><pre class="error-box" id="td-error">${task.error || ''}</pre></div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2 class="card-title">${t('td.logs.title')}</h2>
        <div class="card-actions">
          <select class="input select select-sm" id="td-log-filter" aria-label="${t('td.logs.filter')}">
            <option value="all">${t('common.all')}</option>
            <option value="structured">${t('td.structured')}</option>
            <option value="stdout">${t('td.stdout')}</option>
          </select>
          <label class="checkbox-inline">
            <input type="checkbox" id="td-log-pause"><span>${t('td.logs.pause')}</span>
          </label>
          <span class="muted" id="td-log-count">${t('td.logs.lines', { n: 0 })}</span>
        </div>
      </div>
      <div class="card-body card-body-flush">
        <div class="log-panel" id="td-logs" role="log" aria-live="polite">
          <p class="muted log-placeholder">${isActive ? t('td.logs.waiting') : t('td.logs.empty')}</p>
        </div>
      </div>
    </section>`;
}

function logLineHtml(line) {
  const level = (line.level || 'INFO').toUpperCase();
  const stream = line.stream || '';
  const cls = 'log-line log-' + level + (stream === 'stdout' ? ' log-stdout' : '');
  const ts = line.ts ? '<span class="log-ts">' + esc(line.ts) + '</span>' : '';
  const lv = '<span class="log-level">' + esc(level) + '</span>';
  return '<div class="' + cls + '" data-stream="' + esc(stream) + '">' + ts + lv +
    '<span class="log-msg">' + esc(line.message || '') + '</span></div>';
}

export async function render(container, params) {
  const id = params && params.id;
  let alive = true;
  let afterId = 0;
  let loaded = 0;
  let task = null;
  let tickTimer = null;
  let loops = 0;

  container.innerHTML = '<div class="page-head">' + skeletonBlock(2) + '</div>' +
    '<div class="grid-2"><div class="card"><div class="card-body">' + skeletonBlock(6) +
    '</div></div><div class="card"><div class="card-body">' + skeletonBlock(6) + '</div></div></div>' +
    '<div class="card"><div class="card-body">' + skeletonBlock(8) + '</div></div>';

  try {
    task = await api.getTask(id);
  } catch (err) {
    if (!alive) return null;
    container.innerHTML = errorState({
      retryId: 'td-retry-load',
      message: err && err.message ? err.message : t('err.unknown')
    });
    const btn = container.querySelector('#td-retry-load');
    if (btn) btn.addEventListener('click', function () { router.reload(); });
    return null;
  }
  if (!alive || !task) return null;

  container.innerHTML = pageHtml(task);
  bind();
  startPoll();

  const logPanel = container.querySelector('#td-logs');

  function currentFilter() {
    const sel = container.querySelector('#td-log-filter');
    return sel ? sel.value : 'all';
  }

  function applyFilterTo(node) {
    const filter = currentFilter();
    node.querySelectorAll('.log-line').forEach(function (el) {
      const stream = el.dataset.stream || '';
      el.hidden = filter === 'all' ? false : (filter === 'stdout' ? stream !== 'stdout' : stream === 'stdout');
    });
  }

  function appendLogs(items) {
    if (!items || !items.length) return;
    const placeholder = logPanel.querySelector('.log-placeholder');
    if (placeholder) placeholder.remove();
    const wasNearBottom = !logPanel.__pausedFlag;
    logPanel.insertAdjacentHTML('beforeend', items.map(logLineHtml).join(''));
    loaded += items.length;
    const counter = container.querySelector('#td-log-count');
    if (counter) counter.textContent = t('td.logs.lines', { n: loaded });
    applyFilterTo(logPanel);
    if (wasNearBottom) logPanel.scrollTop = logPanel.scrollHeight;
  }

  async function fetchLogs() {
    try {
      const res = await api.getTaskLogs(id, afterId, 500);
      if (!alive) return false;
      afterId = res.last_id;
      appendLogs(res.items);
      return res.items.length > 0;
    } catch (err) {
      return false;
    }
  }

  async function fetchTask() {
    try {
      const next = await api.getTask(id);
      if (!alive || !next) return;
      const statusChanged = next.status !== task.status;
      task = next;
      if (statusChanged) {
        const badgeHost = container.querySelector('#td-status');
        if (badgeHost) badgeHost.innerHTML = statusBadge(task.status);
        const resultHost = container.querySelector('#td-result');
        if (resultHost) resultHost.innerHTML = resultHtml(task.result);
        const errorCard = container.querySelector('#td-error-card');
        const errorBox = container.querySelector('#td-error');
        if (errorCard) errorCard.hidden = !task.error;
        if (errorBox) errorBox.textContent = task.error || '';
        const metaHost = container.querySelector('.grid-2 .card .card-body');
        if (metaHost) metaHost.innerHTML = metaHtml(task);
        updateButtons();
      }
    } catch (err) {
      /* 忽略：下一轮重试 */
    }
  }

  function updateButtons() {
    const isActive = ACTIVE.indexOf(task.status) >= 0;
    const retry = container.querySelector('#td-retry');
    const cancel = container.querySelector('#td-cancel');
    if (retry) retry.disabled = !(task.status === 'failed' || task.status === 'cancelled');
    if (cancel) cancel.disabled = task.status !== 'queued';
    if (!isActive) stopPoll();
  }

  async function tick() {
    if (!alive) return;
    loops += 1;
    await fetchLogs();
    if (loops % 4 === 0 && ACTIVE.indexOf(task.status) >= 0) await fetchTask();
    if (!alive) return;
    if (ACTIVE.indexOf(task.status) >= 0 || loops % 4 !== 0) {
      tickTimer = window.setTimeout(tick, 1500);
    } else {
      // 任务已结束：做最后一次收尾拉取
      await fetchLogs();
    }
  }

  function startPoll() {
    tickTimer = window.setTimeout(tick, 800);
  }

  function stopPoll() {
    if (tickTimer) {
      window.clearTimeout(tickTimer);
      tickTimer = null;
    }
  }

  function bind() {
    const pause = container.querySelector('#td-log-pause');
    if (pause) {
      pause.addEventListener('change', function () {
        logPanel.__pausedFlag = pause.checked;
        pause.closest('label').querySelector('span').textContent =
          pause.checked ? t('td.logs.resume') : t('td.logs.pause');
      });
    }

    const filterSel = container.querySelector('#td-log-filter');
    if (filterSel) filterSel.addEventListener('change', function () { applyFilterTo(logPanel); });

    const retryBtn = container.querySelector('#td-retry');
    if (retryBtn) {
      retryBtn.addEventListener('click', async function () {
        retryBtn.disabled = true;
        try {
          const created = await api.retryTask(id);
          const newId = created && created.id !== undefined ? created.id : id;
          toast(t('td.retried', { id: newId }), 'success');
          if (String(newId) !== String(id)) {
            router.navigate('#/tasks/' + encodeURIComponent(String(newId)));
          } else {
            router.reload();
          }
        } catch (err) {
          retryBtn.disabled = false;
        }
      });
    }

    const cancelBtn = container.querySelector('#td-cancel');
    if (cancelBtn) {
      cancelBtn.addEventListener('click', async function () {
        cancelBtn.disabled = true;
        try {
          await api.cancelTask(id);
          toast(t('td.cancelled'), 'success');
          router.reload();
        } catch (err) {
          cancelBtn.disabled = false;
        }
      });
    }

    const deleteBtn = container.querySelector('#td-delete');
    if (deleteBtn) {
      deleteBtn.addEventListener('click', async function () {
        const ok = await confirmDialog({
          title: t('td.delete_confirm.title'),
          message: t('td.delete_confirm.body', { id: id }),
          confirmText: t('td.delete'),
          danger: true
        });
        if (!ok) return;
        try {
          await api.deleteTask(id);
          toast(t('td.deleted'), 'success');
          router.navigate('#/tasks', true);
        } catch (err) {
          /* apiFetch 已 toast */
        }
      });
    }
  }

  return function cleanup() {
    alive = false;
    stopPoll();
    // 只释放资源，不清空 DOM：内容由每次渲染负责覆盖，
    // 这样即使清理被更晚的渲染延迟调用，也不会抹掉新页面。
  };
}
