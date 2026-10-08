"""Fast commands: proven code saved under a name and run in one call.

Nothing here needs Fusion. Three things are tested:

- The library refuses anything that would fail later, inside Fusion, where it
  is expensive to diagnose: a name that is not a plain identifier, code with
  no run(p), a built-in quietly replaced.
- The invocation passes arguments as data. A command's arguments come from a
  model, and an argument must never be able to become code.
- The built-ins do what their docstrings say, run against small stand-ins
  for the Fusion objects they touch. Live behaviour (which coordinate space a
  profile reports in, for example) was probed against Fusion 2704 and is
  recorded where it matters, in the built-ins themselves.

    cd server && uv run --frozen --no-sync python ../tests/test_fastcommands.py
"""

import json
import os
import stat
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "server/src"))

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def truthy(label, got):
    check(label, bool(got), True)


def raises(label, fn, kind=Exception, says=""):
    global PASS, FAIL
    try:
        fn()
    except kind as exc:
        if says and says not in str(exc):
            FAIL += 1
            print(f"  FAIL {label}: raised, but without {says!r}: {exc}")
            return
        PASS += 1
        return
    FAIL += 1
    print(f"  FAIL {label}: did not raise {kind.__name__}")


from arges_mcp import common  # noqa: E402

SANDBOX = Path(tempfile.mkdtemp(prefix="arges-fastcommands-test-"))
common.state_dir = lambda: SANDBOX / "state"
os.environ["ARGES_SAVE_DIR"] = str(SANDBOX / "saves")

from arges_mcp import fastcommands as fc  # noqa: E402

GOOD = '''"""Count the bodies in the root component.

args: none
"""
READONLY = True


def run(p):
    return {"bodies": design.rootComponent.bRepBodies.count}
'''


# ------------------------------------------------------------- parsing ----
print("Reading a command")

cmd = fc.parse(GOOD, "count_bodies", origin="learned")
check("the description is the docstring's first line", cmd.description,
      "Count the bodies in the root component.")
check("the usage is the rest of it", cmd.usage, "args: none")
check("READONLY is read without running anything", cmd.readonly, True)
check("a command with no READONLY is assumed to change the design",
      fc.parse(GOOD.replace("READONLY = True\n", ""), "x_y").readonly, False)
raises("no docstring is refused: a command nobody can identify is no use",
       lambda: fc.parse("def run(p):\n    return 1\n", "x_y"), ValueError, "description")
raises("no run(p) is refused", lambda: fc.parse('"""Doc."""\nx = 1\n', "x_y"),
       ValueError, "run(p)")
raises("run with the wrong arity is refused",
       lambda: fc.parse('"""Doc."""\ndef run():\n    return 1\n', "x_y"), ValueError, "run(p)")
raises("code that does not parse is refused, naming the line",
       lambda: fc.parse('"""Doc."""\ndef run(p):\n    return (\n', "x_y"),
       ValueError, "line")
raises("READONLY must be a literal boolean",
       lambda: fc.parse('"""Doc."""\nREADONLY = "yes"\ndef run(p):\n    return 1\n', "x_y"),
       ValueError, "READONLY")


# ------------------------------------------------------------ built-ins ----
print("The built-ins")

library = fc.default_library()
builtins = {c.name: c for c in library.builtins()}
check("the built-in set", sorted(builtins),
      ["bbox", "inspect", "profiles", "remove", "selection", "set_params", "sketch_frame", "timeline"])
for name, command in sorted(builtins.items()):
    check(f"{name} is a built-in", command.origin, "builtin")
    truthy(f"{name} says how to call it", len(command.usage) > 20)
check("only remove and set_params change the design",
      sorted(n for n, c in builtins.items() if not c.readonly), ["remove", "set_params"])


# -------------------------------------------------------------- saving ----
print("Saving a learned command")

LEARNED = "def run(p):\n    return {'twice': p.get('n', 0) * 2}\n"
saved, replaced = library.save("double_it", "Double a number.", LEARNED,
                               usage="args: {\"n\": number}", readonly=True)
check("a new command is not a replacement", replaced, False)
check("it lands in the state directory's commands folder",
      saved.path, SANDBOX / "state" / "commands" / "double_it.py")
