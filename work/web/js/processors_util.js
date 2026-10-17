// processors_util.js —— 处理器规格的纯前端辅助（命名段预览、摘要、slug）
//
// 契约：docs/contracts/PROCESSORS.md §3 / §3.1 / §8
//   naming = { expressions: {name: python表达式}, segments: [{param|literal, pad?}] }
//   上下文名字：title / filename / stem / ext / extname / season_raw / season / episode_raw /
//               episode / suffix / works_dir / rel_dir
//   段渲染：param 先查 expressions（前端无法求值 Python，只能显示占位符），再查上下文表；
//           pad ⇒ str(v).zfill(pad)

/** 上下文名字表（用于段编辑器的参数下拉与预览）。 */
export const PROC_PARAMS = [
  'title', 'filename', 'stem', 'ext', 'extname',
  'season_raw', 'season', 'episode_raw', 'episode', 'suffix',
  'works_dir', 'rel_dir'
];

/** 参数下拉里需要 pad 的（补零位数）——按 §3.1，pad 对任意返回值做 zfill，这里只提示常用项。 */
export const PAD_PARAMS = ['season', 'episode', 'season_raw', 'episode_raw'];

/**
 * 引擎内部键：不在变量表里、但段序列与表达式都读得到。
 * 与服务端 `app/processors/spec.py` 的 `ENGINE_ONLY_VARS` 必须一致（`tests/test_library_overrides.py`
 * 有防漂移断言）。`episode_resolved` = 这个文件有没有解析出集数。
 */
export const ENGINE_ONLY_PARAMS = ['episode_resolved'];

/**
 * 这次命名真正能读到的变量名 = 变量表 + 引擎内部键 + 表达式名。
 * 镜像服务端的 `spec.known_context_names()`：模板框与段编辑器用它判断"这个名字认不认识"，
 * 两边必须同口径，否则界面会拦下服务端其实接受的名字（或反过来）。
 */
export function knownContextNames(expressions) {
  const names = PROC_PARAMS.concat(ENGINE_ONLY_PARAMS);
  if (expressions && typeof expressions === 'object') {
    Object.keys(expressions).forEach(function (name) {
      if (names.indexOf(name) === -1) names.push(name);
    });
  }
  return names;
}

export const KINDS = ['rename', 'hardlink_only'];

export const CREATE_TEMPLATES = ['bangumi', 'movie', 'hardlink_only', 'blank'];

/** 示例上下文（用于实时预览；值是字符串，与后端 zfill 语义一致）。 */
export function sampleContext(suffix) {
  const suf = suffix || '.mkv';
  return {
    title: '示例作品',
    filename: '示例作品 01' + suf,
    stem: '示例作品 01',
    ext: suf,
    extname: suf.replace(/^\./, ''),
    season_raw: '01',
    season: '01',
    episode_raw: '03',
    episode: '03',
    suffix: suf,
    // 预览用的示例上下文；路径给宿主侧的真实形态（单挂载形态下容器路径 = 宿主路径）
    works_dir: '/vol1/1000/Download/qbittorrent/示例作品',
    rel_dir: ''
  };
}

function zfill(v, pad) {
  if (v === undefined || v === null) return '';
  const s = String(v);
  if (!pad || !/^\d+$/.test(String(pad))) return s;
  return s.padStart(Number(pad), '0');
}

/**
 * 按段序列拼出文件名（前端近似实现，仅用于预览）。
 * 返回 {text, expressions:[names]}：引用 expressions 的段前端无法求值，值为 {名字}。
 */
export function renderSegments(spec, ctx) {
  const naming = (spec && spec.naming) || {};
  const segments = Array.isArray(naming.segments) ? naming.segments : [];
  const expressions = (naming.expressions && typeof naming.expressions === 'object') ? naming.expressions : {};
  const usedExpressions = [];
  let text = '';
  segments.forEach(function (seg) {
    if (!seg || typeof seg !== 'object') return;
    if (seg.literal !== undefined && seg.literal !== null) {
      text += String(seg.literal);
      return;
    }
    const name = seg.param;
    if (!name) return;
    let value;
    if (Object.prototype.hasOwnProperty.call(ctx, name)) {
      value = ctx[name];
    } else if (Object.prototype.hasOwnProperty.call(expressions, name)) {
      if (usedExpressions.indexOf(name) < 0) usedExpressions.push(name);
      text += '{' + name + '}';
      return;
    } else {
      text += '{' + name + '}';
      return;
    }
    text += zfill(value, seg.pad);
  });
  return { text: text, expressions: usedExpressions };
}

