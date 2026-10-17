// pages/library_edit.js —— 新建 / 编辑媒体库规则
// 布局（区块顺序）：
//   1) 页头：返回链接 + 标题（新建 / 编辑：名称）+ 右侧操作（预检·保存）
//   2) 服务端 warnings 提示条（如有）
//   3) 卡片「基本信息」：名称 / 类型（自由标签 + datalist）/ 处理器（GET /api/processors）/ 启用开关
//      + 处理器命名方式只读摘要
//   4) 卡片「路径（容器内路径）」：source_dir / target_dir + 允许根目录提示 + 同文件系统提示
//   5) 卡片「匹配条件」：分类 / 标签 / 路径前缀 三个 chip 输入 + AND 说明 + 全空不匹配警告
//   5b) 卡片「重命名规则（本库）」：开关（关 = 跟随处理器）+ 命名模板（calibre 风格 {字段}）
//      + 接受 / 拒绝目录正则（每库独立）+ 本库后缀白名单；写入 libraries.rule_override
//   6) 卡片「重命名日志」：log_file（只允许文件名）
//   7) 卡片「行为选项」：orphan_cleanup（含风险）/ permission_mode（含风险；从关到开需二次确认）
//   8) 卡片「已知限制」：details 折叠（L2/L4/L5/L6/L7/L8/L9、结构前提）
//   9) 底部粘性操作条：保存 / 取消
// 路由：#/libraries/new（新建）、#/libraries/{id}/edit（编辑）
//
// 契约（docs/contracts/PROCESSORS.md §6.3 / §7）：库通过 `processor_id` 引用处理器（v2 取消了
// type/processor 枚举约束）；`type` 变成自由文本标签；`log_file` 只允许文件名。

import { t } from '../i18n.js';
import * as api from '../api.js';
import * as router from '../router.js';
import { state, setSettings, getSetting, setProcessors } from '../store.js';
import { openValidateDialog } from './libraries.js';
import {
  namingSummary, slugLogFile, validLogFile, PROC_PARAMS, sampleContext,
  renderTemplate, templateErrors, regexError, segmentsToTemplate, knownContextNames
} from '../processors_util.js';
import {
  html, raw, icon, esc, toast, field, textInput, selectInput, switchControl,
  createChipInput, setFieldError, clearFieldErrors, loadingCard, errorState, confirmDialog
} from '../ui.js';

/** 类型只是展示/统计用的自由标签，datalist 只做建议。 */
const TYPE_SUGGESTIONS = ['Bangumi', 'Movie', 'Custom'];

function datalistHtml(id, values) {
  return '<datalist id="' + id + '">' +
    values.map(function (v) { return '<option value="' + esc(v) + '"></option>'; }).join('') +
    '</datalist>';
}

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

/** 处理器下拉（v2：来自 GET /api/processors，按 id 引用）。 */
function processorOptions(processors, current) {
  const usable = (processors || []).filter(function (p) { return p && p.enabled !== false; });
  const options = [{ value: '', label: t('le.processor.none_option') }].concat(
    usable.map(function (p) {
      return {
        value: p.id,
        label: (p.name ? p.name + '（' + p.id + '）' : p.id) +
          (p.kind === 'hardlink_only' ? ' · ' + t('proc.kind.hardlink_only') : '')
      };
    })
  );
  let known = usable.some(function (p) { return p.id === current; });
  if (current && !known) {
    options.push({ value: current, label: t('le.processor.unknown_option', { id: current }) });
    known = true;
  }
  return { value: known ? current : '', options: options, known: known };
}

function limitsHtml() {
  const keys = [
    'le.limit.a10', 'le.limit.a9', 'le.limit.l9', 'le.limit.l8',
    'le.limit.l7', 'le.limit.l6', 'le.limit.l5', 'le.limit.l4', 'le.limit.l2',
    'le.struct.bangumi', 'le.struct.movie'
  ];
  return '<details class="disclosure"><summary>' + esc(t('le.section.limits')) + '</summary>' +
    '<ul class="limit-list">' +
    keys.map(function (k) { return '<li>' + esc(t(k)) + '</li>'; }).join('') +
    '</ul></details>';
}

