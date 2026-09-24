// Dependency-free DOM fixture for executing the production snapshot in Node's VM.
// Input is checked-in public HTML plus the exact JavaScript passed to Chromium.
const fs = require('node:fs');
const vm = require('node:vm');

const { html, script, url } = JSON.parse(fs.readFileSync(0, 'utf8'));
const anchors = [];
for (const match of html.matchAll(/<a\b([^>]*)>([^<]*)<\/a>/gi)) {
  const attrs = Object.fromEntries(
    [...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map((entry) => [entry[1], entry[2]])
  );
  const label = match[2];
  const href = attrs.href || '';
  const zeroSize = Object.hasOwn(attrs, 'data-zero-size');
  const element = {
    hidden: false,
    disabled: false,
    tagName: 'A',
    innerText: label,
    textContent: label,
    href: new URL(href, url).href,
    getAttribute: (name) => attrs[name] || null,
    setAttribute: (name, value) => { attrs[name] = value; },
    closest: () => null,
    getBoundingClientRect: () => ({ top: 20, bottom: zeroSize ? 20 : 40,
      height: zeroSize ? 0 : 20, width: zeroSize ? 0 : 80 }),
  };
  anchors.push(element);
}
const root = {
  innerText: html.replace(/<[^>]*>/g, ' '),
  querySelectorAll: (selector) => selector.includes('a[href]') ? anchors : [],
};
const document = {
  body: root,
  title: 'Public fixture',
  activeElement: null,
  documentElement: { scrollHeight: 800 },
  querySelector: (selector) => selector.includes('main') ? root : null,
};
const window = { innerHeight: 800, scrollY: 0, pageYOffset: 0 };
const context = { document, window, location: { href: url }, URL, CSS: { escape: String },
  Map, Set, WeakMap };
const snapshot = vm.runInNewContext(script, context, { timeout: 1000 });
process.stdout.write(JSON.stringify(snapshot));
