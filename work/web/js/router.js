// router.js —— 极简 hash 路由：支持 #/a/b/:id 形式的参数与每次渲染的清理回调

const routes = [];
let rootNode = null;
let onChange = null;
let currentCleanup = null;
let currentRoute = null;
// 每次 renderCurrent 递增。异步渲染返回时若序号已过期，说明它已被更晚的
// 渲染取代：此时必须立刻执行它自己的清理（否则其中的定时器会一直跑），
// 但绝不能把它的清理器登记为「当前清理器」。
let renderToken = 0;

/**
 * 注册路由。
 * pattern 形如 '/libraries/:id/edit'；params 值会被 decodeURIComponent。
 */
export function addRoute(pattern, handler, options) {
  routes.push({
    pattern: pattern,
    segments: pattern.split('/').filter(Boolean),
    handler: handler,
    options: options || {}
  });
}

function parseHash() {
  let raw = window.location.hash || '';
  if (raw.indexOf('#') === 0) raw = raw.slice(1);
  if (!raw || raw === '/') raw = '/dashboard';
  const qIndex = raw.indexOf('?');
  const query = {};
  if (qIndex >= 0) {
    raw.slice(qIndex + 1).split('&').forEach(function (pair) {
      if (!pair) return;
      const kv = pair.split('=');
      query[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || '');
    });
    raw = raw.slice(0, qIndex);
  }
  const path = raw.replace(/\/+$/, '') || '/';
  return { path: path, query: query };
}

function match(path) {
  const parts = path.split('/').filter(Boolean);
  for (let i = 0; i < routes.length; i += 1) {
    const route = routes[i];
    if (route.segments.length !== parts.length) continue;
    const params = {};
    let ok = true;
    for (let j = 0; j < route.segments.length; j += 1) {
      const seg = route.segments[j];
      if (seg.charAt(0) === ':') {
        params[seg.slice(1)] = decodeURIComponent(parts[j]);
      } else if (seg !== parts[j]) {
        ok = false;
        break;
      }
    }
    if (ok) return { route: route, params: params };
  }
  return null;
}

function runCleanup(fn, label) {
  if (typeof fn !== 'function') return;
  try {
    fn();
  } catch (e) {
    console.error(label, e);
  }
}

function renderCurrent() {
  const parsed = parseHash();
  const found = match(parsed.path);
  runCleanup(currentCleanup, '[router] cleanup error');
  currentCleanup = null;
  currentRoute = { path: parsed.path, query: parsed.query };
  const token = (renderToken += 1);

  if (!found) {
    renderNotFound(parsed.path);
    return;
  }
  const params = Object.assign({}, found.params, { query: parsed.query });
  if (onChange) onChange(currentRoute);
  try {
    const result = found.route.handler(rootNode, params);
    if (result && typeof result.then === 'function') {
      result
        .then(function (cleanup) {
          if (typeof cleanup !== 'function') return;
          if (token !== renderToken) {
            // 已被更晚的渲染取代：立刻释放它的资源，但不登记为当前清理器。
            runCleanup(cleanup, '[router] late cleanup error');
            return;
          }
          currentCleanup = cleanup;
        })
        .catch(function (err) {
          console.error('[router] render error', err);
          if (currentCleanup && typeof currentCleanup === 'function') {
            /* 渲染失败时保留已注册清理器 */
          }
        });
    } else if (typeof result === 'function') {
      if (token !== renderToken) runCleanup(result, '[router] late cleanup error');
      else currentCleanup = result;
    }
  } catch (e) {
    console.error('[router] render error', e);
  }
}

function renderNotFound(path) {
  if (typeof rootNode.__renderNotFound === 'function') {
    rootNode.__renderNotFound(path);
    return;
  }
  rootNode.innerHTML = '<div class="page"><div class="card empty-state">' +
    '<h2>404</h2><p class="muted">未找到页面：' + path + '</p>' +
    '<a class="btn btn-primary" href="#/dashboard">返回仪表盘</a></div></div>';
}

export function start(node, options) {
  rootNode = node;
  onChange = options && options.onChange ? options.onChange : null;
  window.addEventListener('hashchange', renderCurrent);
  renderCurrent();
}

export function reload() {
  renderCurrent();
}

export function navigate(path, replace) {
  const target = path.charAt(0) === '#' ? path : '#' + path;
  if (window.location.hash === target) {
    reload();
    return;
  }
  if (replace) {
    window.location.replace(target);
  } else {
    window.location.hash = target;
  }
}

export function current() {
  return currentRoute;
}

export function currentPath() {
  return parseHash().path;
}