/** 一行目录正则：pattern [+ 季号捕获组（仅接受目录）] + 删除。 */
function ruleRowHtml(kind, item) {
  const it = item || {};
  const pattern = it.pattern === undefined || it.pattern === null ? '' : String(it.pattern);
  const withGroup = kind === 'accept';
  const group = it.season_group === undefined || it.season_group === null ? 1 : it.season_group;
  return '<div class="rule-row" data-kind="' + kind + '">' +
    '<input class="input mono" type="text" data-role="pattern" value="' + esc(pattern) + '"' +
    ' placeholder="' + esc(t(withGroup ? 'le.ov.accept.ph' : 'le.ov.reject.ph')) + '">' +
    (withGroup
      ? '<input class="input rule-group" type="number" min="0" step="1" data-role="group" value="' +
        esc(String(group)) + '" title="' + esc(t('le.ov.accept.group')) + '">'
      : '') +
    '<button type="button" class="btn btn-secondary btn-sm" data-role="remove" title="' +
    esc(t('common.remove')) + '">&#10005;</button>' +
    '</div>';
}

function ruleRowsHtml(kind, items) {
  return (Array.isArray(items) ? items : []).map(function (item) {
    return ruleRowHtml(kind, item);
  }).join('');
}

/**
 * 卡片「重命名规则（本库）」—— 库级命名覆盖与库级目录规则的前端。
 *
 * 语义：**开关关着 = 完全跟随处理器**（提交空对象，后端把覆盖清成 NULL）；开着 = 只覆盖
 * 用户真正填了的字段，其余继承处理器。命名方式在这里用 calibre 风格的模板写
 * （`{title} S{season}E{episode}{suffix}`），保存时由服务端编译成段序列。
 */
function overrideHtml(lib, globalExtText) {
  const ov = lib.rule_override && typeof lib.rule_override === 'object' ? lib.rule_override : {};
  const has = Object.keys(ov).length > 0;
  const naming = ov.naming && typeof ov.naming === 'object' ? ov.naming : {};
  const traverse = ov.traverse && typeof ov.traverse === 'object' ? ov.traverse : {};
  const templateText = naming.template === undefined || naming.template === null
    ? '' : String(naming.template);
  const extText = Array.isArray(ov.extensions) ? ov.extensions.join('\n') : '';
  const vars = PROC_PARAMS.map(function (name) {
    return '<button type="button" class="btn btn-secondary btn-sm var-btn" data-var="' +
      esc(name) + '">' + esc(name) + '</button>';
  }).join('');
  return html`<section class="card" id="le-override">
    <div class="card-head"><h2 class="card-title">${t('le.ov.section')}</h2></div>
    <div class="card-body">
      <p class="muted">${t('le.ov.desc')}</p>
      <div class="form-grid">
        ${field({
          id: 'ov_enabled',
          label: t('le.ov.enabled'),
          hint: t('le.ov.enabled.hint'),
          control: switchControl({ id: 'ov-enabled', checked: has, label: t('common.on') })
        })}
      </div>
      <div id="ov-body"${has ? raw('') : raw(' hidden')}>
        <div class="field" data-field="rule_override">
          <label class="field-label" for="ov-template">${t('le.ov.template')}</label>
          <input class="input mono" type="text" id="ov-template" value="${templateText}"
            placeholder="{title} S{season}E{episode}{suffix}">
          <p class="field-hint">${t('le.ov.template.hint')}</p>
          <div class="var-row" id="ov-vars">${raw(vars)}</div>
          <dl class="kv">
            <div>
              <dt>${t('le.ov.preview')}</dt>
              <dd><span class="mono" id="ov-preview"></span></dd>
            </div>
          </dl>
          <p class="field-error" data-error-for="rule_override"></p>
        </div>
        <div class="field" data-field="ov_accept">
          <label class="field-label">${t('le.ov.accept')}</label>
          <div class="rule-list" id="ov-accept-list">${raw(ruleRowsHtml('accept', traverse.accept_dirs))}</div>
          <button type="button" class="btn btn-secondary btn-sm" id="ov-accept-add">${t('le.ov.add')}</button>
          <p class="field-hint">${t('le.ov.accept.hint')}</p>
        </div>
        <div class="field" data-field="ov_reject">
          <label class="field-label">${t('le.ov.reject')}</label>
          <div class="rule-list" id="ov-reject-list">${raw(ruleRowsHtml('reject', traverse.reject_dirs))}</div>
          <button type="button" class="btn btn-secondary btn-sm" id="ov-reject-add">${t('le.ov.add')}</button>
          <p class="field-hint">${t('le.ov.reject.hint')}</p>
        </div>
        <div class="field" data-field="ov_ext">
          <label class="field-label" for="ov-ext">${t('le.ov.ext')}</label>
          <textarea class="input textarea" id="ov-ext" rows="3"
            placeholder=".mkv&#10;.mp4&#10;.ass">${extText}</textarea>
          <p class="field-hint">${t('le.ov.ext.hint', { global: globalExtText })}</p>
        </div>
      </div>
    </div>
  </section>`;
}

