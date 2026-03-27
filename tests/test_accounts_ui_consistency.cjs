const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ACCOUNTS_JS_PATH = path.join(__dirname, '..', 'static', 'js', 'accounts.js');

function createClassList() {
  const values = new Set();
  return {
    add(...items) {
      items.forEach((item) => values.add(item));
    },
    remove(...items) {
      items.forEach((item) => values.delete(item));
    },
    toggle(item, force) {
      if (force === undefined) {
        if (values.has(item)) {
          values.delete(item);
          return false;
        }
        values.add(item);
        return true;
      }
      if (force) {
        values.add(item);
        return true;
      }
      values.delete(item);
      return false;
    },
    contains(item) {
      return values.has(item);
    },
  };
}

function createElementStub(overrides = {}) {
  const listeners = new Map();
  const element = {
    style: {},
    dataset: {},
    value: '',
    checked: false,
    disabled: false,
    innerHTML: '',
    textContent: '',
    className: '',
    children: [],
    parentElement: null,
    classList: createClassList(),
    addEventListener(type, handler) {
      const handlers = listeners.get(type) || [];
      handlers.push(handler);
      listeners.set(type, handlers);
    },
    removeEventListener(type, handler) {
      const handlers = listeners.get(type) || [];
      listeners.set(
        type,
        handlers.filter((item) => item !== handler),
      );
    },
    dispatch(type, event = {}) {
      const handlers = listeners.get(type) || [];
      handlers.forEach((handler) => handler({ target: this, ...event }));
    },
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      return child;
    },
    querySelector() {
      return createElementStub();
    },
    querySelectorAll() {
      return [];
    },
    closest() {
      return null;
    },
  };

  return Object.assign(element, overrides);
}

function createSandbox() {
  const elements = new Map();
  const documentListeners = new Map();

  function getElement(id) {
    if (!elements.has(id)) {
      elements.set(id, createElementStub({ id }));
    }
    return elements.get(id);
  }

  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    requestAnimationFrame(callback) {
      callback();
      return 1;
    },
    cancelAnimationFrame() {},
    document: {
      getElementById(id) {
        return getElement(id);
      },
      createElement() {
        return createElementStub();
      },
      addEventListener(type, handler) {
        const handlers = documentListeners.get(type) || [];
        handlers.push(handler);
        documentListeners.set(type, handlers);
      },
      removeEventListener(type, handler) {
        const handlers = documentListeners.get(type) || [];
        documentListeners.set(
          type,
          handlers.filter((item) => item !== handler),
        );
      },
      dispatch(type, event = {}) {
        const handlers = documentListeners.get(type) || [];
        handlers.forEach((handler) => handler(event));
      },
    },
    window: null,
    api: {
      async get() {
        return [];
      },
      async post() {
        return { success: true };
      },
    },
    toast: {
      info() {},
      success() {},
      warning() {},
      error() {},
    },
    confirm: async () => true,
    debounce(fn) {
      return fn;
    },
    delegate() {},
    copyToClipboard() {},
    escapeHtml(value) {
      return String(value ?? '');
    },
    format: {
      number(value) {
        return String(value ?? 0);
      },
      date(value) {
        return value || '';
      },
    },
    getServiceTypeText(value) {
      return value || '';
    },
    getStatusIcon(value) {
      return value || '';
    },
  };

  sandbox.window = sandbox;
  sandbox.window.location = { protocol: 'http:', host: '127.0.0.1:8000' };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(ACCOUNTS_JS_PATH, 'utf8'), sandbox, { filename: 'accounts.js' });

  return { sandbox, elements, documentListeners };
}

test('renderAccounts empty state spans all visible columns', () => {
  const { sandbox, elements } = createSandbox();

  vm.runInContext('renderAccounts([])', sandbox);

  assert.match(elements.get('accounts-table').innerHTML, /colspan="11"/);
});

test('bindSimpleModal closes modal via close button and backdrop', () => {
  const { sandbox } = createSandbox();
  const modal = sandbox.document.getElementById('codex-auth-modal');
  const closeButton = sandbox.document.getElementById('close-codex-auth-modal');

  vm.runInContext("bindSimpleModal('codex-auth-modal', ['close-codex-auth-modal']);", sandbox);
  vm.runInContext("setAccountsModalOpen(document.getElementById('codex-auth-modal'), true);", sandbox);

  closeButton.dispatch('click');
  assert.equal(modal.classList.contains('active'), false);

  vm.runInContext("setAccountsModalOpen(document.getElementById('codex-auth-modal'), true);", sandbox);
  modal.dispatch('click', { target: modal });
  assert.equal(modal.classList.contains('active'), false);
});

test('selectNewapiService closes on escape and resolves null', async () => {
  const { sandbox, documentListeners } = createSandbox();

  const promise = vm.runInContext('selectNewapiService()', sandbox);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(sandbox.document.getElementById('newapi-service-modal').classList.contains('active'), true);

  const keydownHandlers = documentListeners.get('keydown') || [];
  const escapeHandler = keydownHandlers.find((handler) => typeof handler === 'function');
  escapeHandler({ key: 'Escape' });

  const result = await promise;
  assert.equal(result, null);
  assert.equal(sandbox.document.getElementById('newapi-service-modal').classList.contains('active'), false);
});
