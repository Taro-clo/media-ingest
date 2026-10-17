// processor_edit.js —— 处理器规则编辑器（hash 路由 #/processors/{id}）
//
// 契约：docs/contracts/PROCESSORS.md §3（ProcessorSpec）、§3.1（naming 的可用名字）、§4（引擎语义，
// 用于界面上解释规则的作用）、§7（PUT /api/processors/{id} / reset / validate）、§8（前端增量）。
//
// 页面结构（自上而下）：
//   1) 页头：返回 / 标题（名称 + 标识 + 内置徽章）/ 未保存徽章 / 校验 / 保存
//   2) 基本信息（名称、说明、处理方式、启用、只读参考实现）
//   3) 命名方式：具名表达式（key/value 行）+ 段序列（上移/下移/增删/参数/pad/字面量）+ 实时预览
//   4) 目录遍历（title_depth、file_scope、skip_season_zero）
//   5) 接受的目录正则（{pattern, season_group}，逐行试编译）
//   6) 拒绝的目录正则（{pattern}，命中即整棵子树跳过）
//   7) 接受的后缀名（chips，必须点开头）
//   8) 剧集号识别（episode_patterns + episode_fallback）
//   9) 多文件选择（selection.per）
//  10) 重命名日志文件名（log_file）

import { t } from '../i18n.js';
import * as api from '../api.js';
import * as router from '../router.js';
import { setProcessors, setLibraries, state } from '../store.js';
import {
  raw, esc, icon, toast, confirmDialog, field, textInput, numberInput, selectInput,
  textareaInput, switchControl, badge, createChipInput, clearFieldErrors, setFieldError,
  loadingCard, errorState
} from '../ui.js';
import {
  PROC_PARAMS, sampleContext, renderSegments, validLogFile, regexError,
  compileTemplate, templateErrors, segmentsToTemplate, knownContextNames
} from '../processors_util.js';

function clone(v) {
  return JSON.parse(JSON.stringify(v));
}

function isBuiltin(spec) {
  return !!(spec && spec.builtin);
}

/** 内置 rename 处理器 → 可用的预设；hardlink_only 只给 default；自定义不给。 */
export function resettablePresets(spec) {
  if (!isBuiltin(spec)) return [];
  const kind = spec && spec.kind;
  return kind === 'rename' ? ['default', 'legacy'] : ['default'];
}

function paramLabel(name) {
  const key = 'pe.param.' + name;
  const label = t(key);
  return label === key ? name : label + '（' + name + '）';
}

function paramOptions(expressionNames, current) {
  const opts = PROC_PARAMS.map(function (p) { return { value: p, label: paramLabel(p) }; });
  if (expressionNames.length) {
    opts.push({ value: '', label: '── ' + t('pe.expr.title') + ' ──', disabled: true });
    expressionNames.forEach(function (n) {
      opts.push({ value: n, label: n + '（表达式）' });
    });
  }
  if (current && !opts.some(function (o) { return o.value === current; })) {
    // 引擎内部键（episode_resolved）能被段序列读到，只是不在变量表里 —— 不算"未知"。
    const known = knownContextNames(expressionNames);
    opts.push({ value: current, label: current + (known.indexOf(current) === -1 ? '（未知）' : '') });
  }
  return opts;
}

// ---------------------------------------------------------------- 渲染

function segmentRow(seg, index, expressionNames) {
  const isLiteral = seg && seg.literal !== undefined && seg.literal !== null;
  const type = isLiteral ? 'literal' : 'param';
  return '<div class="seg-row" data-index="' + index + '">' +
    '<div class="seg-order">' +
      '<button type="button" class="icon-btn" data-act="up" aria-label="' + esc(t('pe.seg.up')) + '"' +
        (index === 0 ? ' disabled' : '') + '>' + icon('arrow_up', 14) + '</button>' +
      '<button type="button" class="icon-btn" data-act="down" aria-label="' + esc(t('pe.seg.down')) + '"' +
        '>' + icon('arrow_down', 14) + '</button>' +
    '</div>' +
    '<div class="seg-field">' +
      '<label class="field-label" for="seg-type-' + index + '">' + esc(t('pe.seg.type')) + '</label>' +
      selectInput({
        id: 'seg-type-' + index,
        value: type,
        options: [
          { value: 'param', label: t('pe.seg.type.param') },
          { value: 'literal', label: t('pe.seg.type.literal') }
        ]
      }) +
    '</div>' +
    (isLiteral
      ? '<div class="seg-field">' +
          '<label class="field-label" for="seg-literal-' + index + '">' + esc(t('pe.seg.literal')) + '</label>' +
          textInput({ id: 'seg-literal-' + index, value: seg.literal, placeholder: t('pe.seg.literal.ph') }) +
        '</div>'
      : '<div class="seg-field">' +
          '<label class="field-label" for="seg-param-' + index + '">' + esc(t('pe.seg.param')) + '</label>' +
          selectInput({ id: 'seg-param-' + index, value: seg.param, options: paramOptions(expressionNames, seg.param) }) +
        '</div>' +
        '<div class="seg-field seg-field-sm">' +
          '<label class="field-label" for="seg-pad-' + index + '">' + esc(t('pe.seg.pad')) + '</label>' +
          numberInput({ id: 'seg-pad-' + index, value: seg.pad === undefined ? '' : seg.pad, min: 0, max: 10 }) +
        '</div>') +
    '<button type="button" class="icon-btn" data-act="remove" aria-label="' + esc(t('pe.seg.remove')) + '">' +
      icon('trash', 14) + '</button>' +
    '</div>';
}

function previewHtml(spec) {
  if (spec.kind === 'hardlink_only') {
    return '<p class="muted">' + esc(t('pe.processor.summary_empty')) + '</p>';
  }
  const mkv = renderSegments(spec, sampleContext('.mkv'));
  const ass = renderSegments(spec, sampleContext('.chs.ass'));
  const exprs = mkv.expressions.concat(ass.expressions).filter(function (v, i, a) {
    return a.indexOf(v) === i;
  });
  return '<dl class="kv preview-kv">' +
    '<div><dt>' + esc(t('pe.preview.mkv')) + '</dt><dd><span class="mono">' + esc(mkv.text || '—') + '</span></dd></div>' +
    '<div><dt>' + esc(t('pe.preview.ass')) + '</dt><dd><span class="mono">' + esc(ass.text || '—') + '</span></dd></div>' +
    '</dl>' +
    '<p class="field-hint">' + esc(t('pe.preview.context')) + '</p>' +
    (exprs.length
      ? '<p class="field-hint">' + esc(t('pe.preview.expr_note', { names: exprs.join('、') })) + '</p>'
      : '');
}

