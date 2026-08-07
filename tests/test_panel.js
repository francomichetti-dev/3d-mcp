// The chat panel's behaviour, driven the way the browser drives it.
//
// Runs the real <script> out of agent/static/index.html against a stub DOM and
// replays event sequences. Written because the panel had no tests: a spinner
// that would not stop had to be diagnosed by reading, which is exactly the
// situation every other part of this repo is set up to avoid.
//
//     node tests/test_panel.js

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeHarness } = require('./panel_harness');

let PASS = 0;
let FAIL = 0;

function check(label, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) { PASS++; return; }
  FAIL++;
  console.log(`  FAIL ${label}\n       got  ${JSON.stringify(got)}\n       want ${JSON.stringify(want)}`);
}
function truthy(label, got) { check(label, Boolean(got), true); }

const HTML = fs.readFileSync(
  path.join(__dirname, '..', 'agent', 'static', 'index.html'), 'utf8');

const script = HTML.match(/<script>([\s\S]*?)<\/script>/)[1];
const ids = [...HTML.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);

function start() {
  const h = makeHarness(ids);
  vm.createContext(h.sandbox);
  vm.runInContext(script, h.sandbox);
  // A design has to be on screen before anything else means anything.
  h.deliver({ type: 'document', key: 'design-1', name: 'tower', design: true,
              transcript: [], plan: [], busy: false });
  return h;
}

const banner = (h) => {
  const el = h.doc.getElementById('banner');
  return { hidden: el.hidden, done: el.classList.contains('done'),
           text: h.doc.getElementById('banner-text').textContent };
};
const plan = (h) => {
  const box = h.doc.getElementById('plan');
  return { hidden: box.hidden,
           count: h.doc.getElementById('plan-count').textContent,
           steps: h.doc.getElementById('plan-steps').children.map(
             (li) => [li.classList.value, li.textContent]) };
};

// ---------------------------------------------------------------- banner ---
// The reported bug: the spinner kept going after the build had finished.
console.log('The working banner');

let h = start();
check('nothing is showing before a turn starts', banner(h).hidden, true);

h.deliver({ type: 'tool', doc: 'design-1', name: 'fusion_execute',
            summary: 'fillets.add(inp)', verb: 'Filleting' });
check('a tool call names what it is doing', banner(h).text, 'Filleting');
check('and it is not marked done', banner(h).done, false);
check('and it is visible', banner(h).hidden, false);

h.deliver({ type: 'turn_end', doc: 'design-1' });
check('the turn ending says Done', banner(h).text, 'Done');
truthy('and marks the banner done, which is what hides the spinner',
       banner(h).done);

// The two orderings that could leave it spinning forever.
h = start();
h.deliver({ type: 'tool', doc: 'design-1', name: 'fusion_execute',
            summary: 'x', verb: 'Extruding' });
// turn_end for ANOTHER design must not be what stops this one...
h.deliver({ type: 'turn_end', doc: 'other-design' });
check('a turn ending elsewhere leaves this one alone', banner(h).done, false);
// ...and this design's own turn_end must still land afterwards.
h.deliver({ type: 'turn_end', doc: 'design-1' });
truthy('this design ending still stops it', banner(h).done);

// A turn_end with no design on it at all — belt and braces, since a dropped
// key would strand the spinner exactly as reported.
h = start();
h.deliver({ type: 'tool', doc: 'design-1', name: 'fusion_execute',
            summary: 'x', verb: 'Extruding' });
h.deliver({ type: 'turn_end' });
truthy('an unattributed turn_end still stops the spinner', banner(h).done);

// Switching to a design that is not building must not show a stale spinner.
h = start();
h.deliver({ type: 'tool', doc: 'design-1', name: 'fusion_execute',
            summary: 'x', verb: 'Extruding' });
h.deliver({ type: 'document', key: 'design-2', name: 'bracket', design: true,
            transcript: [], plan: [], busy: false });
check('switching to an idle design clears the banner', banner(h).hidden, true);

// ...but switching to one that IS building should show it.
h.deliver({ type: 'document', key: 'design-3', name: 'gear', design: true,
            transcript: [], plan: [], busy: true });
check('switching to a busy design shows it', banner(h).hidden, false);
check('generically, since no tool call has arrived yet', banner(h).text, 'Working');

// Replaying a transcript must not start a spinner for a build long finished.
h = start();
h.deliver({ type: 'document', key: 'design-4', name: 'old', design: true,
            busy: false, plan: [],
            transcript: [{ type: 'tool', name: 'fusion_execute',
                           summary: 'x', verb: 'Filleting' }] });
check('a replayed transcript leaves the banner alone', banner(h).hidden, true);


// The bug as reported: "spinner keeps spinning also when the design has
// finished". Every in-band sequence above stops it correctly, which left one
// way for it to strand — the stream carrying turn_end dying before turn_end
// arrives. Restarting the service does exactly that to an open panel.
console.log('A dropped connection');

h = start();
h.deliver({ type: 'tool', doc: 'design-1', name: 'fusion_execute',
            summary: 'x', verb: 'Filleting' });