function formHtml(lib, processors, globalExtText) {
  const procSel = processorOptions(processors, lib.processor_id || '');
  return html`<form class="form" id="le-form" novalidate>
    <section class="card">
      <div class="card-head"><h2 class="card-title">${t('le.section.basic')}</h2></div>
      <div class="card-body">
        <div class="form-grid">
          ${field({
            id: 'name',
            label: t('le.name'),
            required: true,
            hint: t('le.name.hint'),
            control: textInput({ id: 'name', value: lib.name || '', placeholder: t('le.name.ph') })
          })}
          ${field({
            id: 'type',
            label: t('le.type'),
            hint: t('le.type.hint2'),
            control: raw(
              textInput({
                id: 'type',
                value: lib.type || '',
                placeholder: t('le.type.ph'),
                list: 'le-type-suggestions'
              }) + datalistHtml('le-type-suggestions', TYPE_SUGGESTIONS)
            )
          })}
          ${field({
            id: 'processor_id',
            label: t('le.processor.select'),
            required: true,
            hint: t('le.processor.select.hint'),
            control: selectInput({
              id: 'processor_id',
              value: procSel.value,
              options: procSel.options
            })
          })}
          ${field({
            id: 'enabled',
            label: t('le.enabled'),
            hint: t('le.enabled.hint'),
            control: switchControl({ id: 'enabled', checked: lib.enabled !== false, label: t('common.enabled') })
          })}
        </div>
        <dl class="kv">
          <div>
            <dt>${t('le.processor.summary')}</dt>
            <dd><span class="mono" id="processor-summary"></span></dd>
          </div>
        </dl>
        <p class="callout callout-info" id="processor-warn" hidden>
          ${raw(icon('alert', 13))}<span>${t('le.processor.missing', { id: lib.processor_id || '' })}</span>
        </p>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><h2 class="card-title">${t('le.section.paths')}</h2></div>
      <div class="card-body">
        <div class="form-grid">
          ${field({
            id: 'source_dir',
            label: t('le.source_dir'),
            required: true,
            hint: t('le.source_dir.hint'),
            control: textInput({ id: 'source_dir', value: lib.source_dir || '', placeholder: t('le.source_dir.ph') })
          })}
          ${field({
            id: 'target_dir',
            label: t('le.target_dir'),
            required: true,
            hint: t('le.target_dir.hint'),
            control: textInput({ id: 'target_dir', value: lib.target_dir || '', placeholder: t('le.target_dir.ph') })
          })}
        </div>
        <p class="callout callout-info">${raw(icon('info', 13))}<span>${t('le.paths.hint')}</span></p>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><h2 class="card-title">${t('le.section.match')}</h2></div>
      <div class="card-body">
        <p class="muted">${t('le.match.desc')}</p>
        <div class="form-grid">
          <div class="field" data-field="match_categories">
            <label class="field-label">${t('le.match.categories')}</label>
            <div id="chip-categories"></div>
            <p class="field-error" data-error-for="match_categories"></p>
          </div>
          <div class="field" data-field="match_tags">
            <label class="field-label">${t('le.match.tags')}</label>
            <div id="chip-tags"></div>
            <p class="field-error" data-error-for="match_tags"></p>
          </div>
          <div class="field" data-field="match_prefixes">
            <label class="field-label">${t('le.match.prefixes')}</label>
            <div id="chip-prefixes"></div>
            <p class="field-error" data-error-for="match_prefixes"></p>
          </div>
        </div>
        <p class="callout callout-info">
          ${raw(icon('info', 13))}<span>${t('le.match.and_hint')}</span>
        </p>
        <p class="callout callout-warn" id="match-warn" hidden>
          ${raw(icon('alert', 13))}<span>${t('le.match.empty_warn')}</span>
        </p>
      </div>
    </section>

    ${overrideHtml(lib, globalExtText)}

    <section class="card">
      <div class="card-head"><h2 class="card-title">${t('le.log_section')}</h2></div>
      <div class="card-body">
        ${field({
          id: 'log_file',
          label: t('le.log_file'),
          hint: t('le.log_file.hint'),
          control: textInput({
            id: 'log_file',
            value: lib.log_file || '',
            placeholder: t('le.log_file.ph', { slug: slugLogFile(lib.name || '') })
          })
        })}
        <p class="callout callout-info">
          ${raw(icon('info', 13))}<span>${t('le.log_section.hint')}</span>
        </p>
        ${lib.log_path
          ? html`<p class="field-hint">${t('le.log_path')}<span class="mono">${lib.log_path}</span></p>`
          : raw('')}
      </div>
    </section>

    <section class="card">
      <div class="card-head"><h2 class="card-title">${t('le.section.options')}</h2></div>
      <div class="card-body">
        <div class="form-grid">
          ${field({
            id: 'orphan_cleanup',
            label: t('le.orphan_cleanup'),
            control: switchControl({ id: 'orphan_cleanup', checked: lib.orphan_cleanup !== false, label: t('common.on') })
          })}
          ${field({
            id: 'permission_mode',
            label: t('le.permission_mode'),
            control: switchControl({ id: 'permission_mode', checked: !!lib.permission_mode, label: t('common.on') })
          })}
        </div>
        <p class="callout callout-danger">${raw(icon('alert', 13))}<span>${t('le.orphan_cleanup.risk')}</span></p>
        <p class="callout callout-danger">${raw(icon('alert', 13))}<span>${t('le.permission_mode.risk')}</span></p>
      </div>
    </section>

    <section class="card">
      <div class="card-body">${raw(limitsHtml())}</div>
    </section>
  </form>`;
}