check("the file is private", stat.S_IMODE(saved.path.stat().st_mode), 0o600)
check("so is the folder", stat.S_IMODE(saved.path.parent.stat().st_mode), 0o700)
check("it reads back as learned", library.get("double_it").origin, "learned")
check("with its description", library.get("double_it").description, "Double a number.")
check("and its flag", library.get("double_it").readonly, True)
check("listed after the built-ins",
      [c.name for c in library.all()][-1], "double_it")

again, replaced = library.save("double_it", "Double a number, better.", LEARNED)
check("saving again replaces", replaced, True)
truthy("keeping the previous version beside it",
       (saved.path.parent / "double_it.py.prev").exists())

for bad in ["", "x", "Bad Name", "../escape", "has-dash", "1starts_digit", "a" * 50]:
    raises(f"the name {bad!r} is refused",
           lambda b=bad: library.save(b, "Doc.", LEARNED), ValueError)
raises("a built-in cannot be replaced", lambda: library.save("bbox", "Mine.", LEARNED),
       ValueError, "built-in")
raises("code without run(p) is refused",
       lambda: library.save("no_run", "Doc.", "x = 1\n"), ValueError, "run(p)")
raises("an empty description is refused",
       lambda: library.save("no_doc", "  ", LEARNED), ValueError, "description")
raises("a READONLY in the code is refused: it is a parameter, set once",
       lambda: library.save("sneaky", "Doc.", "READONLY = True\n" + LEARNED),
       ValueError, "READONLY")
raises("enormous code is refused",
       lambda: library.save("huge", "Doc.", LEARNED + "#" * fc.MAX_SOURCE_BYTES),
       ValueError, "large")

# A file somebody broke by hand must not take the library down with it.
(SANDBOX / "state" / "commands" / "broken.py").write_text("def run(p:\n")
(SANDBOX / "state" / "commands" / "Not A Name.py").write_text(GOOD)
names = [c.name for c in library.all()]
check("a broken learned file is skipped, not fatal", "broken" in names, False)
check("so is a file whose name is not a command name", "Not A Name" in names, False)
check("and everything else is still there", "double_it" in names, True)

check("removing a learned command works", library.remove("double_it"), True)
check("and it is gone", library.get("double_it"), None)
check("removing an unknown one says so", library.remove("double_it"), False)
raises("removing a built-in is refused", lambda: library.remove("bbox"), ValueError)

blocked = fc.Library(user_dir=SANDBOX / "not-a-dir")
(SANDBOX / "not-a-dir").write_text("a file where the folder should be")
check("an unreadable commands folder leaves the built-ins",
      sorted(c.name for c in blocked.all()), sorted(builtins))


# ----------------------------------------------------------- invocation ----
# The generated code is run here exactly as Fusion would run it: exec'd in a
# namespace holding the injected names. The stand-in design is just enough for
# the command under test.
print("Running a command")


def fake_design(bodies=()):
    return NS(rootComponent=NS(bRepBodies=NS(count=len(bodies))))


def execute(code, design=None):
    namespace = {"adsk": NS(), "app": NS(), "ui": NS(), "design": design or fake_design()}
    exec(compile(code, "<fusion_execute>", "exec"), namespace, namespace)
    return namespace


ns = execute(fc.invocation(cmd, {}), fake_design([1, 2, 3]))
check("the command's result comes back as `result`", ns["result"], {"bodies": 3})
check("its run() does not leak into the persistent namespace", "run" in ns, False)
check("nor does its READONLY", "READONLY" in ns, False)

echo = fc.parse('"""Echo."""\ndef run(p):\n    return p\n', "echo")
nasty = {"s": '"); import os; os.system("x") #', "n": "\n'''\n", "nested": {"a": [1, None]}}
check("arguments arrive as data, however they are quoted",
      execute(fc.invocation(echo, nasty))["result"], nasty)

boom = fc.parse('"""Boom."""\ndef run(p):\n    raise RuntimeError("3 : nope")\n', "boom")
try:
    execute(fc.invocation(boom, {}))
    tb = ""
except RuntimeError:
    tb = traceback.format_exc()
truthy("a failure's traceback names the command it came from", "<command:boom>" in tb)

