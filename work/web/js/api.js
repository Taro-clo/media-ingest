// api.js —— 统一请求封装 + 全部后端接口（路径严格对应 docs/contracts/ARCHITECTURE.md §5）
// 依赖方向：api.js → i18n.js, ui.js, store.js（单向，无循环）。

import { t } from './i18n.js';
import { toast } from './ui.js';
import { clearUser } from './store.js';

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status || 0;
    this.detail = detail;
  }
}

let unauthorizedHandler = null;

/** 由 app.js 注册：401 时清理状态并跳转 #/login。 */
export function onUnauthorized(fn) {
  unauthorizedHandler = fn;
}

function buildQuery(query) {
  if (!query) return '';
  const parts = [];
  Object.keys(query).forEach(function (key) {
    const value = query[key];
    if (value === undefined || value === null || value === '') return;
    parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
  });
  return parts.length ? '?' + parts.join('&') : '';
}

/** 把后端的 detail（字符串 / FastAPI 422 数组 / 对象）转成中文可读文本。 */
export function readableDetail(data, status) {
  if (!data) return t('err.unknown');
  const detail = data.detail !== undefined ? data.detail : data;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(function (item) {
      if (typeof item === 'string') return item;
      if (item && item.msg) {
        const loc = Array.isArray(item.loc) ? item.loc.filter(function (p) {
          return p !== 'body';
        }).join('.') : '';
        return (loc ? loc + ': ' : '') + item.msg;
      }
      return JSON.stringify(item);
    }).join('；');
  }
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string') return detail.message;
    try {
      return JSON.stringify(detail);
    } catch (e) {
      return String(detail);
    }
  }
  if (status === 401) return t('err.unauthorized');
  return t('err.unknown');
}

/**
 * 统一请求。
 * opts: {method, body, query, silent, redirectOn401, headers, rawText}
 * 失败时抛出 ApiError；除非 silent=true 否则自动 toast。
 */
export async function apiFetch(path, opts) {
  const options = opts || {};
  const method = options.method || 'GET';
  const url = path + buildQuery(options.query);
  const headers = Object.assign({ Accept: 'application/json' }, options.headers || {});
  const init = {
    method: method,
    credentials: 'same-origin',
    headers: headers
  };
  if (options.body !== undefined && options.body !== null) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }

  let res;
  try {
    res = await fetch(url, init);
  } catch (e) {
    const err = new ApiError(t('err.network'), 0, e && e.message);
    if (!options.silent) toast(err.message, 'error');
    throw err;
  }

  let data = null;
  if (res.status !== 204) {
    const text = await res.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (e) {
        data = text;
      }
    }
  }

  if (res.status === 401) {
    // 个别接口用 401 表达「旧密码不对」而不是「会话失效」（目前只有改密码）：
    // 这类请求不能清用户态、不能弹「登录已失效」，也不走全局跳登录，
    // 由调用页面自己把 detail（如「当前密码不正确。」）显示在对应输入框下。
    if (options.resetAuthOn401 === false) {
      const err = new ApiError(readableDetail(data, 401), 401, data);
      if (!options.silent) toast(err.message, 'warning');
      throw err;
    }
    clearUser();
    if (options.redirectOn401 !== false && unauthorizedHandler) unauthorizedHandler();
    const err = new ApiError(t('err.unauthorized'), 401, readableDetail(data, 401));
    if (!options.silent) toast(err.message, 'warning');
    throw err;
  }

  if (!res.ok) {
    const message = readableDetail(data, res.status);
    const err = new ApiError(message, res.status, data);
    if (!options.silent) toast(t('err.action_failed', { msg: message }), 'error');
    throw err;
  }

  return data;
}

// ---------------------------------------------------------------- 归一化
// 列表信封已冻结（ARCHITECTURE.md §5.0）：GET /api/tasks 与 GET /api/libraries 统一返回
// {"items":[...], "total":N, "limit":L, "offset":O}。此处只做单一形态解析，不再多路回退。