export async function render(container, params) {
  const idParam = params && params.id;
  const isEdit = idParam !== undefined && idParam !== null && idParam !== '';
  let alive = true;

  container.innerHTML = loadingCard(t('common.loading'));

  // 允许根目录来自设置；缓存未命中时拉一次
  let settings = state.settings;
  if (!settings) {
    try {
      settings = await api.getSettings();
      setSettings(settings);
    } catch (e) {
      settings = {};
    }
  }
  if (!alive) return null;
  const globalExt = asList(getSetting('rename_extensions', []));
  const globalExtText = globalExt.length ? globalExt.join('、') : t('le.ov.ext.global_default');

  // 处理器注册表（下拉来源）；失败不阻塞页面，只提示
  let processors = Array.isArray(state.processors) ? state.processors : [];
  let processorsFailed = false;
  try {
    const res = await api.listProcessors();
    if (Array.isArray(res.items)) {
      processors = res.items;
      setProcessors(processors);
    }
  } catch (e) {
    processorsFailed = true;
  }
  if (!alive) return null;

  let lib = {
    type: 'Bangumi',
    processor_id: '',
    enabled: true,
    orphan_cleanup: true,
    permission_mode: false,
    log_file: ''
  };
  if (isEdit) {
    try {
      const loaded = await api.getLibrary(idParam);
      if (!loaded) throw new Error(t('err.unknown'));
      lib = loaded;
    } catch (err) {
      if (!alive) return null;
      container.innerHTML = errorState({
        retryId: 'le-retry',
        message: err && err.message ? err.message : t('err.unknown')
      });
      const btn = container.querySelector('#le-retry');
      if (btn) btn.addEventListener('click', function () { router.reload(); });
      return null;
    }
  } else {
    lib.match_categories = [];
    lib.match_tags = [];
    lib.match_path_prefixes = [];
  }
  if (!alive) return null;

  const warnings = asList(lib.warnings);

  container.innerHTML = html`<div class="page-head">
      <div>
        <a class="back-link" href="#/libraries">${raw(icon('back', 13))}<span>${t('lib.title')}</span></a>
        <h1 class="page-title">${isEdit ? t('le.title_edit', { name: lib.name || '' }) : t('le.title_new')}</h1>
      </div>
      <div class="page-actions">
        ${isEdit
          ? html`<button type="button" class="btn btn-secondary btn-sm" id="le-validate">
              ${raw(icon('check', 14))}<span>${t('le.validate')}</span>
            </button>`
          : html`<button type="button" class="btn btn-secondary btn-sm" id="le-validate" disabled
              title="${t('le.validate.need_save')}">
              ${raw(icon('check', 14))}<span>${t('le.validate')}</span>
            </button>`}
      </div>
    </div>

    ${warnings.length
      ? html`<p class="callout callout-warn">${raw(icon('alert', 13))}<span>${warnings.join('；')}</span></p>`
      : raw('')}

    ${processorsFailed
      ? html`<p class="callout callout-warn">${raw(icon('alert', 13))}<span>${t('le.processor.load_failed', { msg: t('err.unknown') })}</span></p>`
      : raw('')}

    <div class="callout callout-danger" id="le-form-error" hidden>${raw(icon('alert', 14))}<span id="le-form-error-text"></span></div>

    ${raw(formHtml(lib, processors, globalExtText))}

    <div class="action-bar">
      <button type="button" class="btn btn-primary" id="le-save">
        ${raw(icon('check', 15))}<span>${t('le.save')}</span>
      </button>
      <a class="btn btn-secondary" href="#/libraries">${t('common.cancel')}</a>
      <span class="muted action-hint">${t('le.validate.save_first')}</span>
    </div>`;

  // ---------------- chip 输入 ----------------
  const chips = {
    match_categories: createChipInput({
      id: 'chip-categories',
      values: asList(lib.match_categories),
      placeholder: t('le.match.categories.ph'),
      emptyText: t('common.none'),
      onChange: updateMatchWarn
    }),
    match_tags: createChipInput({
      id: 'chip-tags',
      values: asList(lib.match_tags),
      placeholder: t('le.match.tags.ph'),
      emptyText: t('common.none'),
      onChange: updateMatchWarn
    }),
    match_prefixes: createChipInput({
      id: 'chip-prefixes',
      values: asList(lib.match_path_prefixes),
      placeholder: t('le.match.prefixes.ph'),
      emptyText: t('common.none'),
      onChange: updateMatchWarn
    })
  };
  container.querySelector('#chip-categories').appendChild(chips.match_categories.root);
  container.querySelector('#chip-tags').appendChild(chips.match_tags.root);
  container.querySelector('#chip-prefixes').appendChild(chips.match_prefixes.root);

  function updateMatchWarn() {
    const empty = chips.match_categories.getValues().length === 0 &&
      chips.match_tags.getValues().length === 0 &&
      chips.match_prefixes.getValues().length === 0;
    const warn = container.querySelector('#match-warn');
    if (warn) warn.hidden = !empty;
  }
  updateMatchWarn();

  // ---------------- 处理器：命名方式只读摘要 ----------------
  const processorSelect = container.querySelector('#processor_id');
  const summaryEl = container.querySelector('#processor-summary');
  const procWarn = container.querySelector('#processor-warn');

  function updateProcessorSummary() {
    const id = processorSelect.value;
    const proc = processors.filter(function (p) { return p && p.id === id; })[0];
    if (summaryEl) {
      if (!proc) {
        summaryEl.textContent = id ? t('le.processor.missing', { id: id }) : t('le.processor.none_option');
      } else {
        const summary = namingSummary(proc);
        summaryEl.textContent = summary || t('le.processor.summary_empty');
      }
    }
    if (procWarn) procWarn.hidden = !(id && !proc);
  }
  processorSelect.addEventListener('change', updateProcessorSummary);
  updateProcessorSummary();

  // ---------------- 本库规则覆盖（库级命名覆盖 / 库级目录规则）----------------
  const ovToggle = container.querySelector('#ov-enabled');
  const ovBody = container.querySelector('#ov-body');
  const ovTemplate = container.querySelector('#ov-template');
  const ovPreview = container.querySelector('#ov-preview');
  const ovErrEl = container.querySelector('[data-error-for="rule_override"]');

  function currentProcessor() {
    const id = processorSelect.value;
    return processors.filter(function (p) { return p && p.id === id; })[0] || null;
  }

  function ovRows(selector) {
    return Array.prototype.slice.call(container.querySelectorAll(selector + ' .rule-row'));
  }

  /** 只收集填了正则的行；接受目录带 season_group（留空按 1）。 */
  function readRuleRows(selector, withGroup) {
    const out = [];
    ovRows(selector).forEach(function (row) {
      const pattern = (row.querySelector('[data-role="pattern"]').value || '').trim();
      if (!pattern) return;
      if (withGroup) {
        const cell = row.querySelector('[data-role="group"]');
        const text = cell ? String(cell.value || '').trim() : '';
        const group = text === '' ? 1 : parseInt(text, 10);
        out.push({ pattern: pattern, season_group: isNaN(group) ? 1 : group });
      } else {
        out.push({ pattern: pattern });
      }
    });
    return out;
  }

  function readExtLines() {
    const cell = container.querySelector('#ov-ext');
    if (!cell) return [];
    return String(cell.value || '').split('\n').map(function (line) {
      return line.trim().toLowerCase();
    }).filter(function (line) { return line !== ''; });
  }

  /**
   * 收集本库规则覆盖：开关关着 ⇒ 提交 `{}`（后端把覆盖清成 NULL，回到"跟随处理器"；
   * 与"没提这件事"的 `null` 语义不同，PUT 的 null 表示保留现值）。
   */
  function collectOverride() {
    if (!ovToggle || !ovToggle.checked) return {};
    const result = {};
    const template = (ovTemplate.value || '').trim();
    if (template) result.naming = { template: template };
    const accept = readRuleRows('#ov-accept-list', true);
    const reject = readRuleRows('#ov-reject-list', false);
    const traverse = {};
    if (accept.length) traverse.accept_dirs = accept;
    if (reject.length) traverse.reject_dirs = reject;
    if (Object.keys(traverse).length) result.traverse = traverse;
    const extensions = readExtLines();
    if (extensions.length) result.extensions = extensions;
    return result;
  }

  function updateOverridePreview() {
    if (!ovToggle || !ovToggle.checked) return;
    const proc = currentProcessor();
    const expressions = (proc && proc.naming && proc.naming.expressions) || {};
    const text = ovTemplate ? ovTemplate.value : '';
    if (ovPreview) {
      if (!text.trim()) {
        ovPreview.textContent = t('common.none');
      } else {
        const rendered = renderTemplate(text, sampleContext('.mkv'), expressions);
        let out = rendered.text;
        if (rendered.expressions && rendered.expressions.length) {
          out += '　·　' + t('le.ov.preview.expr', { names: rendered.expressions.join('、') });
        }
        ovPreview.textContent = out;
      }
    }
    // 库级模板也能引用处理器/覆盖自己的表达式名与引擎内部键，口径与服务端一致。
    const messages = templateErrors(text, knownContextNames(expressions));
    ovRows('#ov-accept-list').concat(ovRows('#ov-reject-list')).forEach(function (row) {
      const value = (row.querySelector('[data-role="pattern"]').value || '').trim();
      if (value && regexError(value)) {
        messages.push(t('le.ov.bad_regex', { pattern: value }));
      }
    });
    if (ovErrEl) ovErrEl.textContent = messages.join(' ');
  }

  function wireRuleRow(row) {
    const remove = row.querySelector('[data-role="remove"]');
    if (remove) {
      remove.addEventListener('click', function () {
        if (row.parentNode) row.parentNode.removeChild(row);
        updateOverridePreview();
      });
    }
    const pattern = row.querySelector('[data-role="pattern"]');
    if (pattern) pattern.addEventListener('input', updateOverridePreview);
    const group = row.querySelector('[data-role="group"]');
    if (group) group.addEventListener('input', updateOverridePreview);
  }

  function addRuleRow(selector, kind) {
    const host = container.querySelector(selector);
    if (!host) return;
    const holder = document.createElement('div');
    holder.innerHTML = ruleRowHtml(kind, null);
    const row = holder.firstChild;
    host.appendChild(row);
    wireRuleRow(row);
    const input = row.querySelector('[data-role="pattern"]');
    if (input) input.focus();
    updateOverridePreview();
  }

  ovRows('#ov-accept-list').concat(ovRows('#ov-reject-list')).forEach(wireRuleRow);

  const acceptAdd = container.querySelector('#ov-accept-add');
  if (acceptAdd) acceptAdd.addEventListener('click', function () { addRuleRow('#ov-accept-list', 'accept'); });
  const rejectAdd = container.querySelector('#ov-reject-add');
  if (rejectAdd) rejectAdd.addEventListener('click', function () { addRuleRow('#ov-reject-list', 'reject'); });

  function insertTemplateVar(name) {
    if (!ovTemplate) return;
    const token = '{' + name + '}';
    const start = ovTemplate.selectionStart === null ? ovTemplate.value.length : ovTemplate.selectionStart;
    const end = ovTemplate.selectionEnd === null ? start : ovTemplate.selectionEnd;
    ovTemplate.value = ovTemplate.value.slice(0, start) + token + ovTemplate.value.slice(end);
    const caret = start + token.length;
    if (ovTemplate.setSelectionRange) ovTemplate.setSelectionRange(caret, caret);
    ovTemplate.focus();
    updateOverridePreview();
  }

  Array.prototype.slice.call(container.querySelectorAll('#ov-vars .var-btn')).forEach(function (btn) {
    btn.addEventListener('click', function () { insertTemplateVar(btn.getAttribute('data-var')); });
  });

  function syncOverrideVisibility() {
    const on = !!(ovToggle && ovToggle.checked);
    if (ovBody) ovBody.hidden = !on;
    if (on) {
      // 第一次打开时把处理器的命名方式带进模板框：用户看到的是"当前的写法"，
      // 而不是一个空框（改 / 不改都安全）。
      if (ovTemplate && !ovTemplate.value.trim()) {
        const proc = currentProcessor();
        const segments = (proc && proc.naming && proc.naming.segments) || [];
        if (segments.length) ovTemplate.value = segmentsToTemplate(segments);
      }
      updateOverridePreview();
    }
  }
  if (ovToggle) ovToggle.addEventListener('change', syncOverrideVisibility);
  if (ovTemplate) ovTemplate.addEventListener('input', updateOverridePreview);
  const ovExt = container.querySelector('#ov-ext');
  if (ovExt) ovExt.addEventListener('input', updateOverridePreview);
  syncOverrideVisibility();

  // ---------------- 校验 ----------------
  function collect() {
    return {
      name: (container.querySelector('#name').value || '').trim(),
      type: (container.querySelector('#type').value || '').trim(),
      processor_id: processorSelect.value,
      enabled: container.querySelector('#enabled').checked,
      source_dir: (container.querySelector('#source_dir').value || '').trim(),
      target_dir: (container.querySelector('#target_dir').value || '').trim(),
      match_categories: chips.match_categories.getValues(),
      match_tags: chips.match_tags.getValues(),
      match_path_prefixes: chips.match_prefixes.getValues(),
      orphan_cleanup: container.querySelector('#orphan_cleanup').checked,
      permission_mode: container.querySelector('#permission_mode').checked,
      log_file: (container.querySelector('#log_file').value || '').trim(),
      rule_override: collectOverride()
    };
  }

  /**
   * 客户端校验 —— **乐观提交**：只给即时提示，绝不阻断保存
   * （硬要求：后端 422 说合法就不许挡；与 processor_edit.js 同一策略）。
   * 因此本函数没有返回值、不参与保存分支。
   */
  function validate(payload) {
    clearFieldErrors(container);
    if (!payload.name) setFieldError(container, 'name', t('le.err.name_required'));
    if (!payload.processor_id) {
      setFieldError(container, 'processor_id', t('le.err.processor_required'));
    } else if (!processors.some(function (p) { return p && p.id === payload.processor_id; })) {
      setFieldError(container, 'processor_id', t('le.processor.missing', { id: payload.processor_id }));
    }
    if (!validLogFile(payload.log_file)) setFieldError(container, 'log_file', t('le.log_file.bad'));
    if (!payload.source_dir) {
      setFieldError(container, 'source_dir', t('le.err.source_required'));
    } else if (payload.source_dir.charAt(0) !== '/') {
      setFieldError(container, 'source_dir', t('le.err.absolute', { field: t('le.source_dir') }));
    }
    if (!payload.target_dir) {
      setFieldError(container, 'target_dir', t('le.err.target_required'));
    } else if (payload.target_dir.charAt(0) !== '/') {
      setFieldError(container, 'target_dir', t('le.err.absolute', { field: t('le.target_dir') }));
    }
    if (payload.source_dir && payload.target_dir && payload.source_dir === payload.target_dir) {
      setFieldError(container, 'target_dir', t('le.err.same'));
    }
    if (payload.source_dir && payload.target_dir) {
      const a = payload.source_dir.replace(/\/+$/, '');
      const b = payload.target_dir.replace(/\/+$/, '');
      if (a.indexOf(b + '/') === 0 || b.indexOf(a + '/') === 0) {
        setFieldError(container, 'target_dir', t('le.err.nested'));
      }
    }
  }

  /** 库字段路径 → 表单控件；映射不到的（含 ""）进顶部通用错误区，message 原样保留。 */
  const LIB_FIELD_MAP = ['name', 'type', 'processor_id', 'source_dir', 'target_dir', 'log_file',
    'orphan_cleanup', 'permission_mode', 'enabled'];

  function applyServerErrors(err) {
    const rawDetail = err && err.detail;
    let list = null;
    if (rawDetail && Array.isArray(rawDetail.detail)) list = rawDetail.detail;
    else if (rawDetail && Array.isArray(rawDetail.errors)) list = rawDetail.errors;
    else if (Array.isArray(rawDetail)) list = rawDetail;
    const host = container.querySelector('#le-form-error');
    const text = container.querySelector('#le-form-error-text');
    if (text) text.textContent = '';
    if (!host || !text || !list || !list.length) {
      if (host) host.hidden = true;
      return false;
    }
    const general = [];
    const overrideErrors = [];
    list.forEach(function (item) {
      if (!item) return;
      let field;
      if (item.field !== undefined) field = String(item.field).split('.')[0];
      else if (Array.isArray(item.loc)) field = String(item.loc.filter(function (p) { return p !== 'body' && !/^\d+$/.test(p); })[0] || '');
      else field = '';
      const message = String(item.message !== undefined ? item.message : (item.msg || ''));
      if (field === 'rule_override') {
        // 覆盖的报错自带完整字段路径（rule_override.traverse.accept_dirs[0].season_group），
        // 全部留在卡片里，别按第一段吞掉后面的信息。
        const full = item.field !== undefined ? String(item.field) : 'rule_override';
        overrideErrors.push({ field: full, message: message });
        return;
      }
      if (LIB_FIELD_MAP.indexOf(field) >= 0) setFieldError(container, field, message);
      else general.push({ field: field, message: message });
    });
    if (overrideErrors.length) {
      // 服务端说覆盖有问题 ⇒ 不管开关当前什么状态，都要让用户看到那张卡片。
      if (ovToggle && !ovToggle.checked) {
        ovToggle.checked = true;
        syncOverrideVisibility();
      }
      if (ovErrEl) {
        ovErrEl.textContent = overrideErrors.map(function (item) {
          return item.field + '：' + item.message;
        }).join(' ');
      }
    }
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
    } else {
      host.hidden = true;
    }
    return true;
  }

  // ---------------- 保存 ----------------
  const saveBtn = container.querySelector('#le-save');
  saveBtn.addEventListener('click', async function () {
    const payload = collect();
    // 乐观提交：客户端校验只给即时提示（见 validate 注释），后端 422 才是真源。
    validate(payload);
    // permission_mode 是"红灯"开关（开启前必须二次确认）。
    // 只在**从关到开**时问：编辑一个本来就开着的库不该每次保存都被拦一次。
    // 放在 disable 按钮之前 —— 用户点"取消"时按钮状态必须原样保持。
    if (payload.permission_mode && !(isEdit && lib.permission_mode)) {
      const agreed = await confirmDialog({
        title: t('le.permission_mode.confirm.title'),
        message: t('le.permission_mode.confirm.message'),
        detail: t('le.permission_mode.confirm.detail'),
        confirmText: t('le.permission_mode.confirm.ok'),
        cancelText: t('common.cancel'),
        danger: true
      });
      if (!agreed) {
        toast(t('le.permission_mode.confirm.cancelled'), 'info');
        return;
      }
    }
    const label = saveBtn.querySelector('span');
    const original = label.textContent;
    saveBtn.disabled = true;
    label.textContent = t('common.saving');
    try {
      if (isEdit) {
        await api.updateLibrary(idParam, payload);
        toast(t('le.saved'), 'success');
        router.reload();
      } else {
        const created = await api.createLibrary(payload);
        toast(t('le.created'), 'success');
        const newId = created && created.id !== undefined ? created.id : null;
        if (newId !== null) {
          router.navigate('#/libraries/' + encodeURIComponent(String(newId)) + '/edit', true);
        } else {
          router.navigate('#/libraries', true);
        }
      }
    } catch (err) {
      if (!applyServerErrors(err)) {
        toast(t('le.err.save_failed', { msg: err && err.message ? err.message : t('err.unknown') }), 'error');
      }
    } finally {
      saveBtn.disabled = false;
      label.textContent = original;
    }
  });

  // ---------------- 预检 ----------------
  const validateBtn = container.querySelector('#le-validate');
  if (validateBtn && isEdit) {
    validateBtn.addEventListener('click', function () {
      openValidateDialog({ id: idParam, name: collect().name || lib.name });
    });
  }

  return function cleanup() {
    alive = false;
    // 只释放资源，不清空 DOM：内容由每次渲染负责覆盖，
    // 这样即使清理被更晚的渲染延迟调用，也不会抹掉新页面。
  };
}