for name, command in sorted(builtins.items()):
    # Defining a built-in must not touch Fusion: only run() may.
    try:
        exec(compile(command.source, f"<command:{name}>", "exec"),
             {"adsk": None, "app": None, "ui": None, "design": None})
        ok = True
    except Exception:                                      # noqa: BLE001
        ok = False
    check(f"{name} loads without touching Fusion", ok, True)


# ------------------------------------------------------ built-in logic ----
print("Built-ins against stand-ins")


def point(x, y, z):
    return NS(x=x, y=y, z=z)


def body(name, lo, hi, **more):
    return NS(name=name, boundingBox=NS(minPoint=point(*lo), maxPoint=point(*hi)), **more)


class Collection(list):
    @property
    def count(self):
        return len(self)

    def item(self, i):
        return self[i]

    def itemByName(self, name):
        return next((x for x in self if x.name == name), None)


plate = body("Plate", (0, 0, 0), (4, 3, 0.2))
pin = body("Pin", (1, 1, 0.2), (1.3, 1.3, 1.2))
housing = NS(name="Housing:1", bRepBodies=Collection([body("Shell", (-1, -1, 0), (1, 1, 2))]))
design = NS(rootComponent=NS(bRepBodies=Collection([plate, pin]),
                             allOccurrences=Collection([housing])))


def run_builtin(name, args, design):
    return execute(fc.invocation(builtins[name], args), design)["result"]


boxes = run_builtin("bbox", {}, design)
check("bbox reports millimetres, not centimetres",
      boxes["bodies"]["Plate"]["size"], [40.0, 30.0, 2.0])
check("including bodies inside components, keyed by occurrence",
      boxes["bodies"]["Housing:1/Shell"]["min"], [-10.0, -10.0, 0.0])
check("and the overall extent", boxes["overall"]["size"], [50.0, 40.0, 20.0])
flat = run_builtin("bbox", {}, NS(rootComponent=NS(
    bRepBodies=Collection([body("Flat", (-0.0001, 0, -0.00001), (1, 1, 0))]),
    allOccurrences=Collection())))
check("a coordinate that rounds to zero reads 0.0, never -0.0",
      [str(v) for v in flat["bodies"]["Flat"]["min"]], ["-0.001", "0.0", "0.0"])
picked = run_builtin("bbox", {"bodies": ["Pin", "Shell", "Ghost"]}, design)
check("a named pick returns just those", sorted(picked["bodies"]), ["Housing:1/Shell", "Pin"])
check("and says which names it could not find", picked["missing"], ["Ghost"])


class RemoveFeatures(Collection):
    def add(self, entity):
        prefix = "RemoveInstance" if entity.objectType.endswith("::Occurrence") else "RemoveBody"
        feature = NS(name=f"{prefix}-{self.count + 1}", entity=entity)
        self.append(feature)
        return feature


def selection_design():
    root = NS(name="Root", features=NS(removeFeatures=RemoveFeatures()))
    comp = NS(name="Housing", features=NS(removeFeatures=RemoveFeatures()))
    plate = body("Plate", (0, 0, 0), (4, 3, 0.2), objectType="adsk::fusion::BRepBody",
                 assemblyContext=None, nativeObject=None, parentComponent=root)
    shell = body("Shell", (-1, -1, 0), (1, 1, 2), objectType="adsk::fusion::BRepBody",
                 assemblyContext=None, nativeObject=None, parentComponent=comp)
    root.bRepBodies = Collection([plate])
    comp.bRepBodies = Collection([shell])
    occ = NS(name="Housing:1", objectType="adsk::fusion::Occurrence", component=comp,
             assemblyContext=None)
    proxy = body("Shell", (9, -1, 0), (11, 1, 2), objectType="adsk::fusion::BRepBody",
                 assemblyContext=occ, nativeObject=shell, parentComponent=comp)
    occ.bRepBodies = Collection([proxy])
    root.occurrences = Collection([occ])
    root.allOccurrences = Collection([occ])
    return NS(rootComponent=root), plate, shell, proxy, occ


