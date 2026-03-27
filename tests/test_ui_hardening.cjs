const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP_JS_PATH = path.join(__dirname, '..', 'static', 'js', 'app.js');

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
  let innerHTML = '';
  let textContent = '';
  const element = {
    style: {},
    dataset: {},
    value: '',
    checked: false,
    disabled: false,
    className: '',
    children: [],
    parentElement: null,
    classList: createClassList(),
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      return child;
    },
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return createElementStub();
    },
    querySelectorAll(selector) {
      if (selector === '.log-line') {
        return this.children.filter((child) => child.className.includes('log-line'));
      }
      return [];
    },
    closest() {
      return null;
    },
    remove() {
      if (!this.parentElement) {
        return;
      }
      const index = this.parentElement.children.indexOf(this);
      if (index >= 0) {
        this.parentElement.children.splice(index, 1);
      }
    },
  };

  Object.defineProperty(element, 'childElementCount', {
    get() {
      return this.children.length;
    },
  });

  Object.defineProperty(element, 'firstElementChild', {
    get() {
      return this.children[0] || null;
    },
  });

  Object.defineProperty(element, 'innerHTML', {
    get() {
      return innerHTML;
    },
    set(value) {
      innerHTML = value;
    },
  });

  Object.defineProperty(element, 'textContent', {
    get() {
      return textContent;
    },
    set(value) {
      textContent = value;
      innerHTML = String(value);
    },
  });

  return Object.assign(element, overrides);
}

function createSandbox() {
  const elements = new Map();
  const formControls = [
    createElementStub({ id: 'email-service' }),
    createElementStub({ id: 'reg-mode' }),
    createElementStub({ id: 'start-btn' }),
    createElementStub({ id: 'cancel-btn' }),
  ];

  const registrationForm = createElementStub({
    id: 'registration-form',
    querySelectorAll(selector) {
      if (selector === 'input, select, textarea, button') {
        return formControls;
      }
      return [];
    },
  });

  const consoleLog = createElementStub({ id: 'console-log' });
  const registrationConfigPanel = createElementStub({ id: 'registration-config-panel' });
  const configLockBadge = createElementStub({ id: 'config-lock-badge' });
  const configLockNote = createElementStub({ id: 'config-lock-note' });
  const systemHealthBadge = createElementStub({ id: 'system-health-badge' });

  elements.set('registration-form', registrationForm);
  elements.set('email-service', formControls[0]);
  elements.set('reg-mode', formControls[1]);
  elements.set('start-btn', formControls[2]);
  elements.set('cancel-btn', formControls[3]);
  elements.set('console-log', consoleLog);
  elements.set('registration-config-panel', registrationConfigPanel);
  elements.set('config-lock-badge', configLockBadge);
  elements.set('config-lock-note', configLockNote);
  elements.set('system-health-badge', systemHealthBadge);

  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => 1,
    clearInterval: () => {},
    AbortController,
    fetch() {
      throw new Error('fetch should not be called in this test');
    },
    document: {
      getElementById(id) {
        if (!elements.has(id)) {
          elements.set(id, createElementStub({ id }));
        }
        return elements.get(id);
      },
      createElement() {
        return createElementStub();
      },
      addEventListener() {},
      querySelector() {
        return createElementStub();
      },
      querySelectorAll() {
        return [];
      },
    },
    sessionStorage: {
      getItem() {
        return null;
      },
      setItem() {},
      removeItem() {},
    },
    toast: {
      info() {},
      success() {},
      warning() {},
      error() {},
    },
    api: {
      get() {
        throw new Error('api.get should not be called in this test');
      },
      post() {
        throw new Error('api.post should not be called in this test');
      },
    },
    window: null,
    WebSocket: null,
  };

  sandbox.window = sandbox;
  sandbox.window.location = { protocol: 'http:', host: '127.0.0.1:8000' };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS_PATH, 'utf8'), sandbox, { filename: 'app.js' });

  return { sandbox, elements };
}

test('registration config lock disables mutable controls and keeps cancel enabled', () => {
  const { sandbox, elements } = createSandbox();

  vm.runInContext('setRegistrationConfigLock(true);', sandbox);

  assert.equal(elements.get('email-service').disabled, true);
  assert.equal(elements.get('reg-mode').disabled, true);
  assert.equal(elements.get('start-btn').disabled, true);
  assert.equal(elements.get('cancel-btn').disabled, false);
  assert.equal(elements.get('config-lock-badge').style.display, 'inline-flex');
  assert.equal(elements.get('config-lock-note').style.display, 'block');

  vm.runInContext('setRegistrationConfigLock(false);', sandbox);

  assert.equal(elements.get('email-service').disabled, false);
  assert.equal(elements.get('reg-mode').disabled, false);
  assert.equal(elements.get('start-btn').disabled, false);
  assert.equal(elements.get('cancel-btn').disabled, true);
});

test('log hardening keeps at most 1000 DOM nodes', () => {
  const { sandbox, elements } = createSandbox();

  vm.runInContext(
    `
      displayedLogs.clear();
      for (let index = 0; index < 1005; index += 1) {
        addLog('info', 'line-' + index);
      }
    `,
    sandbox,
  );

  const consoleLog = elements.get('console-log');
  assert.equal(consoleLog.childElementCount, 1000);
  assert.match(consoleLog.children[0].innerHTML, /line-5/);
  assert.match(consoleLog.children[999].innerHTML, /line-1004/);
});

test('system health badge reflects degraded status', () => {
  const { sandbox, elements } = createSandbox();

  vm.runInContext(
    `
      updateSystemHealthBadge({
        status: 'degraded',
        issues: ['event_loop_lag_high'],
        uptime_hms: '00:01:30',
      });
    `,
    sandbox,
  );

  assert.equal(elements.get('system-health-badge').textContent, 'System Degraded');
  assert.match(elements.get('system-health-badge').className, /degraded/);
  assert.match(elements.get('system-health-badge').title, /event_loop_lag_high/);
});