function readEnvelope(data) {
  if (!data || typeof data !== 'object') return { items: [], total: 0, limit: null, offset: 0 };
  return {
    items: Array.isArray(data.items) ? data.items : [],
    total: typeof data.total === 'number' ? data.total : 0,
    limit: typeof data.limit === 'number' ? data.limit : null,
    offset: typeof data.offset === 'number' ? data.offset : 0
  };
}

// ---------------------------------------------------------------- 认证

export function authMe(opts) {
  return apiFetch('/api/auth/me', Object.assign({ silent: true, redirectOn401: false }, opts || {}));
}

export function authSetup(username, password) {
  return apiFetch('/api/auth/setup', {
    method: 'POST',
    body: { username: username, password: password },
    silent: true,
    redirectOn401: false
  });
}

export function authLogin(username, password) {
  return apiFetch('/api/auth/login', {
    method: 'POST',
    body: { username: username, password: password },
    silent: true,
    redirectOn401: false
  });
}

export function authLogout() {
  return apiFetch('/api/auth/logout', { method: 'POST', silent: true, redirectOn401: false });
}

export function authChangePassword(oldPassword, newPassword) {
  return apiFetch('/api/auth/password', {
    method: 'POST',
    body: { old_password: oldPassword, new_password: newPassword },
    silent: true,
    redirectOn401: false,
    resetAuthOn401: false
  });
}

// ---------------------------------------------------------------- 媒体库

export async function listLibraries() {
  const data = await apiFetch('/api/libraries');
  return readEnvelope(data);
}

export function createLibrary(payload) {
  return apiFetch('/api/libraries', { method: 'POST', body: payload });
}

export function getLibrary(id) {
  return apiFetch('/api/libraries/' + encodeURIComponent(String(id)));
}

export function updateLibrary(id, payload) {
  return apiFetch('/api/libraries/' + encodeURIComponent(String(id)), { method: 'PUT', body: payload });
}

/** 二次确认由界面负责；confirm=true 由本函数附加。 */
export function deleteLibrary(id) {
  return apiFetch('/api/libraries/' + encodeURIComponent(String(id)), {
    method: 'DELETE',
    query: { confirm: 'true' }
  });
}

/**
 * 预检。响应是**扁平对象**（ARCHITECTURE §5.0 第 9 条，v1 的 {ok,checks[]} 已作废）：
 * {ok, library_id, library_name, type, source_dir, target_dir, source_exists, target_exists,
 *  target_dir_exists, same_filesystem, same_filesystem_paths, source_folders,
 *  processor_id, processor_name, processor_kind, processor_ok, log_file, log_path,
 *  probe:{ok,note}, errors:[], warnings:[]}。
 * 注意 v2 起「处理器 + 日志文件校验」的键名是 **processor_ok**（v1 的 combination_ok 已不存在）。
 *
 * 现在 **`same_filesystem` 是 `bool | None`**（三级判据：真实 link 实证为主 → 挂载 ID 对比
 * 为预筛 → st_dev 兜底；`null` = 无法实证，例如源目录下没有可链的普通文件）。`probe.ok` 同样可能为
 * `null`；`probe.note` 是可执行的中文原文。调用方必须按三态渲染，**不得**把 `null` 折成 true/false
 * （折成 true 就是「预检假绿」，折成 false 就是误报跨挂载）。
 * `probe=true` 时后端只在**目标目录**里做真实 link 实证，不写源目录。
 */
export function validateLibrary(id, opts) {
  const probe = opts && opts.probe;
  return apiFetch('/api/libraries/' + encodeURIComponent(String(id)) + '/validate', {
    method: 'POST',
    query: probe ? { probe: 'true' } : undefined
  });
}

/**
 * POST /api/libraries/{id}/run → 202 + Task。
 * 注意：后端对 **force + 整库范围** 直接 400 —— force 只允许配合
 * `scope.mode='folder'`（单个作品）。整库重做的正道是先在作品记录里删掉要重做的作品记录。
 */
export function runLibrary(id, scope, force) {
  return apiFetch('/api/libraries/' + encodeURIComponent(String(id)) + '/run', {
    method: 'POST',
    body: { scope: scope || { mode: 'full' }, force: !!force }
  });
}

// ---------------------------------------------------------------- 作品记录（每库「已执行作品」账本）