def run_selected(name, args, design, entities=()):
    fake_adsk = NS(fusion=NS(
        BRepBody=NS(classType=lambda: "adsk::fusion::BRepBody"),
        Occurrence=NS(classType=lambda: "adsk::fusion::Occurrence")))
    ns = {"adsk": fake_adsk, "app": NS(), "design": design,
          "ui": NS(activeSelections=Collection([NS(entity=e) for e in entities]))}
    exec(compile(fc.invocation(builtins[name], args), "<fusion_execute>", "exec"), ns, ns)
    return ns["result"]


selected_design, root_body, native_body, proxy_body, occurrence = selection_design()
face = NS(objectType="adsk::fusion::BRepFace", body=proxy_body)
edge = NS(objectType="adsk::fusion::BRepEdge", body=proxy_body)
vertex = NS(objectType="adsk::fusion::BRepVertex", body=proxy_body)
sketch = NS(objectType="adsk::fusion::Sketch", name="Sketch1")
selected = run_selected("selection", {}, selected_design,
                        [face, edge, vertex, proxy_body, root_body, occurrence, sketch])
check("selection reports the actual selected types",
      [x["type"] for x in selected["items"]],
      ["BRepFace", "BRepEdge", "BRepVertex", "BRepBody", "BRepBody", "Occurrence", "Sketch"])
check("a face reports the body and occurrence it belongs to", selected["items"][0],
      {"type": "BRepFace", "body": "Housing:1/Shell", "occurrence": "Housing:1",
       "size_mm": [20.0, 20.0, 20.0]})
check("a selected body also reports its own name", selected["items"][3]["name"], "Shell")
check("a root body has no occurrence prefix", selected["items"][4],
      {"type": "BRepBody", "name": "Plate", "body": "Plate", "occurrence": None,
       "size_mm": [40.0, 30.0, 2.0]})
check("faces, edges and vertices collapse to one body",
      selected["bodies"], ["Housing:1/Shell", "Plate"])
check("only selected occurrence entities go in occurrences",
      selected["occurrences"], ["Housing:1"])
check("other named entities are still reported", selected["items"][-1],
      {"type": "Sketch", "name": "Sketch1"})
check("selection counts every selected entity", (selected["count"], selected["truncated"]), (7, 0))
check("an empty selection gives the model nothing to guess from",
      run_selected("selection", {}, selected_design),
      {"items": [], "bodies": [], "occurrences": [], "count": 0, "truncated": 0})
capped = run_selected("selection", {}, selected_design, [face] * 103)
check("selection caps its items while keeping the total count",
      (len(capped["items"]), capped["count"], capped["truncated"]), (100, 103, 3))
check("repeated occurrence selections are de-duplicated",
      run_selected("selection", {}, selected_design, [occurrence, occurrence])["occurrences"],
      ["Housing:1"])

removed = run_selected("remove", {"selection": True}, selected_design,
                       [face, edge, vertex, proxy_body, root_body, occurrence, occurrence, sketch])
check("remove by selection creates a timeline feature for each target", removed,
      {"removed": [{"what": "Housing:1/Shell", "feature": "RemoveBody-1"},
                   {"what": "Plate", "feature": "RemoveBody-1"},
                   {"what": "Housing:1", "feature": "RemoveInstance-2"}],
       "missing": [], "skipped": ["Sketch"]})
truthy("a selected proxy removes its native body in the body's component",
       occurrence.component.features.removeFeatures[0].entity is native_body)
check("faces, edges, vertices and their body make only one RemoveBody",
      occurrence.component.features.removeFeatures.count, 1)
truthy("a root body is removed through root features",
       selected_design.rootComponent.features.removeFeatures[0].entity is root_body)
truthy("a selected occurrence is removed through root features",
       selected_design.rootComponent.features.removeFeatures[1].entity is occurrence)
check("removing an empty selection is a no-op",
      run_selected("remove", {"selection": True}, selected_design),
      {"removed": [], "missing": [], "skipped": []})
check("remove without targets does not remove everything",
      run_selected("remove", {}, selected_design, [root_body]),
      {"removed": [], "missing": [], "skipped": []})

named_design, named_root, named_native, named_proxy, named_occ = selection_design()
removed = run_selected("remove", {"bodies": ["Housing:1/Shell", "Plate", "Plate", "Ghost", "Shell"],
                                  "occurrences": ["Housing:1", "Housing:1", "motor:1"]},
                       named_design)
