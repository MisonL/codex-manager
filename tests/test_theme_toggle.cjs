const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const UTILS_JS_PATH = path.join(__dirname, '..', 'static', 'js', 'utils.js');
const MOON_ICON = String.fromCodePoint(0x1F319);
const SUN_ICON = String.fromCodePoint(0x2600, 0xFE0F);

function createElementStub(overrides = {}) {
  let textContent = '';
  let innerHTML = '';
  const attributes = new Map();

  const element = {
    style: {},
    dataset: {},
    disabled: false,
    value: '',
    checked: false,
    className: '',
    children: [],
    parentElement: null,
    classList: {
      add() {},
      remove() {},
      toggle() {},
      contains() {
        return false;
      },
    },
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      this.children = this.children.filter((item) => item !== child);
    },
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    focus() {},
    select() {},
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    remove() {
      if (!this.parentElement) {
        return;
      }

      this.parentElement.removeChild(this);
    },
  };

  Object.defineProperty(element, 'textContent', {
    get() {
      return textContent;
    },
    set(value) {
      textContent = String(value);
      innerHTML = textContent;
    },
  });

  Object.defineProperty(element, 'innerHTML', {
    get() {
      return innerHTML;
    },
    set(value) {
      innerHTML = String(value);
      textContent = innerHTML;
    },
  });

  return Object.assign(element, overrides);
}

function createSandbox(savedTheme = null) {
  const themeButtons = [createElementStub(), createElementStub()];
  const domEventHandlers = new Map();
  const body = createElementStub();
  const documentElement = {
    attributes: new Map(),
    setAttribute(name, value) {
      this.attributes.set(name, String(value));
    },
    getAttribute(name) {
      return this.attributes.get(name) ?? null;
    },
  };

  const storage = new Map();
  if (savedTheme !== null) {
    storage.set('theme', savedTheme);
  }

  const sandbox = {
    console,
    setTimeout: () => 1,
    clearTimeout() {},
    fetch() {
      throw new Error('fetch should not be called in this test');
    },
    AbortController,
    navigator: {
      clipboard: {
        writeText() {
          return Promise.resolve();
        },
      },
    },
    localStorage: {
      getItem(key) {
        return storage.has(key) ? storage.get(key) : null;
      },
      setItem(key, value) {
        storage.set(key, String(value));
      },
      removeItem(key) {
        storage.delete(key);
      },
    },
    document: {
      body,
      documentElement,
      createElement() {
        return createElementStub();
      },
      addEventListener(type, handler) {
        domEventHandlers.set(type, handler);
      },
      getElementById() {
        return null;
      },
      querySelector() {
        return null;
      },
      querySelectorAll(selector) {
        if (selector === '.theme-toggle') {
          return themeButtons;
        }
        return [];
      },
      execCommand() {
        return true;
      },
    },
    window: null,
  };

  sandbox.window = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(UTILS_JS_PATH, 'utf8'), sandbox, { filename: 'utils.js' });

  return {
    sandbox,
    themeButtons,
    domEventHandlers,
    documentElement,
    storage,
  };
}

test('light theme initializes moon icon on load and DOMContentLoaded', () => {
  const { themeButtons, domEventHandlers, documentElement } = createSandbox('light');

  assert.equal(documentElement.getAttribute('data-theme'), 'light');
  themeButtons.forEach((button) => {
    assert.equal(button.textContent, MOON_ICON);
    assert.equal(button.title, '切换到暗色模式');
    assert.equal(button.getAttribute('aria-label'), '切换到暗色模式');
  });

  domEventHandlers.get('DOMContentLoaded')();

  themeButtons.forEach((button) => {
    assert.equal(button.textContent, MOON_ICON);
    assert.equal(button.title, '切换到暗色模式');
  });
});

test('dark theme initializes sun icon on load and DOMContentLoaded', () => {
  const { themeButtons, domEventHandlers, documentElement } = createSandbox('dark');

  assert.equal(documentElement.getAttribute('data-theme'), 'dark');
  themeButtons.forEach((button) => {
    assert.equal(button.textContent, SUN_ICON);
    assert.equal(button.title, '切换到亮色模式');
    assert.equal(button.getAttribute('aria-label'), '切换到亮色模式');
  });

  domEventHandlers.get('DOMContentLoaded')();

  themeButtons.forEach((button) => {
    assert.equal(button.textContent, SUN_ICON);
    assert.equal(button.title, '切换到亮色模式');
  });
});

test('theme.toggle flips theme, icon, and persisted value together', () => {
  const { sandbox, themeButtons, documentElement, storage } = createSandbox('light');

  vm.runInContext('theme.toggle()', sandbox);

  assert.equal(documentElement.getAttribute('data-theme'), 'dark');
  assert.equal(storage.get('theme'), 'dark');
  themeButtons.forEach((button) => {
    assert.equal(button.textContent, SUN_ICON);
    assert.equal(button.title, '切换到亮色模式');
  });

  vm.runInContext('theme.toggle()', sandbox);

  assert.equal(documentElement.getAttribute('data-theme'), 'light');
  assert.equal(storage.get('theme'), 'light');
  themeButtons.forEach((button) => {
    assert.equal(button.textContent, MOON_ICON);
    assert.equal(button.title, '切换到暗色模式');
  });
});