// 实时正则检查只是「提示」（JS RegExp 的报错文案与后端 Python re 不同）：
// 徽章只说能不能编译，技术细节降级为 title + 小字 code；真源是保存后后端的 422 message。
function regexBadge(pattern) {
  if (!pattern) return badge(t('pe.regex.required'), 'warn');
  const err = regexError(pattern);
  if (!err) return badge(t('pe.regex.ok'), 'ok');
  return '<span class="badge badge-danger" title="' + esc(err) + '">' + esc(t('pe.regex.bad')) + '</span>' +
    '<code class="re-err" title="' + esc(err) + '">' + esc(err.length > 80 ? err.slice(0, 80) + '…' : err) + '</code>';
}

/** 后端 422 命中某个列表字段时，用它返回的中文 message 覆盖 JS 侧那行提示。 */
function overrideRegexStates(listHost, message) {
  if (!listHost) return;
  const short = message.length > 60 ? message.slice(0, 60) + '…' : message;
  Array.prototype.forEach.call(listHost.querySelectorAll('.re-state'), function (host) {
    host.innerHTML = '<span class="badge badge-danger" title="' + esc(message) + '">' + esc(short) + '</span>';
  });
}

function acceptRow(row, index) {
  const err = regexError(row.pattern);
  return '<div class="re-row" data-index="' + index + '">' +
    '<div class="re-field">' +
      '<label class="field-label" for="acc-pattern-' + index + '">' + esc(t('pe.accept.pattern')) + '</label>' +
      textInput({ id: 'acc-pattern-' + index, value: row.pattern, placeholder: '^Season (\\d+)$' }) +
    '</div>' +
    '<div class="re-field re-field-sm">' +
      '<label class="field-label" for="acc-season-' + index + '">' + esc(t('pe.accept.season_group')) + '</label>' +
      textInput({
        id: 'acc-season-' + index,
        value: row.season_group === undefined || row.season_group === null ? '' : row.season_group,
        placeholder: '1'
      }) +
    '</div>' +
    '<div class="re-state">' + regexBadge(row.pattern) + '</div>' +
    '<button type="button" class="icon-btn" data-act="remove" aria-label="' + esc(t('pe.row.remove')) + '">' + icon('trash', 14) + '</button>' +
    '</div>';
}

function rejectRow(row, index) {
  return '<div class="re-row" data-index="' + index + '">' +
    '<div class="re-field">' +
      '<label class="field-label" for="rej-pattern-' + index + '">' + esc(t('pe.reject.pattern')) + '</label>' +
      textInput({ id: 'rej-pattern-' + index, value: row.pattern, placeholder: '(?i)^scans?$' }) +
    '</div>' +
    '<div class="re-state">' + regexBadge(row.pattern) + '</div>' +
    '<button type="button" class="icon-btn" data-act="remove" aria-label="' + esc(t('pe.row.remove')) + '">' + icon('trash', 14) + '</button>' +
    '</div>';
}

function episodeRow(row, index) {
  return '<div class="ep-row" data-index="' + index + '">' +
    '<div class="re-field re-field-sm">' +
      '<label class="field-label" for="ep-name-' + index + '">' + esc(t('pe.episode.name')) + '</label>' +
      textInput({ id: 'ep-name-' + index, value: row.name, placeholder: 'ep' }) +
    '</div>' +
    '<div class="re-field">' +
      '<label class="field-label" for="ep-pattern-' + index + '">' + esc(t('pe.episode.pattern')) + '</label>' +
      textInput({ id: 'ep-pattern-' + index, value: row.pattern }) +
    '</div>' +
    '<div class="re-state">' + regexBadge(row.pattern) + '</div>' +
    '<button type="button" class="icon-btn" data-act="remove" aria-label="' + esc(t('pe.row.remove')) + '">' + icon('trash', 14) + '</button>' +
    '</div>';
}

function expressionRow(name, value, index) {
  return '<div class="expr-row" data-index="' + index + '">' +
    '<div class="re-field re-field-sm">' +
      '<label class="field-label" for="expr-key-' + index + '">' + esc(t('pe.expr.name')) + '</label>' +
      textInput({ id: 'expr-key-' + index, value: name, placeholder: 'ep2' }) +
    '</div>' +
    '<div class="re-field">' +
      '<label class="field-label" for="expr-value-' + index + '">' + esc(t('pe.expr.value')) + '</label>' +
      textInput({
        id: 'expr-value-' + index,
        value: value,
        placeholder: "int(episode_raw) + 12"
      }) +
    '</div>' +
    '<button type="button" class="icon-btn" data-act="remove" aria-label="' + esc(t('pe.row.remove')) + '">' + icon('trash', 14) + '</button>' +
    '</div>';
}