/**
 * GET /api/libraries/{id}/works → 冻结信封 {items,total,limit,offset} **加** 库级元信息
 * （library_id/library_name/source_dir/target_dir/rename_log_path/rename_entries）与两本账的
 * 差集：`rename_missing`（账本有、重命名缓存没有 ⇒ 下次运行会补做重命名）、
 * `rename_only`（重命名缓存有、账本没有）。
 *
 * 分页与搜索都在**服务端**做 —— `params.q` 是作品名子串（大小写不敏感，
 * `%`/`_` 按字面处理），`params.limit`/`params.offset` 是分页窗口；`total` = **命中**条数
 * （翻页用它），`ledger_total` = 本库账本总条数（"筛选出了多少"用它）。
 *
 * readEnvelope() 只保留 items/total/limit/offset，所以这里显式把差集与元信息一并带出
 * （它们只是提示，后端不自动修复，前端也不该丢）。
 * 每行 = {folder, size, linked, source_dir, target_dir, target_path, first_at, updated_at,
 *          target_exists, rename_recorded, target_files, target_bytes, target_loose}。
 */
export async function listLibraryWorks(id, params) {
  const p = params || {};
  const data = await apiFetch('/api/libraries/' + encodeURIComponent(String(id)) + '/works', {
    query: { q: p.q || undefined, limit: p.limit, offset: p.offset }
  });
  const env = readEnvelope(data);
  return {
    items: env.items,
    total: env.total,
    limit: env.limit,
    offset: env.offset,
    q: data && typeof data.q === 'string' ? data.q : '',
    ledger_total: data && typeof data.ledger_total === 'number' ? data.ledger_total : env.total,
    library_id: data && data.library_id !== undefined ? data.library_id : id,
    library_name: data && data.library_name ? data.library_name : '',
    source_dir: data && data.source_dir ? data.source_dir : '',
    target_dir: data && data.target_dir ? data.target_dir : '',
    rename_log_path: data && data.rename_log_path ? data.rename_log_path : '',
    rename_entries: data && typeof data.rename_entries === 'number' ? data.rename_entries : 0,
    rename_missing: data && Array.isArray(data.rename_missing) ? data.rename_missing : [],
    rename_only: data && Array.isArray(data.rename_only) ? data.rename_only : []
  };
}

/**
 * POST /api/libraries/{id}/works/delete —— **破坏性**：删账本行 + 重命名缓存条目 +
 * 目标目录里整个作品文件夹（源目录不动）。
 *   opts.dryRun=true → 200 只预演（target_path/in_ledger/rename_recorded/exists/files/bytes/
 *                      loose_files），一个字节都不动；WebUI 用它填确认框；
 *   opts.confirm=true（且 dryRun=false）→ 200 真删，items 里多出 target_removed/error，
 *                      并带 ledger_removed / rename_removed / next_run_note。
 * dry_run=false 且 confirm 不为 true → 后端 400（拒绝执行）。
 */
export function deleteLibraryWorks(id, folders, opts) {
  const o = opts || {};
  return apiFetch('/api/libraries/' + encodeURIComponent(String(id)) + '/works/delete', {
    method: 'POST',
    body: { folders: folders || [], dry_run: !!o.dryRun, confirm: !!o.confirm },
    silent: !!o.silent
  });
}

/** POST /api/libraries/{id}/works/rename → 202 + Task（scope.mode='works'，只重命名不建链）。 */
export function renameLibraryWorks(id, folders) {
  return apiFetch('/api/libraries/' + encodeURIComponent(String(id)) + '/works/rename', {
    method: 'POST',
    body: { folders: folders || [] }
  });
}

// ---------------------------------------------------------------- 任务

/**
 * 任务列表。返回 {items, total, limit, offset}（冻结信封，§5.0）。
 */
export async function listTasks(params) {
  const p = params || {};
  const data = await apiFetch('/api/tasks', {
    query: {
      status: p.status,
      library_id: p.library_id,
      q: p.q,
      limit: p.limit,
      offset: p.offset
    }
  });
  return readEnvelope(data);
}