check("remove by exact names reports missing bodies and occurrences without raising", removed,
      {"removed": [{"what": "Housing:1/Shell", "feature": "RemoveBody-1"},
                   {"what": "Plate", "feature": "RemoveBody-1"},
                   {"what": "Housing:1", "feature": "RemoveInstance-2"}],
       "missing": ["Ghost", "Shell", "motor:1"], "skipped": []})
truthy("a named occurrence body is resolved to its native body",
       named_occ.component.features.removeFeatures[0].entity is named_native)

mixed_design, mixed_root, mixed_native, mixed_proxy, mixed_occ = selection_design()
another_occ = NS(name="Housing:2", objectType="adsk::fusion::Occurrence",
                 component=mixed_occ.component, assemblyContext=None)
another_proxy = body("Shell", (19, -1, 0), (21, 1, 2), objectType="adsk::fusion::BRepBody",
                     assemblyContext=another_occ, nativeObject=mixed_native,
                     parentComponent=mixed_occ.component)
another_occ.bRepBodies = Collection([another_proxy])
mixed_design.rootComponent.occurrences.append(another_occ)
mixed_design.rootComponent.allOccurrences.append(another_occ)
removed = run_selected("remove", {"selection": True,
                                  "bodies": ["Housing:1/Shell", "Housing:2/Shell", "Plate"],
                                  "occurrences": ["Housing:1"]}, mixed_design,
                       [mixed_proxy, another_proxy, mixed_root, mixed_occ])
check("selection and names, including two proxies of one native body, are de-duplicated",
      [x["what"] for x in removed["removed"]], ["Housing:1/Shell", "Plate", "Housing:1"])
check("the shared native body gets one feature", mixed_occ.component.features.removeFeatures.count, 1)

fallback_design, _, fallback_native, fallback_proxy, fallback_occ = selection_design()
fallback_proxy.nativeObject = None
run_selected("remove", {"selection": True}, fallback_design, [fallback_proxy])
truthy("a proxy without nativeObject resolves by name in its own component",
       fallback_occ.component.features.removeFeatures[0].entity is fallback_native)

nested_design, _, _, _, parent_occ = selection_design()
nested_occ = NS(name="Nested:1", objectType="adsk::fusion::Occurrence",
                assemblyContext=parent_occ, bRepBodies=Collection())
nested_design.rootComponent.allOccurrences.append(nested_occ)
check("only top-level occurrences can be removed", run_selected(
    "remove", {"selection": True, "occurrences": ["Nested:1"]}, nested_design, [nested_occ]),
    {"removed": [], "missing": ["Nested:1"], "skipped": ["Occurrence"]})


class Params(Collection):
    def add(self, name, value_input, unit, comment):
        param = NS(name=name, expression=value_input.text, unit=unit, comment=comment)
        self.append(param)
        return param


params = Params([NS(name="wall", expression="2 mm", unit="mm", comment="")])
fake_adsk = NS(core=NS(ValueInput=NS(createByString=lambda text: NS(text=text))))
ns = {"adsk": fake_adsk, "app": NS(), "ui": NS(), "design": NS(userParameters=params)}
exec(compile(fc.invocation(builtins["set_params"],
                           {"set": {"wall": "2.4 mm",
                                    "holes": {"expression": "4", "unit": ""},
                                    "boss": "wall * 3"}}),
             "<fusion_execute>", "exec"), ns, ns)
out = ns["result"]
check("an existing parameter is updated in place", params.itemByName("wall").expression, "2.4 mm")
check("a new one is created with the default unit", params.itemByName("boss").unit, "mm")
check("or the unit it was given, including none", params.itemByName("holes").unit, "")
check("and the result says which were created",
      {p["name"]: p["created"] for p in out["parameters"]},
      {"wall": False, "holes": True, "boss": True})
check("with no errors", out["errors"], {})


# ------------------------------------------------------------- wiring ----
print("Wired into the tools")

from fastmcp.exceptions import ToolError  # noqa: E402

from arges_mcp import experience as xp  # noqa: E402
from arges_mcp import server as srv  # noqa: E402