function pageHtml(spec) {
  const naming = spec.naming || {};
  const segments = Array.isArray(naming.segments) ? naming.segments : [];
  const expressions = (naming.expressions && typeof naming.expressions === 'object') ? naming.expressions : {};
  const expressionNames = Object.keys(expressions);
  const traverse = spec.traverse || {};
  const accept = Array.isArray(traverse.accept_dirs) ? traverse.accept_dirs : [];
  const reject = Array.isArray(traverse.reject_dirs) ? traverse.reject_dirs : [];
  const episodePatterns = Array.isArray(spec.episode_patterns) ? spec.episode_patterns : [];
  const selection = spec.selection || {};
  const origin = window.location.origin && window.location.origin !== 'null'
    ? window.location.origin
    : 'http://' + window.location.host;

  return '' +
  '<div class="page-head">' +
    '<div>' +
      '<a class="back-link" href="#/processors">' + icon('back', 14) + '<span>' + esc(t('pe.back')) + '</span></a>' +
      '<h1>' + esc(t('pe.title', { name: spec.name || spec.id })) + '</h1>' +
      '<p class="page-sub">' + esc(spec.id) +
        (spec.builtin ? ' · ' + badge(t('proc.builtin.yes'), 'info') : ' · ' + badge(t('proc.builtin.no'), 'muted')) +
        ' · ' + badge(spec.kind === 'rename' ? t('proc.kind.rename') : t('proc.kind.hardlink_only'), 'muted') +
        ' <span class="badge badge-warn" id="pe-dirty" hidden>' + esc(t('pe.unsaved')) + '</span>' +
      '</p>' +
    '</div>' +
    '<div class="page-actions">' +
      '<button type="button" class="btn btn-secondary btn-sm" id="pe-validate">' + esc(t('pe.validate')) + '</button>' +
      (isBuiltin(spec)
        ? '<button type="button" class="btn btn-secondary btn-sm" id="pe-reset">' + esc(t('pe.reset')) + '</button>'
        : '') +
      '<button type="button" class="btn btn-primary btn-sm" id="pe-save">' + esc(t('pe.save')) + '</button>' +
    '</div>' +
  '</div>' +

  // 通用错误区：后端 422 里映射不到具体控件的 field（含 ""）也必须显示在这里，绝不静默吞掉。
  '<div class="callout callout-danger" id="pe-form-error" hidden>' + raw(icon('alert', 14)) +
    '<span id="pe-form-error-text"></span></div>' +

  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.basic.title')) + '</h2></div>' +
    '<div class="card-body">' +
      '<div class="form-grid">' +
        field({ id: 'pe-name', label: t('pe.basic.name'), required: true, control: textInput({ id: 'pe-name', value: spec.name }) }) +
        '<div class="field"><label class="field-label">' + esc(t('proc.col.id')) + '</label>' +
          '<p class="mono">' + esc(spec.id) + '</p></div>' +
        field({
          id: 'pe-kind',
          label: t('pe.basic.kind'),
          hint: t('pe.basic.kind.hint'),
          control: selectInput({
            id: 'pe-kind',
            value: spec.kind,
            options: [
              { value: 'rename', label: t('proc.kind.rename') },
              { value: 'hardlink_only', label: t('proc.kind.hardlink_only') }
            ]
          })
        }) +
        '<div class="field"><label class="field-label">' + esc(t('pe.basic.enabled')) + '</label>' +
          switchControl({ id: 'pe-enabled', checked: spec.enabled !== false, label: spec.enabled === false ? t('common.disabled') : t('common.enabled') }) +
          '<p class="field-hint">' + esc(t('pe.basic.enabled.hint')) + '</p></div>' +
      '</div>' +
      field({ id: 'pe-desc', label: t('pe.basic.desc'), control: textareaInput({ id: 'pe-desc', value: spec.description || '', rows: 2, placeholder: t('pe.basic.desc.ph') }) }) +

    '</div>' +
  '</div>' +

  (spec.kind === 'hardlink_only' ? '' :
  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.naming.title')) + '</h2></div>' +
    '<div class="card-body" data-field="pe-segments">' +
      '<p class="field-hint">' + esc(t('pe.naming.desc')) + '</p>' +
      '<div class="field" data-field="pe-template">' +
        '<label class="field-label" for="pe-template">' + esc(t('pe.template.label')) + '</label>' +
        textInput({ id: 'pe-template', value: segmentsToTemplate(segments), placeholder: '{title} S{season}E{episode}{suffix}' }) +
        '<p class="field-hint">' + esc(t('pe.template.hint')) + '</p>' +
        '<div class="var-row" id="pe-vars">' +
          PROC_PARAMS.map(function (name) {
            return '<button type="button" class="btn btn-secondary btn-sm var-btn" data-var="' + esc(name) + '">' + esc(name) + '</button>';
          }).join('') +
        '</div>' +
        '<div class="chip-row">' +
          '<button type="button" class="btn btn-secondary btn-sm" id="pe-template-apply">' + esc(t('pe.template.apply')) + '</button>' +
        '</div>' +
        '<p class="field-error" data-error-for="pe-template"></p>' +
      '</div>' +
      '<div id="pe-seg-list">' +
        (segments.length ? segments.map(function (s, i) { return segmentRow(s, i, expressionNames); }).join('')
          : '<p class="muted">' + esc(t('pe.seg.empty')) + '</p>') +
      '</div>' +
      '<div class="chip-row">' +
        '<button type="button" class="btn btn-secondary btn-sm" id="pe-seg-add-param">' + raw(icon('plus', 13)) + '<span>' + esc(t('pe.seg.add_param')) + '</span></button>' +
        '<button type="button" class="btn btn-secondary btn-sm" id="pe-seg-add-literal">' + raw(icon('plus', 13)) + '<span>' + esc(t('pe.seg.add_literal')) + '</span></button>' +
      '</div>' +
      '<p class="field-error" data-error-for="pe-segments"></p>' +
      '<h3 class="cfg-sub">' + esc(t('pe.preview.title')) + '</h3>' +
      '<div id="pe-preview">' + previewHtml(spec) + '</div>' +
    '</div>' +
  '</div>' +

  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.expr.title')) + '</h2></div>' +
    '<div class="card-body" data-field="pe-expressions">' +
      '<p class="field-hint">' + esc(t('pe.expr.desc')) + '</p>' +
      '<div class="callout callout-warn">' + raw(icon('alert', 14)) + '<span>' + esc(t('pe.expr.eval_warn')) + '</span></div>' +
      '<div id="pe-expr-list">' +
        (expressionNames.length
          ? expressionNames.map(function (n, i) { return expressionRow(n, expressions[n], i); }).join('')
          : '<p class="muted">' + esc(t('pe.expr.empty')) + '</p>') +
      '</div>' +
      '<div class="chip-row">' +
        '<button type="button" class="btn btn-secondary btn-sm" id="pe-expr-add">' + raw(icon('plus', 13)) + '<span>' + esc(t('pe.expr.add')) + '</span></button>' +
      '</div>' +
      '<p class="field-error" data-error-for="pe-expressions"></p>' +
    '</div>' +
  '</div>') +

  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.traverse.title')) + '</h2></div>' +
    '<div class="card-body" data-field="pe-traverse">' +
    '<div class="form-grid">' +
      field({
        id: 'pe-title-depth',
        label: t('pe.traverse.title_depth'),
        hint: t('pe.traverse.title_depth.hint'),
        control: numberInput({ id: 'pe-title-depth', value: traverse.title_depth === undefined ? 1 : traverse.title_depth, min: 1, max: 5 })
      }) +
      field({
        id: 'pe-file-scope',
        label: t('pe.traverse.file_scope'),
        control: selectInput({
          id: 'pe-file-scope',
          value: traverse.file_scope || 'subdir',
          options: [
            { value: 'subdir', label: t('pe.traverse.file_scope.subdir') },
            { value: 'title', label: t('pe.traverse.file_scope.title') }
          ]
        })
      }) +
      '<div class="field"><label class="field-label">' + esc(t('pe.traverse.skip_season_zero')) + '</label>' +
        switchControl({ id: 'pe-skip-zero', checked: traverse.skip_season_zero !== false, label: traverse.skip_season_zero === false ? t('common.off') : t('common.on') }) +
        '<p class="field-hint">' + esc(t('pe.traverse.skip_season_zero.hint')) + '</p></div>' +
    '</div>' +
    '<p class="field-error" data-error-for="pe-traverse"></p>' +
    '</div>' +
  '</div>' +

  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.accept.title')) + '</h2></div>' +
    '<div class="card-body" data-field="pe-accept">' +
      '<p class="field-hint">' + esc(t('pe.accept.desc')) + '</p>' +
      // 这一行必须真正渲染出来，否则用户看不到任何季号捕获组说明。
      // 后端契约：合法值 0 或 ≥1 且 ≤ 该正则的捕获组数量；留空 = 缺省 1（内置 `^Season (\d+)$` 正是靠它）。
      '<p class="field-hint" id="pe-accept-season-hint">' + esc(t('pe.accept.season_group.hint')) + '</p>' +
      '<div id="pe-accept-list">' +
        (accept.length ? accept.map(acceptRow).join('') : '<p class="muted">' + esc(t('pe.accept.empty')) + '</p>') +
      '</div>' +
      '<div class="chip-row"><button type="button" class="btn btn-secondary btn-sm" id="pe-accept-add">' +
        raw(icon('plus', 13)) + '<span>' + esc(t('pe.accept.add')) + '</span></button></div>' +
      '<p class="field-error" data-error-for="pe-accept"></p>' +
    '</div>' +
  '</div>' +

  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.reject.title')) + '</h2></div>' +
    '<div class="card-body" data-field="pe-reject">' +
      '<p class="field-hint">' + esc(t('pe.reject.desc')) + '</p>' +
      '<div id="pe-reject-list">' +
        (reject.length ? reject.map(rejectRow).join('') : '<p class="muted">' + esc(t('pe.reject.empty')) + '</p>') +
      '</div>' +
      '<div class="chip-row"><button type="button" class="btn btn-secondary btn-sm" id="pe-reject-add">' +
        raw(icon('plus', 13)) + '<span>' + esc(t('pe.reject.add')) + '</span></button></div>' +
      '<p class="field-error" data-error-for="pe-reject"></p>' +
    '</div>' +
  '</div>' +

  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.ext.title')) + '</h2></div>' +
    '<div class="card-body" data-field="pe-ext">' +
      '<p class="field-hint">' + esc(t('pe.ext.desc')) + '</p>' +
      '<div id="pe-ext-host"></div>' +
      '<p class="field-hint">' + esc(t('pe.ext.empty')) + '</p>' +
      '<p class="field-error" data-error-for="pe-ext"></p>' +
    '</div>' +
  '</div>' +

  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.episode.title')) + '</h2></div>' +
    '<div class="card-body" data-field="pe-episode">' +
      '<p class="field-hint">' + esc(t('pe.episode.desc')) + '</p>' +
      '<div id="pe-ep-list">' +
        (episodePatterns.length ? episodePatterns.map(episodeRow).join('') : '<p class="muted">' + esc(t('pe.episode.empty')) + '</p>') +
      '</div>' +
      '<div class="chip-row"><button type="button" class="btn btn-secondary btn-sm" id="pe-ep-add">' +
        raw(icon('plus', 13)) + '<span>' + esc(t('pe.episode.add')) + '</span></button></div>' +
      '<p class="field-error" data-error-for="pe-episode"></p>' +
      field({
        id: 'pe-ep-fallback',
        label: t('pe.episode.fallback'),
        hint: t('pe.episode.fallback.hint'),
        control: textInput({ id: 'pe-ep-fallback', value: spec.episode_fallback === undefined || spec.episode_fallback === null ? '' : spec.episode_fallback })
      }) +
    '</div>' +
  '</div>' +

  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.selection.title')) + '</h2></div>' +
    '<div class="card-body" data-field="pe-selection">' +
      field({
        id: 'pe-per',
        label: t('pe.selection.per'),
        hint: t('pe.selection.per.hint'),
        control: selectInput({
          id: 'pe-per',
          value: selection.per || 'file',
          options: [
            { value: 'file', label: t('pe.selection.per.file') },
            { value: 'extension', label: t('pe.selection.per.extension') }
          ]
        })
      }) +
      '<p class="field-error" data-error-for="pe-selection"></p>' +
    '</div>' +
  '</div>' +

  '<div class="card">' +
    '<div class="card-head"><h2>' + esc(t('pe.log.title')) + '</h2></div>' +
    '<div class="card-body"><div class="form-grid">' +
      field({
        id: 'pe-log-file',
        label: t('pe.log.file'),
        hint: t('pe.log.file.hint'),
        control: raw('<div class="chip-row">' +
          textInput({ id: 'pe-log-file', value: spec.log_file || '', placeholder: 'bangumicache.log' }) +
          '<button type="button" class="btn btn-secondary btn-sm" id="pe-slug-example">' +
          esc(t('pe.log.slug_example')) + '</button></div>')
      }) +
      field({
        id: 'pe-log-format',
        label: t('pe.log.format'),
        control: textInput({ id: 'pe-log-format', value: spec.log_format || 'json_cache' })
      }) +
    '</div>' +
    '<p class="field-hint">' + esc(t('pe.log.copy_hint')) + '</p>' +
    '<p class="field-hint" id="pe-slug-preview"></p>' +
    '</div>' +
  '</div>' +

  '<div class="action-bar">' +
    '<button type="button" class="btn btn-primary" id="pe-save-2">' + esc(t('pe.save')) + '</button>' +
    '<span class="action-hint">' + esc(t('pe.id_hint', { origin: origin })) + '</span>' +
  '</div>';
}

