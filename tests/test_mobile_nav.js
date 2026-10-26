/* Exercise the mobile drawer without browser dependencies. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('frontend/index.html', 'utf8');
const css = fs.readFileSync('frontend/style.css', 'utf8');
assert.match(html, /id="sidebar-toggle"[^>]*aria-label="Open menu"[^>]*aria-controls="app-sidebar"[^>]*aria-expanded="false"/);
assert.match(html, /id="sidebar-backdrop"[^>]*hidden/);
assert.match(css, /@media \(max-width: 820px\)[\s\S]*?body\.sidebar-open \.sidebar/);
assert.match(css, /prefers-reduced-motion: reduce/);

function element() {
  const attrs = {};
  const handlers = {};
  return {
    attrs, handlers, inert: false, hidden: false,
    addEventListener(type, fn) { handlers[type] = fn; },
    setAttribute(key, value) { attrs[key] = value; },
    removeAttribute(key) { delete attrs[key]; },
    focus() { document.activeElement = this; },
    getClientRects() { return [1]; },
    closest() { return null; },
    contains(target) { return target === closeButton || target === navLink; },
    querySelectorAll(query) { return query === '.nav-link' ? [navLink] : [closeButton, navLink]; },
    querySelector() { return accountActions; },
  };
}
const sidebar = element();
const toggle = element();
const backdrop = element();
const closeButton = element();
const navLink = element();
const accountActions = element();
const pageTitle = element();
backdrop.hidden = true;
const classes = new Set();
const document = {
  activeElement: null,
  body: { classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name) } },
  addEventListener() {},
};
const media = { matches: true, addEventListener() {} };
const nodes = { 'app-sidebar': sidebar, 'sidebar-toggle': toggle,
  'sidebar-backdrop': backdrop, 'sidebar-close': closeButton, 'page-title': pageTitle };
const app = fs.readFileSync('frontend/app.js', 'utf8');
const code = app.split('// The mobile navigation is a real drawer, not a horizontally scrolling menu.')[1]
  .split('// ============================================================\n//  Routing')[0];
const context = vm.createContext({
  window: { matchMedia: () => media }, document,
  $: (id) => nodes[id], openModals: new Set(), setTimeout: (fn) => fn(),
});
vm.runInContext(code, context);
assert.equal(sidebar.inert, true);
assert.equal(sidebar.attrs['aria-hidden'], 'true');
toggle.handlers.click();
assert.equal(toggle.attrs['aria-expanded'], 'true');
assert.equal(backdrop.hidden, false);
assert.equal(document.activeElement, closeButton);
assert.ok(classes.has('sidebar-open'));
backdrop.handlers.click();
assert.equal(toggle.attrs['aria-expanded'], 'false');
assert.equal(sidebar.inert, true);
assert.equal(backdrop.hidden, true);
assert.equal(document.activeElement, toggle);
toggle.handlers.click();
navLink.handlers.click();
assert.equal(document.activeElement, pageTitle);
assert.equal(sidebar.inert, true);
console.log('mobile drawer tests passed');
