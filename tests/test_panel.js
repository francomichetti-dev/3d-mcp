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
// Which of them the markup hides, so the stub starts where the browser does.
const hiddenIds = [...HTML.matchAll(/<[^>]*\bid="([^"]+)"[^>]*\shidden\s*>/g)]
  .map((m) => m[1]);

function start() {
  const h = makeHarness(ids, hiddenIds);
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

// ------------------------------------------------- work held elsewhere -----
// Switching designs used to kill a running turn. It now pauses instead, which
// only works if the panel says so — an unexplained disabled Send box reads as
// a broken panel, not as a deliberate hold.
console.log('Work held on another design');

h = start();
check('nothing held, nothing shown',
      h.doc.getElementById('elsewhere').hidden, true);

h.deliver({ type: 'document', key: 'design-2', name: 'bracket', design: true,
            transcript: [], plan: [], busy: false,
            busy_elsewhere: { key: 'design-1', name: 'tower' } });
check('the hold is shown', h.doc.getElementById('elsewhere').hidden, false);
truthy('naming the design it is held on',
       h.doc.getElementById('elsewhere-text').textContent.includes('tower'));
truthy('and saying it resumes rather than that it died',
       h.doc.getElementById('elsewhere-text').textContent.includes('carries on'));

// The point of showing it: Send is disabled, and that needs explaining.
check('Send is disabled while work is held elsewhere',
      h.doc.getElementById('send').disabled, true);
check('so is attaching', h.doc.getElementById('attach').disabled, true);
check('and so are the pickers, which would rebuild that session',
      h.doc.getElementById('model').disabled, true);

// Cancelling asks the service; the panel does not hide it on its own say-so.
h.doc.getElementById('elsewhere-cancel')._listeners;
check('cancel is offered', h.doc.getElementById('elsewhere-cancel').disabled, false);

// Once the service confirms, the panel frees up.
h.deliver({ type: 'document', key: 'design-2', name: 'bracket', design: true,
            transcript: [], plan: [], busy: false, busy_elsewhere: null });
check('the hold clears', h.doc.getElementById('elsewhere').hidden, true);
check('and Send comes back', h.doc.getElementById('send').disabled, false);

// Switching BACK to the held design: it is the active one now, so there is no
// "elsewhere" any more — it is just busy here.
h.deliver({ type: 'document', key: 'design-1', name: 'tower', design: true,
            transcript: [], plan: [], busy: true, busy_elsewhere: null });
check('back on the working design, no elsewhere notice',
      h.doc.getElementById('elsewhere').hidden, true);
check('but Send is still disabled because it is busy here',
      h.doc.getElementById('send').disabled, true);

// ---- the stop button ------------------------------------------------------
//
// It lives inside the working banner, beside the wheel: the spinner says
// "running" and the button is the answer to it. It only exists while there is
// something to stop, and a cancelled turn ends orange "Stopped", never the
// green "Done" of a build that actually finished.
console.log('The stop button');
{
  const h = start();
  check('idle: no banner, so no stop either',
        h.doc.getElementById('banner').hidden, true);

  // A live tool event puts the turn on screen.
  h.deliver({ type: 'tool', doc: 'design-1', name: 'fusion_execute',
              summary: 'base plate', verb: 'Extruding' });
  check('working: the banner is up', h.doc.getElementById('banner').hidden, false);
  check('and stop is clickable',
        h.doc.getElementById('banner-stop').disabled, false);

  // The click must change the screen immediately, before the server answers.
  h.sent.length = 0;
  h.doc.getElementById('banner-stop').onclick();
  check('the click posts the interrupt', h.sent[0] && h.sent[0].url, '/interrupt');
  check('and says so at once',
        h.doc.getElementById('banner-text').textContent, 'Stopping…');
  check('and cannot be double-clicked',
        h.doc.getElementById('banner-stop').disabled, true);

  // The turn ends because it was cancelled: orange Stopped, not green Done.
  h.deliver({ type: 'turn_end', doc: 'design-1' });
  const cls = h.doc.getElementById('banner').className;
  check('a cancelled turn reads Stopped',
        h.doc.getElementById('banner-text').textContent, 'Stopped');
  check('styled as stopped', /stopped/.test(cls) && !/done/.test(cls), true);
  check('and Send comes back', h.doc.getElementById('send').disabled, false);

  // The next turn starts clean — the old Stop must not colour its ending.
  h.deliver({ type: 'tool', doc: 'design-1', name: 'fusion_execute',
              summary: 'fillet', verb: 'Filleting' });
  check('a new turn re-arms the button',
        h.doc.getElementById('banner-stop').disabled, false);
  h.deliver({ type: 'turn_end', doc: 'design-1' });
  check('and an uncancelled turn is still green Done',
        h.doc.getElementById('banner-text').textContent, 'Done');
}

// ---- the hidden attribute must actually hide ------------------------------
//
// Everything above drives the DOM, where `hidden` is a boolean this harness
// respects by construction. The live palette is a browser, where an id rule
// setting `display` OUTRANKS the UA's [hidden] { display: none } — so
// elsewhere.hidden = true hid nothing and its wheel spun forever, on screen,
// under 826 green assertions. A stub DOM cannot compute CSS specificity, so
// the guard rule's presence in the stylesheet is what gets pinned instead.
console.log('The hidden attribute wins over id display rules');
truthy('the stylesheet carries the [hidden] override',
       /\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}/.test(HTML));