// ---------------------------------------------------------------- 页面

export async function render(container, params) {
  const id = params && params.id ? String(params.id) : '';
  let alive = true;
  let spec = null;
  let extChips = null;

  container.innerHTML = loadingCard(t('common.loading'));

  try {
    spec = await api.getProcessor(id);
  } catch (e) {
    if (!alive) return function () {};
    container.innerHTML = errorState({
      title: t('proc.not_found', { id: id }),
      message: e && e.detail ? (typeof e.detail === 'string' ? e.detail : e.message) : t('err.unknown')
    });
    return function () {};
  }
  if (!alive) return function () {};
  if (!spec || typeof spec !== 'object') {
    container.innerHTML = errorState({ title: t('proc.not_found', { id: id }) });
    return function () {};
  }

  spec = clone(spec);

  function dirty(flag) {
    const el = container.querySelector('#pe-dirty');
    if (el) el.hidden = !flag;
  }

  /** 从 DOM 收集完整 spec（结构数组以 DOM 为准，保证「改完再点增删」不丢输入）。 */
  function collect() {
    const next = clone(spec);
    const nameEl = container.querySelector('#pe-name');
    const descEl = container.querySelector('#pe-desc');
    const kindEl = container.querySelector('#pe-kind');
    const enabledEl = container.querySelector('#pe-enabled');
    if (nameEl) next.name = nameEl.value.trim();
    if (descEl) next.description = descEl.value.trim();
    if (kindEl) next.kind = kindEl.value;
    if (enabledEl) next.enabled = !!enabledEl.checked;

    const naming = next.naming || {};
    naming.segments = Array.prototype.map.call(container.querySelectorAll('#pe-seg-list .seg-row'), function (rowEl) {
      const type = rowEl.querySelector('[id^="seg-type-"]').value;
      if (type === 'literal') {
        return { literal: rowEl.querySelector('[id^="seg-literal-"]').value };
      }
      const seg = { param: rowEl.querySelector('[id^="seg-param-"]').value };
      const pad = rowEl.querySelector('[id^="seg-pad-"]');
      if (pad && pad.value !== '') seg.pad = Number(pad.value);
      return seg;
    });
    const expressions = {};
    Array.prototype.forEach.call(container.querySelectorAll('#pe-expr-list .expr-row'), function (rowEl) {
      const key = rowEl.querySelector('[id^="expr-key-"]').value.trim();
      const value = rowEl.querySelector('[id^="expr-value-"]').value;
      if (key) expressions[key] = value;
    });
    naming.expressions = expressions;
    next.naming = naming;

    const traverse = next.traverse || {};
    const depthEl = container.querySelector('#pe-title-depth');
    const scopeEl = container.querySelector('#pe-file-scope');
    const zeroEl = container.querySelector('#pe-skip-zero');
    if (depthEl) traverse.title_depth = depthEl.value === '' ? 1 : Number(depthEl.value);
    if (scopeEl) traverse.file_scope = scopeEl.value;
    if (zeroEl) traverse.skip_season_zero = !!zeroEl.checked;
    traverse.accept_dirs = Array.prototype.map.call(container.querySelectorAll('#pe-accept-list .re-row'), function (rowEl) {
      const row = { pattern: rowEl.querySelector('[id^="acc-pattern-"]').value };
      const sg = rowEl.querySelector('[id^="acc-season-"]').value.trim();
      if (sg !== '') row.season_group = /^\d+$/.test(sg) ? Number(sg) : sg;
      return row;
    });
    traverse.reject_dirs = Array.prototype.map.call(container.querySelectorAll('#pe-reject-list .re-row'), function (rowEl) {
      return { pattern: rowEl.querySelector('[id^="rej-pattern-"]').value };
    });
    next.traverse = traverse;

    next.extensions = extChips ? extChips.getValues() : (Array.isArray(next.extensions) ? next.extensions : []);

    next.episode_patterns = Array.prototype.map.call(container.querySelectorAll('#pe-ep-list .ep-row'), function (rowEl) {
      return {
        name: rowEl.querySelector('[id^="ep-name-"]').value.trim(),
        pattern: rowEl.querySelector('[id^="ep-pattern-"]').value
      };
    });
    const fbEl = container.querySelector('#pe-ep-fallback');
    // 输入框清空就是"没配兜底" —— 提交 null，不要送空串。
    // 空串会被服务端当成"配置了兜底"，把集数渲染成 00（…S01E00.mkv）。
    if (fbEl) next.episode_fallback = fbEl.value.trim() ? fbEl.value : null;

    const perEl = container.querySelector('#pe-per');
    next.selection = { per: perEl ? perEl.value : ((next.selection && next.selection.per) || 'file') };

    const logEl = container.querySelector('#pe-log-file');
    const fmtEl = container.querySelector('#pe-log-format');
    if (logEl) next.log_file = logEl.value.trim();
    if (fmtEl) next.log_format = fmtEl.value.trim();
    return next;
  }

  function updatePreview() {
    const host = container.querySelector('#pe-preview');
    if (!host) return;
    const draft = collect();
    host.innerHTML = previewHtml(draft);
  }

  /**
   * 「日志文件名」旁边的样例按钮：优先展示**真的在用这个处理器的媒体库**里第一个的
   * 实际日志名；没有这样的库（或它的 log_file 为空）时退回**该处理器登记的基名**。
   * 注意：绝不能拿库名去猜 —— 服务端对自定义处理器的基名是 `{processor_id}cache.log`，
   * 按库名派生出来的名字（如 `librarycache.log`）永远不会真正出现。
   */
  function updateSlug() {
    const host = container.querySelector('#pe-slug-preview');
    if (!host) return;
    const logEl = container.querySelector('#pe-log-file');
    const base = String((logEl ? logEl.value : spec.log_file) || '').trim();
    const libs = (Array.isArray(state.libraries) ? state.libraries : []).filter(function (lib) {
      return lib && lib.processor_id === spec.id;
    });
    const lib = libs.length ? libs[0] : null;
    if (lib && lib.log_file) {
      host.textContent = t('pe.log.slug_preview', { name: lib.name, file: lib.log_file });
      return;
    }
    host.textContent = base ? t('pe.log.slug_base', { file: base }) : t('pe.log.slug_none');
  }

  function paint() {
    container.innerHTML = pageHtml(spec);
    extChips = createChipInput({
      id: 'pe-ext',
      values: Array.isArray(spec.extensions) ? spec.extensions : [],
      placeholder: '.mkv',
      addLabel: t('le.chip.add'),
      emptyText: t('pe.ext.empty'),
      onChange: function () { dirty(true); }
    });
    const host = container.querySelector('#pe-ext-host');
    if (host) host.appendChild(extChips.root);
    dirty(false);
    bind();
  }

  function mutate(fn) {
    spec = collect();
    fn(spec);
    paint();
  }

  /**
   * 客户端校验 —— **乐观提交**：它只做「明确非法」的即时反馈，绝不阻断保存。
   * 硬要求：只要后端 `validate` 说合法，客户端就不允许挡住保存；
   * 任何可能与后端方言/规则有出入的判断（尤其正则能否编译：JS `RegExp` 与 Python `re` 方言不同）
   * 都只能是**提示**，真源一律是后端 422 + 字段级中文 message。
   * 因此本函数**没有返回值、不参与 save() 的分支**。
   */
  function validate(draft) {
    clearFieldErrors(container);
    if (!draft.name) setFieldError(container, 'pe-name', t('pe.err.name'));
    if (!validLogFile(draft.log_file)) setFieldError(container, 'pe-log-file', t('pe.log.file.bad'));
    if (draft.kind === 'rename') {
      const segs = draft.naming && Array.isArray(draft.naming.segments) ? draft.naming.segments : [];
      if (!segs.length) setFieldError(container, 'pe-segments', t('pe.seg.empty'));
    }
    const exts = Array.isArray(draft.extensions) ? draft.extensions : [];
    if (!exts.length || exts.some(function (x) { return String(x).charAt(0) !== '.'; })) {
      setFieldError(container, 'pe-ext', t('pe.err.ext'));
    }
    // 正则只提示：徽章已在编辑时给出「正则无法编译」，这里补一条字段级提示，但不阻断保存。
    warnPattern(draft.traverse && draft.traverse.accept_dirs, 'pe-accept');
    warnPattern(draft.traverse && draft.traverse.reject_dirs, 'pe-reject');
    warnPattern(draft.episode_patterns, 'pe-episode');
    const keys = Object.keys(draft.naming.expressions || {});
    if (keys.some(function (k) { return !/^[A-Za-z_][A-Za-z0-9_]*$/.test(k); })) {
      setFieldError(container, 'pe-expressions', t('pe.expr.bad_key'));
    }
  }

  /** 正则编译提示：只写字段提示，不影响能否保存（后端才是真源）。 */
  function warnPattern(rows, field) {
    const list = Array.isArray(rows) ? rows : [];
    const bad = list.findIndex(function (r) { return regexError(r.pattern); });
    if (bad >= 0) {
      const box = container.querySelector('[data-field="' + field + '"] .field-error');
      if (box) box.textContent = t('pe.err.pattern_hint', { n: bad + 1 });
    }
  }

  /**
   * 后端字段路径 → 页面控件的映射（PROCESSORS §7：`field` 是点号路径，未知键就是键名本身、
   * 对象级错误是 `""`）。按**最长前缀**匹配；映射不到（含 `""`）一律进顶部通用错误区，
   * 原样保留后端的 message —— 后端以后新增字段时前端不会静默吞掉校验错误。
   */
  const FIELD_MAP = [
    // 通用错误区（编辑器顶部）
    ['schema_version', 'pe-form-error'],
    ['builtin', 'pe-form-error'],
    ['id', 'pe-form-error'],
    // 命名
    ['naming.expressions', 'pe-expressions'],
    ['naming.segments', 'pe-segments'],
    ['naming', 'pe-segments'],
    // 目录遍历
    ['traverse.accept_dirs', 'pe-accept'],
    ['traverse.reject_dirs', 'pe-reject'],
    ['traverse.title_depth', 'pe-traverse'],
    ['traverse.file_scope', 'pe-traverse'],
    ['traverse.skip_season_zero', 'pe-traverse'],
    ['traverse', 'pe-traverse'],
    // 其他
    ['extensions', 'pe-ext'],
    ['episode_patterns', 'pe-episode'],
    ['episode_fallback', 'pe-episode'],
    ['selection.per', 'pe-selection'],
    ['selection', 'pe-selection'],
    ['log_file', 'pe-log-file'],
    ['log_format', 'pe-log-format'],
    ['description', 'pe-form-error'],
    ['enabled', 'pe-form-error'],
    ['kind', 'pe-form-error'],
    ['name', 'pe-form-error']
  ].sort(function (a, b) { return b[0].length - a[0].length; });

  const REGEX_LISTS = { 'pe-accept': '#pe-accept-list', 'pe-reject': '#pe-reject-list', 'pe-episode': '#pe-ep-list' };

  function normalizePath(value) {
    let parts;
    if (Array.isArray(value)) parts = value.slice();
    else parts = String(value || '').split('.');
    if (parts.length && parts[0] === 'body') parts = parts.slice(1);
    return parts.filter(function (p) { return p !== '' && !/^\d+$/.test(p); }).join('.');
  }

  function showGeneralError(items) {
    const host = container.querySelector('#pe-form-error');
    const text = container.querySelector('#pe-form-error-text');
    if (!host || !text) return;
    text.textContent = '';
    if (!items.length) {
      host.hidden = true;
      return;
    }
    const ul = document.createElement('ul');
    ul.className = 'error-list';
    items.forEach(function (item) {
      const li = document.createElement('li');
      if (item.field) {
        const code = document.createElement('code');
        code.className = 'mono';
        code.textContent = item.field;
        li.appendChild(code);
        li.appendChild(document.createTextNode(' '));
      }
      li.appendChild(document.createTextNode(item.message));
      ul.appendChild(li);
    });
    text.appendChild(ul);
    host.hidden = false;
  }

  /** 把后端的字段级校验错误（422 的 `detail[]` 或 validate 的 `errors[]`）落到页面上。 */
  function applyServerErrors(err) {
    const rawDetail = err && err.detail;
    let list = null;
    if (rawDetail && Array.isArray(rawDetail.detail)) list = rawDetail.detail;
    else if (rawDetail && Array.isArray(rawDetail.errors)) list = rawDetail.errors;
    else if (Array.isArray(rawDetail)) list = rawDetail;
    if (!list || !list.length) {
      clearFieldErrors(container);
      showGeneralError([]);
      return false;
    }
    clearFieldErrors(container);
    const general = [];
    let shown = 0;
    list.forEach(function (item) {
      if (!item) return;
      const field = item.field !== undefined ? normalizePath(item.field) : normalizePath(item.loc);
      const message = item.message !== undefined ? item.message : (item.msg !== undefined ? item.msg : '');
      const hit = FIELD_MAP.find(function (pair) {
        return field === pair[0] || field.indexOf(pair[0] + '.') === 0;
      });
      const target = hit ? hit[1] : 'pe-form-error';
      if (target === 'pe-form-error') {
        general.push({ field: field, message: String(message) });
      } else {
        setFieldError(container, target, String(message));
        const listSel = REGEX_LISTS[target];
        if (listSel) overrideRegexStates(container.querySelector(listSel), String(message));
      }
      shown += 1;
    });
    showGeneralError(general);
    return shown > 0;
  }

  async function save() {
    const draft = collect();
    // 乐观提交：客户端校验只给即时提示（见 validate 的注释），不阻断保存；
    // 真正的合法性由后端 validate/422 判定，错误再按 field 回显。
    validate(draft);
    const btn = container.querySelector('#pe-save');
    const btn2 = container.querySelector('#pe-save-2');
    [btn, btn2].forEach(function (b) { if (b) b.disabled = true; });
    try {
      const saved = await api.updateProcessor(id, draft);
      spec = clone(saved);
      try {
        const [res, libs] = await Promise.all([api.listProcessors(), api.listLibraries()]);
        setProcessors(res.items);
        setLibraries(libs.items);
      } catch (e) { /* 缓存刷新失败不影响保存结果 */ }
      paint();
      toast(t('pe.saved'), 'success');
    } catch (e) {
      if (!applyServerErrors(e)) {
        toast(t('pe.save_failed', { msg: e && e.detail && typeof e.detail === 'string' ? e.detail : (e && e.message) || t('err.unknown') }), 'error');
      }
      [btn, btn2].forEach(function (b) { if (b && b.isConnected) b.disabled = false; });
    }
  }

  async function doValidate() {
    const draft = collect();
    const btn = container.querySelector('#pe-validate');
    if (btn) btn.disabled = true;
    try {
      const res = await api.validateProcessorSpec(draft);
      if (res && res.ok) {
        applyServerErrors(null);
        toast(t('pe.valid_ok'), 'success');
      } else {
        // 校验错误同样落到字段 / 顶部通用错误区（后端新增字段时不会静默吞掉）
        const errs = res && Array.isArray(res.errors) ? res.errors : [];
        const shown = applyServerErrors({ detail: { errors: errs } });
        const text = errs.map(function (x) { return (x.field ? x.field + ': ' : '') + x.message; }).join('；');
        if (!shown) toast(t('pe.valid_bad') + (text ? '：' + text : ''), 'warning', 8000);
      }
    } catch (e) {
      toast(t('pe.valid_bad') + '：' + ((e && e.message) || t('err.unknown')), 'error');
    } finally {
      if (btn && btn.isConnected) btn.disabled = false;
    }
  }

  function doReset() {
    const presets = resettablePresets(spec);
    if (!presets.length) return;
    let chosen = 'default';
    const body = document.createElement('div');
    body.innerHTML = '<p>' + esc(t('pe.reset.msg')) + '</p>' +
      '<div class="radio-group">' +
      presets.map(function (p) {
        return '<label class="radio"><input type="radio" name="pe-preset" value="' + p + '"' +
          (p === chosen ? ' checked' : '') + '>' +
          '<span><strong>' + esc(t('pe.reset.' + p)) + '</strong><br>' +
          '<span class="muted">' + esc(t('pe.reset.' + p + '.desc')) + '</span></span></label>';
      }).join('') + '</div>';
    body.addEventListener('change', function (ev) {
      if (ev.target && ev.target.name === 'pe-preset') chosen = ev.target.value;
    });
    confirmDialog({
      title: t('pe.reset.title'),
      message: body,
      confirmText: t('pe.reset'),
      cancelText: t('common.cancel')
    }).then(async function (yes) {
      if (!yes) return;
      try {
        const saved = await api.resetProcessor(id, chosen);
        spec = clone(saved);
        paint();
        toast(t('pe.reset.ok', { preset: chosen }), 'success');
      } catch (e) {
        toast(t('err.action_failed', { msg: (e && e.detail && typeof e.detail === 'string') ? e.detail : ((e && e.message) || t('err.unknown')) }), 'error');
      }
    });
  }

  function bind() {
    const mark = function () { dirty(true); };
    Array.prototype.forEach.call(
      container.querySelectorAll('#pe-name, #pe-desc, #pe-kind, #pe-enabled, #pe-title-depth, #pe-file-scope, #pe-skip-zero, #pe-per, #pe-log-file, #pe-log-format, #pe-ep-fallback'),
      function (el) {
        el.addEventListener('input', mark);
        el.addEventListener('change', mark);
      }
    );
    Array.prototype.forEach.call(
      container.querySelectorAll('#pe-seg-list input, #pe-seg-list select, #pe-expr-list input, #pe-accept-list input, #pe-reject-list input, #pe-ep-list input'),
      function (el) {
        el.addEventListener('input', function () { mark(); updatePreview(); });
        el.addEventListener('change', function () {
          mark();
          updatePreview();
          if (el.id.indexOf('seg-type-') === 0) mutate(function () {});
          if (el.id.indexOf('acc-pattern-') === 0 || el.id.indexOf('rej-pattern-') === 0 || el.id.indexOf('ep-pattern-') === 0) {
            // 正则随时反馈能否编译：重绘该行状态
            mutate(function () {});
          }
        });
      }
    );

    // 命名段：结构操作
    Array.prototype.forEach.call(container.querySelectorAll('#pe-seg-list .seg-row'), function (rowEl) {
      const index = Number(rowEl.getAttribute('data-index'));
      Array.prototype.forEach.call(rowEl.querySelectorAll('[data-act]'), function (btn) {
        btn.addEventListener('click', function () {
          const act = btn.getAttribute('data-act');
          mutate(function (draft) {
            const segs = draft.naming.segments;
            if (act === 'up' && index > 0) {
              const tmp = segs[index - 1];
              segs[index - 1] = segs[index];
              segs[index] = tmp;
            } else if (act === 'down' && index < segs.length - 1) {
              const tmp = segs[index + 1];
              segs[index + 1] = segs[index];
              segs[index] = tmp;
            } else if (act === 'remove') {
              segs.splice(index, 1);
            }
          });
        });
      });
    });
    ['pe-seg-add-param', 'pe-seg-add-literal'].forEach(function (btnId) {
      const btn = container.querySelector('#' + btnId);
      if (!btn) return;
      btn.addEventListener('click', function () {
        mutate(function (draft) {
          if (!draft.naming || typeof draft.naming !== 'object') draft.naming = {};
          if (!Array.isArray(draft.naming.segments)) draft.naming.segments = [];
          draft.naming.segments.push(btnId.indexOf('literal') >= 0 ? { literal: '' } : { param: 'title' });
        });
      });
    });

    // 命名模板：calibre 风格写一行 ⇒ 编译成段序列（段序列仍是唯一真源，可继续逐段微调）
    const templateInput = container.querySelector('#pe-template');
    Array.prototype.forEach.call(container.querySelectorAll('#pe-vars .var-btn'), function (btn) {
      btn.addEventListener('click', function () {
        if (!templateInput) return;
        const token = '{' + btn.getAttribute('data-var') + '}';
        const start = templateInput.selectionStart === null ? templateInput.value.length : templateInput.selectionStart;
        const end = templateInput.selectionEnd === null ? start : templateInput.selectionEnd;
        templateInput.value = templateInput.value.slice(0, start) + token + templateInput.value.slice(end);
        const caret = start + token.length;
        if (templateInput.setSelectionRange) templateInput.setSelectionRange(caret, caret);
        templateInput.focus();
      });
    });
    const templateApply = container.querySelector('#pe-template-apply');
    // 模板里除了变量表，还能引用表达式名与引擎内部键（episode_resolved）——
    // 这份"认不认识"的清单必须与服务端 spec.known_context_names() 完全一致，
    // 否则界面会在本地先拦下一个服务端其实接受的名字。
    const templateKnown = knownContextNames(
      (spec.naming && spec.naming.expressions) || {}
    );
    if (templateInput) {
      // 边写边校验：写错的变量名不必等到点“生成”才报。
      templateInput.addEventListener('input', function () {
        const errors = templateErrors(String(templateInput.value || ''), templateKnown);
        if (errors.length) setFieldError(container, 'pe-template', errors.join(' '));
        else clearFieldErrors(container, 'pe-template');
      });
    }
    if (templateApply) {
      templateApply.addEventListener('click', function () {
        const text = templateInput ? String(templateInput.value || '') : '';
        const errors = templateErrors(text, templateKnown);
        if (errors.length) {
          setFieldError(container, 'pe-template', errors.join(' '));
          return;
        }
        if (!text.trim()) {
          setFieldError(container, 'pe-template', t('pe.template.empty'));
          return;
        }
        const compiled = compileTemplate(text);
        mutate(function (draft) {
          if (!draft.naming || typeof draft.naming !== 'object') draft.naming = {};
          draft.naming.segments = compiled;
        });
        // mutate() 会整表重绘（模板文本不在 spec 里，所以会被清掉）；把用户写的那行放回去。
        const again = container.querySelector('#pe-template');
        if (again) again.value = text;
      });
    }

    // 表达式
    const exprAdd = container.querySelector('#pe-expr-add');
    if (exprAdd) {
      exprAdd.addEventListener('click', function () {
        mutate(function (draft) {
          if (!draft.naming || typeof draft.naming !== 'object') draft.naming = {};
          if (!draft.naming.expressions || typeof draft.naming.expressions !== 'object') draft.naming.expressions = {};
          let n = 1;
          while (draft.naming.expressions['expr' + n] !== undefined) n += 1;
          draft.naming.expressions['expr' + n] = '';
        });
      });
    }
    Array.prototype.forEach.call(container.querySelectorAll('#pe-expr-list .expr-row'), function (rowEl) {
      const index = Number(rowEl.getAttribute('data-index'));
      const btn = rowEl.querySelector('[data-act="remove"]');
      if (!btn) return;
      btn.addEventListener('click', function () {
        mutate(function (draft) {
          const keys = Object.keys(draft.naming.expressions || {});
          delete draft.naming.expressions[keys[index]];
        });
      });
    });

    // 接受 / 拒绝 / 剧集号：增删
    [['pe-accept-add', 'accept_dirs', function () { return { pattern: '' }; }],
     ['pe-reject-add', 'reject_dirs', function () { return { pattern: '' }; }],
     ['pe-ep-add', 'episode_patterns', function () { return { name: '', pattern: '' }; }]
    ].forEach(function (spec3) {
      const btn = container.querySelector('#' + spec3[0]);
      if (!btn) return;
      btn.addEventListener('click', function () {
        mutate(function (draft) {
          if (spec3[1] === 'episode_patterns') {
            if (!Array.isArray(draft.episode_patterns)) draft.episode_patterns = [];
            draft.episode_patterns.push(spec3[2]());
          } else {
            if (!draft.traverse || typeof draft.traverse !== 'object') draft.traverse = {};
            if (!Array.isArray(draft.traverse[spec3[1]])) draft.traverse[spec3[1]] = [];
            draft.traverse[spec3[1]].push(spec3[2]());
          }
        });
      });
    });
    [['#pe-accept-list', 'accept_dirs'], ['#pe-reject-list', 'reject_dirs'], ['#pe-ep-list', 'episode_patterns']]
      .forEach(function (pair) {
        Array.prototype.forEach.call(container.querySelectorAll(pair[0] + ' .re-row, ' + pair[0] + ' .ep-row'), function (rowEl) {
          const index = Number(rowEl.getAttribute('data-index'));
          const btn = rowEl.querySelector('[data-act="remove"]');
          if (!btn) return;
          btn.addEventListener('click', function () {
            mutate(function (draft) {
              const list = pair[1] === 'episode_patterns' ? draft.episode_patterns : draft.traverse[pair[1]];
              list.splice(index, 1);
            });
          });
        });
      });

    [container.querySelector('#pe-save'), container.querySelector('#pe-save-2')].forEach(function (btn) {
      if (btn) btn.addEventListener('click', save);
    });
    const vbtn = container.querySelector('#pe-validate');
    if (vbtn) vbtn.addEventListener('click', doValidate);
    const rbtn = container.querySelector('#pe-reset');
    if (rbtn) rbtn.addEventListener('click', doReset);
    const slugBtn = container.querySelector('#pe-slug-example');
    if (slugBtn) slugBtn.addEventListener('click', updateSlug);
    updateSlug();
  }

  paint();

  // 冷启动守卫：直接刷新 `#/processors/{id}` 时 store 里还没有库列表，日志名预览会退回
  // 「基名」分支（信息仍正确，但少了「现有库在用哪个名字」这条线索）。从处理器列表进入时
  // `pages/processors.js:239` 已经 setLibraries，所以只在为空时补拉这一次。
  // 不能放进 paint()：它会被 mutate()/save() 反复调用，那样就会重复请求。
  if (!(state.libraries || []).length) {
    api.listLibraries().then(function (res) {
      if (!alive) return;
      setLibraries(res.items);
      updateSlug();
    }).catch(function () {});
  }

  return function cleanup() {
    alive = false;
  };
}