truthy('the spinner is running before the drop', !banner(h).hidden);
h.drop();
check('a dropped stream stops claiming progress',
      h.doc.getElementById('banner').classList.contains('lost'), true);
check('and does not pretend the build finished', banner(h).done, false);
truthy('it says what happened', banner(h).text.includes('Connection lost'));

// Reconnecting: the service sends the current state as its first message, so
// the panel re-syncs from that rather than staying stuck.
h.reconnect();
h.deliver({ type: 'document', key: 'design-1', name: 'tower', design: true,
            transcript: [], plan: [], busy: false });
check('reconnecting to a finished build clears it', banner(h).hidden, true);
check('and the lost state is gone',
      h.doc.getElementById('banner').classList.contains('lost'), false);

// A drop while nothing is running should not invent a warning.
h = start();
h.drop();
check('a drop while idle stays quiet', banner(h).hidden, true);


// ------------------------------------------------------------------ plan ---
console.log('The build plan');

h = start();
check('no plan, no box', plan(h).hidden, true);

h.deliver({ type: 'plan', doc: 'design-1', steps: [
  { title: 'baseplate', status: 'done' },
  { title: 'tower body', status: 'doing' },
  { title: 'battlements', status: 'todo' },
]});
const p = plan(h);
check('the box appears', p.hidden, false);
check('with a count of what is finished', p.count, '1/3');
check('and a row per step, in order', p.steps.map((s) => s[0]),
      ['done', 'doing', 'todo']);
truthy('showing the titles', p.steps[1][1].includes('tower body'));

// The model sends the whole list every time, so an update must replace.
h.deliver({ type: 'plan', doc: 'design-1', steps: [
  { title: 'baseplate', status: 'done' },
  { title: 'tower body', status: 'done' },
]});
check('an update replaces rather than appends', plan(h).steps.length, 2);
check('and recounts', plan(h).count, '2/2');

// A plan belongs to its design.
h.deliver({ type: 'document', key: 'design-2', name: 'bracket', design: true,
            transcript: [], plan: [], busy: false });
check('switching to a design with no plan hides the box', plan(h).hidden, true);

h.deliver({ type: 'document', key: 'design-1', name: 'tower', design: true,
            transcript: [], busy: false,
            plan: [{ title: 'baseplate', status: 'done' }] });
check('switching back redraws that design plan', plan(h).count, '1/1');

// Whatever the model sends, the panel must not break.
h.deliver({ type: 'plan', doc: 'design-1', steps: [] });
check('an empty plan hides the box', plan(h).hidden, true);
h.deliver({ type: 'plan', doc: 'design-1' });
check('a plan event with no steps at all is survivable', plan(h).hidden, true);
h.deliver({ type: 'plan', doc: 'design-1', steps: [{ title: 'x' }] });
check('a step with no status renders as todo', plan(h).steps[0][0], 'todo');

// ------------------------------------------------------- drag and drop -----
// Reported as "drag and drop images into the chat is not working".
//
// A drop only ever reaches the page if dragover called preventDefault first.
// The panel guarded that on dataTransfer.types containing 'Files', which is
// what a normal browser reports — but the palette is a CEF webview inside
// Fusion, and a host that describes the drag any other way meant the guard
// never fired, the browser kept its default handling, and no drop event was
// ever delivered. Nothing in the panel was broken; it simply never ran.
console.log('Drag and drop');

h = start();
const png = { name: 'ref.png', type: 'image/png' };

// The shape a normal browser sends.
truthy('dragover is accepted when the host says Files',
       h.fire('dragover', { dataTransfer: { types: ['Files'], files: [png] } }));

// The shapes that used to be ignored. Each one is a real host: some report an
// empty type list, some report a MIME type, some populate items instead.
truthy('...and when it reports no types at all',
       h.fire('dragover', { dataTransfer: { types: [], files: [png] } }));
truthy('...and when it names the MIME type instead',
       h.fire('dragover', { dataTransfer: { types: ['image/png'], files: [png] } }));
truthy('...and when only items is populated',
       h.fire('dragover', { dataTransfer: { types: [], items: [{ kind: 'file' }], files: [] } }));

// Text being dragged is not an attachment and must keep its normal behaviour,
// or dragging a selection into the input box stops working.
check('dragging plain text is left alone',
      h.fire('dragover', { dataTransfer: { types: ['text/plain'], files: [] } }), false);

// And the drop itself has to reach addFiles.
// The drop itself must be accepted. Asserting on the tray would mean driving
// the whole async shrink-and-encode path; what actually broke is upstream of
// that — whether the panel takes the drop at all.
h = start();
truthy('a dropped image is accepted',
       h.fire('drop', { dataTransfer: { types: ['Files'], files: [png] } }));
truthy('...even from a host that only fills items',
       h.fire('drop', { dataTransfer: { types: [], files: [],
                                        items: [{ kind: 'file', getAsFile: () => png }] } }));
check('but a dropped text selection is still left alone',
      h.fire('drop', { dataTransfer: { types: ['text/plain'], files: [] } }), false);

console.log();
console.log(`${PASS} passed, ${FAIL} failed`);
process.exit(FAIL ? 1 : 0);
