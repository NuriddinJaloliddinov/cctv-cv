/* Node-only DOM stub: persistence, switching, and newly rendered labels. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

for (const page of ['index.html', 'login.html']) {
  const html = fs.readFileSync(`frontend/${page}`, 'utf8');
  assert.match(html, /data-language-switch/);
  assert.ok(html.indexOf('/i18n.js') < html.indexOf(page === 'index.html' ? '/app.js' : '/login.js'));
}

const Node = { ELEMENT_NODE: 1, TEXT_NODE: 3 };
const NodeFilter = { SHOW_ELEMENT: 1, SHOW_TEXT: 4 };
const root = {
  nodeType: Node.ELEMENT_NODE,
  closest() { return null; },
  hasAttribute() { return false; },
  querySelectorAll() { return [select]; },
};
const text = (value) => ({ nodeType: Node.TEXT_NODE, nodeValue: value, parentElement: root });
const overview = text('Overview');
const notice = text('  You are signed in with a read-only account. Configuration, accounts and\n other changes are disabled. ');
const children = [overview, notice];
const select = { value: 'en' };
let mutationCallback;
const saved = new Map([['cctv-language', 'ru']]);
const document = {
  body: root, documentElement: { lang: 'en' },
  querySelectorAll() { return [select]; },
  addEventListener() {},
  createTreeWalker() {
    let index = -1;
    return { nextNode() { this.currentNode = children[++index]; return !!this.currentNode; } };
  },
};
const window = { dispatchEvent() {} };
class MutationObserver {
  constructor(callback) { mutationCallback = callback; }
  observe() {}
}
vm.runInNewContext(fs.readFileSync('frontend/i18n.js', 'utf8'), {
  Node, NodeFilter, document, window, MutationObserver,
  Event: class {},
  localStorage: { getItem: (key) => saved.get(key), setItem: (key, value) => saved.set(key, value) },
});

assert.equal(document.documentElement.lang, 'ru');
assert.equal(select.value, 'ru');
assert.equal(overview.nodeValue, 'Обзор');
assert.match(notice.nodeValue, /Вы вошли в режиме просмотра/);

const dynamic = text('Delete all');
children.push(dynamic);
mutationCallback([{ type: 'childList', addedNodes: [dynamic] }]);
assert.equal(dynamic.nodeValue, 'Удалить все');
dynamic.nodeValue = 'Delete';
mutationCallback([{ type: 'characterData', target: dynamic }]);
assert.equal(dynamic.nodeValue, 'Удалить');
window.dashboardI18n.setLanguage('uz');
assert.equal(dynamic.nodeValue, 'O‘chirish');
assert.equal(overview.nodeValue, 'Umumiy ko‘rinish');
assert.equal(saved.get('cctv-language'), 'uz');
window.dashboardI18n.setLanguage('en');
assert.equal(overview.nodeValue, 'Overview');
assert.equal(dynamic.nodeValue, 'Delete');
console.log('i18n switch tests passed');