// Same class of check, same reason: the engine draws a <select>'s open list
// (and the scrollbars) itself, and without color-scheme it draws them for a
// light page — a white popup over the dark panel, live on 2026-08-09. A stub
// DOM cannot open a native popup, so the declaration is what gets pinned.
truthy('the page declares itself dark to the engine',
       /color-scheme:\s*dark/.test(HTML));
truthy('and pins the dropdown rows to the panel colours',
       /option[^{]*\{[^}]*background:\s*var\(--panel\)/.test(HTML));
{
  // And the guard must precede every id rule that sets display, or the tie
  // would fall to source order for equal-importance rules. (!important makes
  // order moot, but cheap to pin while we are here: guard first.)
  const guardAt = HTML.search(/\[hidden\]\s*\{/);
  const firstDisplayId = HTML.search(/#\w[^{]*\{[^}]*display:\s*flex/);
  truthy('and it appears before the first id display rule',
         guardAt !== -1 && firstDisplayId !== -1 && guardAt < firstDisplayId);
}

// --------------------------------------------------------- files ------------
// The Save and Download buttons exist so that getting the file out never
// depends on the model remembering to do it. What matters in here: a press
// produces exactly one request, the reply is reported with the folder in it
// (because "where did it go" is the question), a failure says so instead of
// reading as success, and a press cannot be doubled while the first is in
// flight — the bridge runs one job at a time and refuses the second.
console.log('Save and Download');

function harnessWithReplies(replies) {
  // Each entry is matched by url; anything else gets a bare ok.
  const h = makeHarness(ids, hiddenIds);
  h.sandbox.fetch = (url, opts) => {
    h.sent.push({ url, opts });
    const body = replies[url];
    const answer = typeof body === 'function' ? body() : body;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(answer || { ok: true }) });
  };
  vm.createContext(h.sandbox);
  vm.runInContext(script, h.sandbox);
  h.deliver({ type: 'document', key: 'd1', name: 'tower', design: true,
              transcript: [], plan: [], busy: false });
  return h;
}

const flush = () => new Promise((r) => setImmediate(r));

truthy('the header declares a Save button', /id="save"/.test(HTML));
truthy('and a Download button', /id="download"/.test(HTML));
truthy('and a config button', /id="cfg-open"/.test(HTML));
// A file the model did not ask for must still be honest about the format, or
// "Download" is a button whose result you have to guess.
truthy('the download button names its format', /Download ' \+ \(config\.format/.test(HTML));
// The format menu is a <select>, and an unpinned dropdown is painted by the
// engine in its own colours — which is how a pale popup over this dark panel
// happened once already.
truthy('the format menu pins its dropdown rows to the panel colours',
       /#cfg select option\s*\{[^}]*background:\s*var\(--panel\)/.test(HTML));
truthy('and its inputs are not transparent over the log',
       /#cfg input, #cfg select\s*\{[^}]*background:\s*var\(--panel\)/.test(HTML));

(async () => {
  {
    const h = harnessWithReplies({
      '/fusion/config': { ok: true, save_dir: '/Users/x/Downloads', format: 'step',
                          formats: ['stl', 'step', 'f3d'], save_format: 'f3d' },
      '/fusion/save': { ok: true, name: 'tower.f3d', bytes: 2956382,
                        folder: '/Users/x/Downloads' },
    });
    await flush();
    check('the panel asks what the settings are', h.sent[0].url, '/fusion/config');
    check('and puts the configured format on the button',
          h.doc.getElementById('download').textContent, 'Download STEP');

    h.sent.length = 0;
    await h.doc.getElementById('save').onclick();
    check('one press, one request', h.sent.map((s) => s.url), ['/fusion/save']);
    check('and it is a POST', h.sent[0].opts.method, 'POST');
    const lines = h.doc.getElementById('log').children;
    const last = lines[lines.length - 1].textContent;
    truthy('the result names the file', last.includes('tower.f3d'));
    truthy('its size in something human', last.includes('2.8 MB'));
    truthy('and the folder it went to', last.includes('/Users/x/Downloads'));
    check('the button is usable again', h.doc.getElementById('save').disabled, false);
  }

  // A save that fails must not read as one that worked.
  {
    const h = harnessWithReplies({
      '/fusion/save': { ok: false, error: 'Fusion is not reachable' },
    });
    await flush();
    await h.doc.getElementById('save').onclick();
    const lines = h.doc.getElementById('log').children;
    const line = lines[lines.length - 1];
    truthy('a failure is marked as one', line.classList.contains('err'));
    truthy('and carries the reason', line.textContent.includes('not reachable'));
  }

  // Both buttons are held while either is working: the bridge does one job at a
  // time, so a second press would be refused rather than queued.
  {
    let release;
    const h = harnessWithReplies({
      '/fusion/download': () => ({ ok: true, name: 'a.stl', bytes: 10, folder: '/f' }),
    });
    h.sandbox.fetch = (url, opts) => {
      h.sent.push({ url, opts });
      return new Promise((res) => { release = () => res({
        ok: true, json: () => Promise.resolve({ ok: true, name: 'a.stl', bytes: 10, folder: '/f' }) }); });
    };
    h.sent.length = 0;                       // startup chatter is not the subject
    const done = h.doc.getElementById('download').onclick();
    await flush();
    check('the other button is held too', h.doc.getElementById('save').disabled, true);
    h.doc.getElementById('save').onclick();          // a press that must do nothing
    await flush();
    check('so a second press sends nothing', h.sent.map((s) => s.url), ['/fusion/download']);
    release(); await done;
    check('and both come back', [h.doc.getElementById('save').disabled,
                                h.doc.getElementById('download').disabled], [false, false]);
  }

  // The config button: fills itself from the service, applies, and stays open on
  // a rejection so a mistyped folder can be corrected rather than lost.
  {
    const h = harnessWithReplies({
      '/fusion/config': { ok: true, save_dir: '/Users/x/Downloads', format: 'stl',
                          formats: ['stl', 'step', '3mf', 'usd', 'f3d'], save_format: 'f3d' },
    });
    await flush();
    await h.doc.getElementById('cfg-open').onclick();
    check('the panel opens', h.doc.getElementById('cfg').hidden, false);
    check('the folder box shows the real path, not a blank meaning "default"',
          h.doc.getElementById('cfg-dir').value, '/Users/x/Downloads');
    check('every format the exporter has is offered',
          h.doc.getElementById('cfg-fmt').children.length, 5);

    h.sandbox.fetch = (url, opts) => {
      h.sent.push({ url, opts });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        { ok: false, error: '/nope is not writable' }) });
    };
    h.sent.length = 0;
    h.doc.getElementById('cfg-dir').value = '/nope';
    await h.doc.getElementById('cfg-save').onclick();
    check('applying sends the folder and the format',
          JSON.parse(h.sent[0].opts.body).save_dir, '/nope');
    check('a rejected folder keeps the panel open',
          h.doc.getElementById('cfg').hidden, false);
    truthy('and says why',
           h.doc.getElementById('cfg-note').textContent.includes('not writable'));
  }

  // ------------------------------------------------------- thinking orb -----
  // The orb is decoration, and the rule it has to obey is that the panel works
  // without it: its engine is a separate module, so an import that fails, or a
  // browser that will not run it, must leave the wheel that was there before.
  console.log('Thinking orb');

  truthy('the banner declares the fallback wheel', /id="wheel"/.test(HTML));
  truthy('and a canvas for the orb', /id="orb"/.test(HTML));
  truthy('the canvas starts hidden, so a failed import shows the wheel',
         /<canvas[^>]*id="orb"[^>]*\shidden\s*>/.test(HTML));
  // Carrying .spin is what makes every existing "hide the indicator" rule —
  // done, stopped, lost — apply to the orb without being restated.
  truthy('the orb carries the indicator class', /class="spin orb"/.test(HTML));
  truthy('and .spin.orb undoes the wheel it inherits',
         /\.spin\.orb\s*\{[^}]*animation:\s*none/.test(HTML));
  // The panel's own script must stay a plain script: the harness runs it, and
  // an import statement in it would make that impossible.
  check('the panel script itself imports nothing', /\bimport\s*\(/.test(script), false);
  truthy('the orb lives in its own module',
         /<script type="module">/.test(HTML));

  {
    // With an orb present, the state on the event is what gets drawn.
    const h = harnessWithReplies({});
    const states = [];
    h.sandbox.window.__orb = { set: (s) => states.push(s) };
    h.deliver({ type: 'tool', name: 'fusion_execute', summary: '', live: true,
                verb: 'Sketching', orb: 'shaping' });
    check('the orb follows the event', states, ['shaping']);
    h.deliver({ type: 'tool', name: 'fusion_save', summary: '',
                verb: 'Saving', orb: 'composing' });
    check('and changes with the activity', states, ['shaping', 'composing']);
  }

  {
    // An event with no orb state — an older service, a replayed transcript —
    // must still start the indicator rather than throwing.
    const h = harnessWithReplies({});
    const states = [];
    h.sandbox.window.__orb = { set: (s) => states.push(s) };
    h.deliver({ type: 'tool', name: 'fusion_state', summary: '', verb: 'Checking' });
    check('a missing orb state falls back to thinking', states, ['breathing']);
  }

  // The engine is vendored, so its API is a thing that can change under us. A
  // frame that generates but does not paint, or an export that got renamed,
  // would show up in Fusion as a banner with a blank square in it — and nowhere
  // else. So the draw path runs here, against a recording 2D context.
  {
    const engine = await import('../agent/static/thinking-orb-engine.js');
    check('the engine exports what the panel calls',
          ['MODE_FRAMES', 'paintFrame', 'resolvePreset']
            .filter((k) => typeof engine[k] === 'undefined'), []);

    const calls = [];
    const ctx = {
      setTransform() { calls.push('setTransform'); },
      clearRect() { calls.push('clearRect'); },
      beginPath() { calls.push('beginPath'); },
      arc() { calls.push('arc'); },
      fill() { calls.push('fill'); },
      moveTo() {}, lineTo() {}, stroke() {}, closePath() {},
      save() {}, restore() {},
      set fillStyle(_v) {}, set strokeStyle(_v) {}, set lineWidth(_v) {},
      set globalAlpha(_v) {},
    };
    // Every state the service can ask for, at the size the banner uses.
    const states = ['working', 'searching', 'solving', 'weaving', 'composing',
                    'breathing', 'shaping'];
    const painted = [];
    for (const state of states) {
      calls.length = 0;
      const preset = engine.resolvePreset(state, 20);
      const frame = engine.MODE_FRAMES[preset.mode](20, 1.0, preset.opts);
      engine.paintFrame(ctx, frame, true, { r: 124, g: 199, b: 255 });
      painted.push(calls.filter((c) => c === 'arc').length > 0);
      truthy(`${state} produces marks to draw`, frame.dots.length > 0);
      truthy(`${state} has a speed to run at`, preset.speed > 0);
    }
    check('every state the panel uses actually paints dots',
          painted.filter((ok) => !ok).length, 0);
  }

  console.log();
  console.log(`${PASS} passed, ${FAIL} failed`);
  process.exit(FAIL ? 1 : 0);
})();

const NEVER = false;
if (NEVER) {
}
