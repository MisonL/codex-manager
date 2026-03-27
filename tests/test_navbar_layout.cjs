const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const rootDir = path.join(__dirname, '..');

function readFile(relativePath) {
  return fs.readFileSync(path.join(rootDir, relativePath), 'utf8');
}

test('shared pages keep navbar as full-width shell with inner container', () => {
  const templates = [
    'templates/index.html',
    'templates/accounts.html',
    'templates/email_services.html',
    'templates/payment.html',
    'templates/settings.html',
  ];

  templates.forEach((templatePath) => {
    const template = readFile(templatePath);
    assert.match(template, /<nav class="navbar">\s*<div class="container navbar-shell">/);
  });
});

test('navbar styles stay opaque and block backdrop bleed', () => {
  const css = readFile('static/css/style.css');
  const navbarBlock = css.match(/\.navbar\s*\{[\s\S]*?\n\}/)?.[0] ?? '';
  const navMetaBlock = css.match(/\.nav-meta\s*\{[\s\S]*?\n\}/)?.[0] ?? '';

  assert.match(css, /--navbar-background: #ffffff;/);
  assert.match(css, /--navbar-background: #111827;/);
  assert.match(navbarBlock, /background-color: var\(--navbar-background\);/);
  assert.match(navbarBlock, /z-index: 2000;/);
  assert.doesNotMatch(navbarBlock, /backdrop-filter/);
  assert.match(navMetaBlock, /padding-right: calc\(var\(--nav-meta-safe-space\) \+ env\(safe-area-inset-right, 0px\)\);/);
});
