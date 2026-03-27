const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SETTINGS_JS_PATH = path.join(__dirname, '..', 'static', 'js', 'settings.js');

function createClassList() {
  const values = new Set();
  return {
    add(...items) {
      items.forEach((item) => values.add(item));
    },
    remove(...items) {
      items.forEach((item) => values.delete(item));
    },
    contains(item) {
      return values.has(item);
    },
  };
}

function createElementStub(overrides = {}) {
  return {
    value: '',
    checked: false,
    disabled: false,
    innerHTML: '',
    textContent: '',
    style: {},
    dataset: {},
    classList: createClassList(),
    addEventListener() {},
    removeEventListener() {},
    querySelectorAll() {
      return [];
    },
    querySelector() {
      return null;
    },
    reset() {},
    ...overrides,
  };
}

function createSandbox() {
  const elements = new Map();
  const calls = [];

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
    document: {
      getElementById(id) {
        return getElement(id);
      },
      querySelectorAll() {
        return [];
      },
      addEventListener() {},
      createElement() {
        return createElementStub();
      },
    },
    window: null,
    api: {
      get: async () => [],
      post: async () => ({ success: true }),
      patch: async () => ({ success: true }),
      delete: async (url) => {
        calls.push({ method: 'delete', url });
        return { success: true, deleted_count: 2, message: '已删除 2 个禁用代理' };
      },
    },
    toast: {
      success(message) {
        calls.push({ method: 'toast.success', message });
      },
      error(message) {
        calls.push({ method: 'toast.error', message });
      },
    },
    debounce(fn) {
      return fn;
    },
    confirm: async () => true,
    escapeHtml(value) {
      return String(value ?? '');
    },
    format: {
      date(value) {
        return value || '-';
      },
      number(value) {
        return String(value ?? 0);
      },
    },
    getServiceTypeText(value) {
      return value || '';
    },
    __calls: calls,
  };

  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SETTINGS_JS_PATH, 'utf8'), sandbox, { filename: 'settings.js' });
  return { sandbox, elements, calls };
}

test('renderProxies enables delete-disabled button only when disabled proxies exist', () => {
  const { sandbox } = createSandbox();
  const button = sandbox.document.getElementById('delete-disabled-proxies-btn');

  vm.runInContext(
    "renderProxies([{id:1,name:'a',type:'http',host:'127.0.0.1',port:8001,is_default:false,enabled:true,last_used:null}])",
    sandbox,
  );
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, '🧹 删除禁用项');

  vm.runInContext(
    "renderProxies([{id:1,name:'a',type:'http',host:'127.0.0.1',port:8001,is_default:false,enabled:false,last_used:null},{id:2,name:'b',type:'http',host:'127.0.0.1',port:8002,is_default:false,enabled:true,last_used:null}])",
    sandbox,
  );
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '🧹 删除禁用项 (1)');
});

test('handleDeleteDisabledProxies calls delete endpoint and refreshes list', async () => {
  const { sandbox, calls } = createSandbox();
  const button = sandbox.document.getElementById('delete-disabled-proxies-btn');
  button.disabled = false;

  vm.runInContext(
    "loadProxies = () => { __calls.push({ method: 'loadProxies' }); };",
    sandbox,
  );

  await vm.runInContext('handleDeleteDisabledProxies()', sandbox);

  assert.deepEqual(
    calls.map((call) => call.method),
    ['delete', 'toast.success', 'loadProxies'],
  );
  assert.equal(calls[0].url, '/settings/proxies/disabled');
});