export function getTask(id) {
  return apiFetch('/api/tasks/' + encodeURIComponent(String(id)));
}

/**
 * 增量日志。返回 {items, last_id}；last_id 原样回传作下次 after_id（§5.0）。
 */
export async function getTaskLogs(id, afterId, limit) {
  const data = await apiFetch('/api/tasks/' + encodeURIComponent(String(id)) + '/logs', {
    query: { after_id: afterId === undefined ? 0 : afterId, limit: limit || 500 },
    silent: true
  });
  return {
    items: data && Array.isArray(data.items) ? data.items : [],
    last_id: data && typeof data.last_id === 'number' ? data.last_id : (afterId || 0)
  };
}

/** POST /api/tasks/{id}/retry → 202 + 完整 Task（仅 failed/cancelled，否则 409）。 */
export function retryTask(id) {
  return apiFetch('/api/tasks/' + encodeURIComponent(String(id)) + '/retry', { method: 'POST' });
}

/** POST /api/tasks/{id}/cancel → 204 无 body（仅 queued；running → 409）。 */
export function cancelTask(id) {
  return apiFetch('/api/tasks/' + encodeURIComponent(String(id)) + '/cancel', { method: 'POST' });
}

export function deleteTask(id) {
  return apiFetch('/api/tasks/' + encodeURIComponent(String(id)), { method: 'DELETE' });
}

// ---------------------------------------------------------------- 待办看板（§5.0/§6.4）

/**
 * 待办列表。返回 {items, total, limit, offset}（冻结信封，§5.0）。
 * status: 'open' | 'done' | 'all'；kind / library_id 不传 = 不过滤。
 * q：按作品筛选的**朴素子串**，匹配问题摘要 / 详情 / 去重键三列文本；空 = 不过滤。
 * limit 缺省 50（后端收敛到 1..200）。
 */
export async function listTodos(params) {
  const p = params || {};
  const data = await apiFetch('/api/todos', {
    query: {
      status: p.status,
      kind: p.kind,
      library_id: p.library_id,
      q: p.q,
      limit: p.limit,
      offset: p.offset
    }
  });
  return readEnvelope(data);
}

/**
 * GET /api/todos/summary → {open, done, total, by_status, by_kind, libraries,
 * enabled_kinds, disabled_kinds}（看板徽标与筛选下拉框的数据源）。
 */
export function todoSummary() {
  return apiFetch('/api/todos/summary');
}

/** POST /api/todos/{id}/resolve → 200 + 打钩后的条目。只改状态，删不掉任何数据。 */
export function resolveTodo(id) {
  return apiFetch('/api/todos/' + encodeURIComponent(String(id)) + '/resolve', { method: 'POST' });
}

/** POST /api/todos/{id}/reopen → 200 + 恢复成 open 的条目。 */
export function reopenTodo(id) {
  return apiFetch('/api/todos/' + encodeURIComponent(String(id)) + '/reopen', { method: 'POST' });
}

/**
 * GET /api/todos/detect（**只读**，不改任何状态）→ {checked, candidates:[...], candidate_total,
 * detectable_kinds, undetectable_kinds, skipped_missing_sample, truncated, ...}
 * 只扫未完成的条目；kind / library_id 不传 = 不限（limit 后端收敛到 1..200）。
 */
export function detectTodos(params) {
  const p = params || {};
  return apiFetch('/api/todos/detect', {
    query: {
      kind: p.kind,
      library_id: p.library_id,
      limit: p.limit,
      offset: p.offset
    }
  });
}

/**
 * POST /api/todos/detect-resolve {ids:[...]} → {resolved:[], skipped:[], requested}
 * 服务端会**逐个再核对一遍**：拿不出证据的条目原样留在未完成，并在 skipped 里说明原因。
 */
export function resolveDetectedTodos(ids) {
  return apiFetch('/api/todos/detect-resolve', {
    method: 'POST',
    body: { ids: ids || [] }
  });
}

// ---------------------------------------------------------------- 统计 / 设置 / 系统

export function statsOverview() {
  return apiFetch('/api/stats/overview');
}

export function getSettings() {
  return apiFetch('/api/settings');
}