/**
 * 处理器命名方式摘要（一行：示例 .mkv 的结果）。hardlink_only 或没有段时返回 ''。
 */
export function namingSummary(spec) {
  if (!spec || spec.kind === 'hardlink_only') return '';
  const segs = spec.naming && Array.isArray(spec.naming.segments) ? spec.naming.segments : [];
  if (!segs.length) return '';
  return renderSegments(spec, sampleContext('.mkv')).text;
}

/** 库名 → 日志文件名的**示例**占位值（仅用于表单提示；留空时真实名字由服务端按可用性分配）。 */
export function slugLogFile(name) {
  const slug = String(name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return (slug || 'library') + 'cache.log';
}

/** log_file 只允许文件名（不得含 / 或 ..），与 §5.2 一致。 */
export function validLogFile(value) {
  const v = String(value || '');
  if (!v) return true; // 留空 ⇒ 交给服务端按库分配唯一名，客户端不预判
  if (v.indexOf('/') >= 0 || v.indexOf('\\') >= 0) return false;
  if (v.indexOf('..') >= 0) return false;
  return true;
}

/** 编程器 id 规则：^[a-z][a-z0-9_-]{1,31}$ */
export function validProcessorId(id) {
  return /^[a-z][a-z0-9_-]{1,31}$/.test(String(id || ''));
}

/**
 * 把 Python `re` 里 JS 不认识的写法剔除，仅用于「能否编译」的提示：
 * 内联标志 `(?i)`（Python 允许，JS 不允许）、作用域内联标志 `(?i:…)`、
 * Python 命名组 `(?P<name>…)` 与命名反向引用 `(?P=name)`。
 */
export function toJsPattern(pattern) {
  return String(pattern || '')
    .replace(/\(\?[aiLmsux]+\)/g, '')
    .replace(/\(\?[aiLmsux]+:/g, '(?:')
    .replace(/\(\?P<[^>]*>/g, '(?:')
    .replace(/\(\?P=[^)]*\)/g, '(?:)');
}

// ── 命名模板（calibre 风格的 {字段} 拼装）──────────────────────────────
//
// 契约：docs/contracts/PROCESSORS.md §3.2。这里是 app/processors/spec.py 的
// `compile_template` / `template_errors` 的**前端镜像**（只用于预览与即时提示；
// 真正的校验与落库永远以服务端为准）。两边必须给出同一串段序列 ——
// tests/test_library_overrides.py 会用两边都认的例子比对。

const TEMPLATE_TOKEN = /\{([^{}]*)\}/y;

/** `{name}` / `{name:0N}` / `{{` / `}}` 编译成段序列（与 spec.py 逐字对应）。 */
export function compileTemplate(text) {
  const source = typeof text === 'string' ? text : '';
  const segments = [];
  let buffer = '';
  let index = 0;

  function flush() {
    if (buffer) {
      segments.push({ literal: buffer });
      buffer = '';
    }
  }

  while (index < source.length) {
    const char = source.charAt(index);
    if (char !== '{') {
      if (char === '}' && source.substr(index, 2) === '}}') {
        buffer += '}';
        index += 2;
        continue;
      }
      buffer += char;
      index += 1;
      continue;
    }
    if (source.substr(index, 2) === '{{') {
      buffer += '{';
      index += 2;
      continue;
    }
    TEMPLATE_TOKEN.lastIndex = index;
    const match = TEMPLATE_TOKEN.exec(source);
    if (!match || match.index !== index) {
      // 落单的 "{"：当字面量处理，避免"写了个 { 就整份模板失效"。
      buffer += '{';
      index += 1;
      continue;
    }
    const body = String(match[1] || '').trim();
    index = match.index + match[0].length;
    const parts = body.split(':');
    const name = String(parts[0] || '').trim();
    const padText = parts.length > 1 ? parts.slice(1).join(':').trim() : '';
    if (!name) {
      buffer += match[0];
      continue;
    }
    const segment = { param: name };
    if (padText) {
      const width = padText.replace(/^0+/, '') || '0';
      if (/^\d+$/.test(width) && Number(width) >= 1) segment.pad = Number(width);
    }
    flush();
    segments.push(segment);
  }
  flush();
  const kept = segments.filter(function (item) { return item.literal !== ''; });
  return kept.length ? kept : [{ literal: '' }];
}

/** 模板体检：返回中文错误列表（空 = 合法），文案与 spec.py 的 template_errors 对齐。 */
export function templateErrors(text, knownNames) {
  if (text === undefined || text === null) return [];
  if (typeof text !== 'string') return ['命名模板必须是字符串。'];
  const names = Array.isArray(knownNames) ? knownNames : PROC_PARAMS;
  const known = {};
  names.forEach(function (n) { known[n] = true; });
  const errors = [];
  const probe = text.replace(/\{\{|\}\}/g, '');
  const re = /\{([^{}]*)\}/g;
  let match;
  while ((match = re.exec(probe)) !== null) {
    const body = String(match[1] || '').trim();
    if (!body) {
      errors.push('模板里出现了空的 {}，请写变量名或对花括号转义为 {{ }}。');
      continue;
    }
    const parts = body.split(':');
    const name = String(parts[0] || '').trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
      errors.push('模板变量名不合法：{' + body + '}（变量名只能是字母、数字、下划线，且不以数字开头）。');
      continue;
    }
    if (!known[name]) {
      errors.push('模板里用了未知变量 {' + name + '}。可用变量：' + names.join('、') + '。');
    }
    const padText = parts.length > 1 ? parts.slice(1).join(':').trim() : '';
    if (padText) {
      const width = padText.replace(/^0+/, '') || '0';
      if (!/^\d+$/.test(width) || Number(width) < 1) {
        errors.push('模板变量 {' + body + '} 的补零写法不合法：只支持 {名称:0位数}，例如 {episode:02}。');
      }
    }
  }
  return errors;
}

/** 模板 + 示例上下文 ⇒ 预览文本（走 compileTemplate + renderSegments，与后端同路）。 */
export function renderTemplate(text, ctx, expressions) {
  const spec = {
    naming: { segments: compileTemplate(text), expressions: expressions || {} }
  };
  return renderSegments(spec, ctx || sampleContext('.mkv'));
}

/**
 * 段序列 ⇒ 模板文本（compileTemplate 的逆运算，用于把处理器的命名方式带进模板输入框）。
 * 字面量里的花括号要转义回 `{{` / `}}`，否则再编译一次语义就变了。
 */
export function segmentsToTemplate(segments) {
  return (Array.isArray(segments) ? segments : []).map(function (seg) {
    if (!seg || typeof seg !== 'object') return '';
    if (seg.literal !== undefined && seg.literal !== null) {
      return String(seg.literal).replace(/\{/g, '{{').replace(/\}/g, '}}');
    }
    if (!seg.param) return '';
    const pad = Number(seg.pad);
    const width = pad >= 1 ? ':' + (pad < 10 ? '0' + pad : String(pad)) : '';
    return '{' + seg.param + width + '}';
  }).join('');
}

/**
 * 正则能否编译（返回 null 表示可以，否则返回错误信息）。
 * 这是**提示**不是真源：JS 与 Python 的正则方言不同，所以原始写法与剔除 Python 专有语法后的
 * 写法**都**编译不过才判定为错误——否则内置处理器默认的 `(?i)^…$` 会被误报为「无法编译」，
 * 并在客户端校验里挡住保存。真正的中文报错以 `POST /api/processors/validate` 为准。
 */
export function regexError(pattern) {
  if (!pattern) return 'empty';
  try {
    // eslint-disable-next-line no-new
    new RegExp(pattern);
    return null;
  } catch (e) {
    // 原始写法 JS 编译不过：再看剔除 Python 专有语法后能否编译
  }
  try {
    // eslint-disable-next-line no-new
    new RegExp(toJsPattern(pattern));
    return null;
  } catch (e2) {
    return e2 && e2.message ? e2.message : 'invalid';
  }
}
