// pages/login.js —— 登录 / 首次设置管理员
// 布局：品牌头 → 文案说明 → 表单（用户名 / 密码 [/ 确认密码]）→ 提交 → 安全提示

import { t } from '../i18n.js';
import * as api from '../api.js';
import { state, setUser } from '../store.js';
import * as router from '../router.js';
import {
  html, raw, icon, field, textInput, setFieldError, clearFieldErrors, toast
} from '../ui.js';

function formHtml(setup) {
  return html`<form class="auth-form" id="auth-form" novalidate>
    ${field({
      id: 'username',
      label: t('login.username'),
      required: true,
      control: textInput({
        id: 'username',
        value: '',
        placeholder: t('login.username.ph'),
        autocomplete: 'username'
      })
    })}
    ${field({
      id: 'password',
      label: t('login.password'),
      required: true,
      control: textInput({
        id: 'password',
        type: 'password',
        value: '',
        placeholder: t('login.password.ph'),
        autocomplete: setup ? 'new-password' : 'current-password'
      })
    })}
    ${setup
      ? field({
          id: 'password2',
          label: t('login.confirm_password'),
          required: true,
          control: textInput({
            id: 'password2',
            type: 'password',
            value: '',
            placeholder: t('login.password.ph'),
            autocomplete: 'new-password'
          })
        })
      : raw('')}
    <button type="submit" class="btn btn-primary btn-block" id="auth-submit">
      ${raw(icon(setup ? 'check' : 'logout', 15))}
      <span>${setup ? t('login.submit_setup') : t('login.submit_login')}</span>
    </button>
  </form>`;
}

function pageHtml(setup) {
  return html`<div class="auth-page">
    <div class="auth-card">
      <div class="auth-brand">
        <span class="auth-mark">${raw(icon('library', 20))}</span>
        <div>
          <h1 class="auth-title">${t('app.name')}</h1>
          <p class="auth-sub">${t('app.tagline')}</p>
        </div>
      </div>
      <h2 class="auth-section">${setup ? t('login.setup_title') : t('login.title')}</h2>
      <p class="muted auth-desc">${setup ? t('login.setup_desc') : t('login.login_desc')}</p>
      ${raw(formHtml(setup))}
      <p class="auth-note muted">${raw(icon('info', 13))}<span>${t('login.note')}</span></p>
    </div>
  </div>`;
}

export async function render(container) {
  let setup = !!state.needsSetup;

  function paint() {
    container.innerHTML = pageHtml(setup);
    bind();
  }

  function bind() {
    const form = container.querySelector('#auth-form');
    const submit = container.querySelector('#auth-submit');
    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      clearFieldErrors(container);

      const username = (container.querySelector('#username').value || '').trim();
      const password = container.querySelector('#password').value || '';
      const password2 = setup ? (container.querySelector('#password2').value || '') : '';

      if (!username) {
        setFieldError(container, 'username', t('login.err.username_required'));
        container.querySelector('#username').focus();
        return;
      }
      if (password.length < 8) {
        setFieldError(container, 'password', t('login.err.password_short'));
        container.querySelector('#password').focus();
        return;
      }
      if (setup) {
        if (!password2) {
          setFieldError(container, 'password2', t('login.err.confirm_required'));
          return;
        }
        if (password2 !== password) {
          setFieldError(container, 'password2', t('login.err.password_mismatch'));
          return;
        }
      }

      submit.disabled = true;
      const label = submit.querySelector('span');
      const original = label.textContent;
      label.textContent = t('login.submitting');
      try {
        if (setup) {
          await api.authSetup(username, password);
          setup = false;
          state.needsSetup = false;
          toast(t('login.setup_ok'), 'success');
          paint();
          return;
        }
        await api.authLogin(username, password);
        setUser(username);
        toast(t('login.ok'), 'success');
        router.navigate('#/dashboard', true);
        return;
      } catch (err) {
        const msg = err && err.detail ? api.readableDetail(err.detail, err.status) : (err && err.message);
        const text = setup
          ? t('login.err.setup_failed', { msg: msg || t('err.unknown') })
          : t('login.err.failed', { msg: msg || t('err.unknown') });
        if (err && err.status === 401) {
          setFieldError(container, 'password', text);
          container.querySelector('#password').focus();
        } else {
          toast(text, 'error');
        }
      } finally {
        submit.disabled = false;
        label.textContent = original;
      }
    });

    const first = container.querySelector('#username');
    if (first) first.focus();
  }

  paint();

  return function cleanup() {
    // 只释放资源，不清空 DOM：内容由每次渲染负责覆盖，
    // 这样即使清理被更晚的渲染延迟调用，也不会抹掉新页面。
  };
}