/** 部分更新；值为 "***" 表示保持原值（由后端处理）。 */
export function putSettings(patch) {
  return apiFetch('/api/settings', { method: 'PUT', body: patch });
}

export function systemHealth() {
  return apiFetch('/api/system/health', { silent: true });
}

export function systemInfo() {
  return apiFetch('/api/system/info');
}


// ---------------------------------------------------------------- qBittorrent 连通性测试
// 契约（§5.0，已冻结）：POST /api/settings/test-qbittorrent
//   body 可选 {url, username, password}；缺省字段用已保存的设置（空 body 即可）。
//   响应 {ok, version, message, latency_ms}；连不上/认证失败仍是 HTTP 200，必须判 body.ok。

export function testQbittorrent(payload) {
  const body = {};
  if (payload && payload.url) body.url = payload.url;
  if (payload && payload.username) body.username = payload.username;
  if (payload && payload.password) body.password = payload.password;
  return apiFetch('/api/settings/test-qbittorrent', { method: 'POST', body: body, silent: true });
}

// ---------------------------------------------------------------- 处理器（v2，PROCESSORS.md §7）
// ProcessorSpec 的形状见 docs/contracts/PROCESSORS.md §3；列表与媒体库一样走 {items,total,limit,offset} 信封
// 之外的 {items,total}（§7 只承诺 items/total，故这里用 readEnvelope 取 items/total，忽略 limit/offset）。

/** GET /api/processors?builtin=&kind= → {items:[ProcessorSpec], total}。 */
export async function listProcessors(params) {
  const p = params || {};
  const data = await apiFetch('/api/processors', {
    query: {
      builtin: p.builtin === undefined || p.builtin === null ? undefined : String(p.builtin),
      kind: p.kind
    }
  });
  return readEnvelope(data);
}

export function getProcessor(id) {
  return apiFetch('/api/processors/' + encodeURIComponent(String(id)));
}

/**
 * 新建自定义处理器。body = {id, name, kind, template}，template ∈ "movie" | "blank"；
 * schema_version / builtin / 其余字段由服务端按模板补齐。成功 → 201 + ProcessorSpec。
 */
export function createProcessor(payload) {
  return apiFetch('/api/processors', { method: 'POST', body: payload });
}

/** 全量替换 spec（id 不可改，内置也允许改规则）。 */
export function updateProcessor(id, spec) {
  return apiFetch('/api/processors/' + encodeURIComponent(String(id)), { method: 'PUT', body: spec });
}

/** 内置处理器 → 409；被任何媒体库引用 → 409（错误体 detail 是中文原因，原样展示）。 */
export function deleteProcessor(id) {
  return apiFetch('/api/processors/' + encodeURIComponent(String(id)), { method: 'DELETE' });
}

/** preset ∈ "default" | "legacy"（legacy = 沿用 v1 的逐字行为、不含拒绝目录规则）。 */
export function resetProcessor(id, preset) {
  return apiFetch('/api/processors/' + encodeURIComponent(String(id)) + '/reset', {
    method: 'POST',
    body: { preset: preset }
  });
}

/** 仅校验 spec，不落库 → {ok, errors:[{field, message}]}。 */
export function validateProcessorSpec(spec) {
  return apiFetch('/api/processors/validate', { method: 'POST', body: spec, silent: true });
}

// ---------------------------------------------------------------- 配置文件同步（v2，PROCESSORS.md §6/§7）
// GET /api/config → {config_dir, files:[{path,relative,exists,sha256,in_sync,size,updated_at}],
//                    last_import_at, last_export_at, errors:[{path,message,at}]}

export function getConfig() {
  return apiFetch('/api/config', { silent: true });
}

/** ?force=true 时忽略 sha256 比对，强制从磁盘重新导入。 */
export function reloadConfig(force) {
  return apiFetch('/api/config/reload', {
    method: 'POST',
    query: force ? { force: 'true' } : undefined,
    silent: true
  });
}

/** 立即把 DB 里的库与处理器写出到配置文件。 */
export function exportConfig() {
  return apiFetch('/api/config/export', { method: 'POST', silent: true });
}