real_bridge = srv._bridge_request
real_capture = srv._capture
sent = []


def bridge_answering(answer):
    def request(method, path, payload=None):
        if path == "/health":
            return {"ok": True, "document": "tower", "bridge_version": "1", "app_version": "2704"}
        code = (payload or {}).get("code", "")
        sent.append("save" if "_fx_export" in code else code)
        return dict(answer)
    return request


try:
    listing = srv.fusion_run()
    names = [c["name"] for c in listing["commands"]]
    truthy("with no command, fusion_run lists them", {"bbox", "set_params"} <= set(names))
    entry = [c for c in listing["commands"] if c["name"] == "bbox"][0]
    check("each with what it needs to be called", sorted(entry),
          ["description", "name", "origin", "readonly", "usage"])

    sent.clear()
    srv._bridge_request = bridge_answering({"ok": True, "result": {"bodies": {}}, "stdout": ""})
    raises("an unknown command is refused", lambda: srv.fusion_run("nope"), ToolError, "bbox")
    raises("arguments that are not an object are refused",
           lambda: srv.fusion_run("bbox", args=[1, 2]), ToolError)
    raises("an unknown view is refused", lambda: srv.fusion_run("bbox", screenshot="back"),
           ToolError, "NOT run")
    check("and in none of those cases did anything reach Fusion", sent, [])

    sent.clear()
    raises("a command changed since the panel's gate read it is not run",
           lambda: srv.fusion_run("bbox", source_sha="0" * 16), ToolError, "changed")
    check("and nothing reached Fusion", sent, [])
    srv.fusion_run("bbox", source_sha=fc.source_sha(builtins["bbox"].source))
    check("the checked source runs", len(sent), 1)

    sent.clear()
    out = srv.fusion_run("bbox", args={"bodies": ["Plate"]})
    check("a command runs and returns the bridge's result", out["result"], {"bodies": {}})
    check("as one call", len(sent), 1)
    truthy("carrying the built-in's source as data", "_fx_command" in sent[0])
    check("a read-only command is not followed by a save", "save" in sent, False)
    rec = xp.default_log().read()[-1]
    check("the log records the command by name",
          (rec["tool"], rec["command"], rec["ok"]), ("fusion_run", "bbox", True))
    check("with its arguments", rec["args"], {"bodies": ["Plate"]})
    check("and not its source, which is already on disk", rec["code"], "")

    sent.clear()
    srv.fusion_run("set_params", args={"set": {"wall": "2 mm"}})
    check("a command that changes the design is saved like any change",
          sent[-1], "save")

    srv._bridge_request = bridge_answering(
        {"ok": False, "traceback": 'File "<command:bbox>"\nRuntimeError: 3 : nope', "stdout": ""})
    out = srv.fusion_run("bbox")
    truthy("a failing command is a normal result with a learn nudge, as with execute",
           "fusion_learn" in out.get("learn", ""))

    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    srv._capture = lambda view, w, h: b"\x89PNG" + b"0" * 64
    out = srv.fusion_run("set_params", args={"set": {}}, screenshot="iso")
    check("with a screenshot it returns the picture too", len(out), 2)
    srv._capture = real_capture

    saved = srv.fusion_command_save(
        name="hole_grid", description="Drill a grid of holes.",
        code="def run(p):\n    return p['n']\n", usage="args: {\"n\": int}")
    check("fusion_command_save saves", (saved["ok"], saved["replaced"]), (True, False))
    check("and the command can be run by name straight away",
          "hole_grid" in [c["name"] for c in srv.fusion_run()["commands"]], True)
    raises("an invalid command is refused with the reason",
           lambda: srv.fusion_command_save(name="bbox", description="x", code="def run(p): 1"),
           ToolError, "built-in")
    gone = srv.fusion_command_save(name="hole_grid", remove=True)
    check("remove=true deletes a learned command", gone["removed"], True)

    state = srv.fusion_state()
    truthy("fusion_state lists the commands, so a session knows they exist",
           "bbox" in [c["name"] for c in state["experience"]["commands"]])
finally:
    srv._bridge_request = real_bridge
    srv._capture = real_capture


import shutil  # noqa: E402
shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
