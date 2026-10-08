"""Importing a file: find it, bring it in, say what arrived. One call.

This exists because of a real session. Asked to import a motor from Downloads,
the model spent its turn on two tool lookups, three Bash searches through
Downloads and Fusion's cache, and two hand-written importManager attempts.
The autosave then overwrote the very file it was importing (fixed separately;
see the save mirror in test_mcp_server.py).

Nothing here needs Fusion. Finding files is host-side and fully tested. The
Fusion-side import runs here against stand-ins for importManager, including
the older API without importToTarget2, the mesh path through a base feature,
and the failures. Those stand-ins are what the code expects; they are
replaced by a probe against the real API as soon as one can run.

    cd server && uv run --frozen --no-sync python ../tests/test_import.py
"""

import os
import sys
import tempfile
import time
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

SANDBOX = Path(tempfile.mkdtemp(prefix="arges-import-test-"))
common.state_dir = lambda: SANDBOX / "state"
os.environ["ARGES_SAVE_DIR"] = str(SANDBOX / "saves")


# -------------------------------------------------------------- formats ----
print("What can be imported")

for name, want in [("motor.f3d", "f3d"), ("MOTOR.F3D", "f3d"), ("asm.f3z", "f3d"),
                   ("part.step", "step"), ("part.stp", "step"), ("part.iges", "iges"),
                   ("part.igs", "iges"), ("part.sat", "sat"), ("part.smt", "smt"),
                   ("scan.stl", "mesh"), ("scan.obj", "mesh"), ("scan.3mf", "mesh"),
                   ("photo.png", None), ("notes.txt", None), ("noext", None)]:
    check(f"{name} -> {want}", common.import_format(name), want)


# -------------------------------------------------------------- finding ----
print("Finding the file")

downloads = SANDBOX / "Downloads"
desktop = SANDBOX / "Desktop"
for folder in (downloads, desktop):
    folder.mkdir()
now = time.time()


def make(path, age, data=b"x" * 10):
    path.write_bytes(data)
    os.utime(path, (now - age, now - age))
    return path


motor = make(downloads / "motor-RECOVERED.f3d", 60, b"m" * 200)
make(downloads / "tank-wheels.f3d", 3600)
bracket = make(desktop / "bracket.STEP", 10)
make(downloads / "photo.png", 5)                       # not CAD
make(downloads / ".hidden.f3d", 1)                     # hidden
(downloads / "folder.f3d").mkdir()                     # a directory, not a file
make(downloads / "old-motor.f3d", 99999)
(downloads / "nested").mkdir()
make(downloads / "nested" / "deep.step", 1)            # below the top level
roots = [downloads, desktop]

listed = common.importable_files(roots)
names = [f["name"] for f in listed]
check("CAD files only, newest first, top level only",
      names, ["bracket.STEP", "motor-RECOVERED.f3d", "tank-wheels.f3d", "old-motor.f3d"])
check("each says its format", listed[0]["format"], "step")
check("its size", [f["bytes"] for f in listed if f["name"] == "motor-RECOVERED.f3d"], [200])
check("and where it is", listed[1]["path"], str(motor))
truthy("and how old it is, readably", listed[0]["modified"])
check("the list is capped", len(common.importable_files(roots, limit=2)), 2)
check("a folder that does not exist is skipped, not fatal",
      len(common.importable_files([SANDBOX / "nope"] + roots)), 4)

check("an absolute path is taken as given", common.resolve_import(str(motor), roots), motor)
check("a bare filename is found in the usual folders",
      common.resolve_import("motor-RECOVERED.f3d", roots), motor)
check("case does not matter", common.resolve_import("MOTOR-recovered.F3D", roots), motor)
check("nor does the extension", common.resolve_import("motor-RECOVERED", roots), motor)
(SANDBOX / "x.f3d").write_bytes(b"x")
check("~ is expanded", common.resolve_import("~/x.f3d", roots, home=SANDBOX), SANDBOX / "x.f3d")
raises("a missing file is refused, naming what is there",
       lambda: common.resolve_import("motr.f3d", roots), ValueError, "motor-RECOVERED.f3d")
raises("a file that cannot be imported is refused",
       lambda: common.resolve_import(str(downloads / "photo.png"), roots), ValueError, "import")
raises("a directory is refused",
       lambda: common.resolve_import(str(downloads / "folder.f3d"), roots), ValueError)
same_name = make(desktop / "motor-RECOVERED.f3d", 1)
check("with the same name in two folders, the newest wins",
      common.resolve_import("motor-RECOVERED.f3d", roots), same_name)
same_name.unlink()


# ------------------------------------------------------ inside Fusion ----
# The import snippet, run as Fusion would run it, against stand-ins.
print("Bringing it in")


class Collection(list):
    @property
    def count(self):
        return len(self)

    def item(self, i):
        return self[i]


def box(lo, hi):
    return NS(minPoint=NS(x=lo[0], y=lo[1], z=lo[2]), maxPoint=NS(x=hi[0], y=hi[1], z=hi[2]))


def occurrence(name, bodies=1):
    comp = NS(name=name.split(":")[0], meshBodies=Collection())
    return NS(name=name, component=comp, bRepBodies=Collection([NS()] * bodies),
              boundingBox=box((0, 0, 0), (3.7, 7.5, 3.7)))


class Root:
    def __init__(self):
        self.occurrences = Collection([occurrence("chassis:1")])


class ImportManager:
    def __init__(self, root, modern=True, accept=True):
        self.root, self.accept, self.calls = root, accept, []
        if not modern:
            self.importToTarget2 = None

    def createFusionArchiveImportOptions(self, path):
        self.calls.append(("archive", path))
        return NS(kind="archive", path=path)

    def createSTEPImportOptions(self, path):
        self.calls.append(("step", path))
        return NS(kind="step", path=path)

    def importToTarget2(self, options, target):
        new = occurrence("motor:1", bodies=2)
        target.occurrences.append(new)
        return Collection([new])

    def importToTarget(self, options, target):
        if not self.accept:
            return False
        target.occurrences.append(occurrence("motor:1", bodies=2))
        return True


def run_import(params, im=None, root=None, parametric=True):
    root = root or Root()
    im = im or ImportManager(root)
    design = NS(rootComponent=root, designType=1 if parametric else 0)
    fake_adsk = NS(fusion=NS(Design=NS(cast=lambda product: product),
                             DesignTypes=NS(ParametricDesignType=1, DirectDesignType=0),
                             MeshUnits=NS(MillimeterMeshUnit="mm-unit", CentimeterMeshUnit="cm-unit",
                                          MeterMeshUnit="m-unit", InchMeshUnit="in-unit",
                                          FootMeshUnit="ft-unit")),
                   core=NS(Matrix3D=NS(create=lambda: "identity")))
    app = NS(importManager=im, activeProduct=design)
    ns = {"adsk": fake_adsk, "app": app, "ui": NS(), "design": design}
    exec(compile(common.snippet(common.IMPORT_BODY, params), "<fusion_execute>", "exec"), ns, ns)
    return ns["result"], im, root


out, im, root = run_import({"path": str(motor), "format": "f3d", "name": "Motor", "units": "mm"})
check("an archive imports through the archive options", im.calls, [("archive", str(motor))])
check("and reports the occurrence it made", out["occurrences"], ["motor:1"])
check("renamed as asked", root.occurrences[-1].component.name, "Motor")
check("with its body count", out["bodies"], 2)
check("and its size in millimetres", out["size_mm"], [37.0, 75.0, 37.0])
check("nothing that was there before is reported as new",
      "chassis:1" in out["occurrences"], False)

out, im, root = run_import({"path": str(motor), "format": "f3d", "name": "", "units": "mm"},
                           im=ImportManager(Root(), modern=False))
check("an older Fusion without importToTarget2 still reports what arrived",
      out["occurrences"], ["motor:1"])
check("and keeps the imported name when none is given", root.occurrences[-1].component.name, "motor")

# A failure raises, like any failing code: it comes back as a traceback, with
# lessons or a learn nudge, and no autosave of a half-imported design.
refused = ImportManager(Root(), modern=False, accept=False)
raises("a refused import raises instead of claiming success",
       lambda: run_import({"path": str(motor), "format": "f3d", "name": "", "units": "mm"},
                          im=refused), RuntimeError, "refused")
raises("so does a file that vanished on the way",
       lambda: run_import({"path": str(SANDBOX / "gone.f3d"), "format": "f3d", "name": "",
                           "units": "mm"}), RuntimeError, "gone.f3d")

# Mesh: no import options in older APIs; meshBodies.add inside a base feature
# in a parametric design.
mesh_file = make(downloads / "scan.stl", 1)
added = []


class MeshRoot(Root):
    def __init__(self):
        super().__init__()
        outer = self

        def add_new_component(matrix):
            comp = NS(name="Component1", meshBodies=NS(add=lambda path, units, base=None:
                                                       added.append((path, units, base)) or Collection([NS()])),
                      features=NS(baseFeatures=NS(add=lambda: NS(
                          startEdit=lambda: added.append("start"),
                          finishEdit=lambda: added.append("finish")))))
            occ = NS(name="scan:1", component=comp, bRepBodies=Collection(),
                     boundingBox=box((0, 0, 0), (1, 1, 1)))
            outer.occurrences.append(occ)
            return occ

        self.occurrences.addNewComponent = add_new_component


out, _, _ = run_import({"path": str(mesh_file), "format": "mesh", "name": "Scan", "units": "in"},
                       root=MeshRoot())
check("a mesh goes in through a base feature in a parametric design",
      [a if isinstance(a, str) else a[:2] for a in added],
      ["start", (str(mesh_file), "in-unit"), "finish"])
check("as its own component", out["occurrences"], ["scan:1"])


# -------------------------------------------------------------- wiring ----
print("The tool")

from fastmcp.exceptions import ToolError  # noqa: E402

from arges_mcp import experience as xp  # noqa: E402
from arges_mcp import server as srv  # noqa: E402

common.import_roots = lambda extra=(): roots
os.environ["ARGES_AUTOSAVE"] = "0"
real_bridge = srv._bridge_request
sent = []


def bridge(answer):
    def request(method, path, payload=None):
        if path == "/health":
            return {"ok": True, "document": "tower", "bridge_version": "1"}
        sent.append((payload or {}).get("code", ""))
        return dict(answer)
    return request


try:
    listing = srv.fusion_import()
    check("with no file, it lists what can be imported",
          [f["name"] for f in listing["files"]][:3],
          ["scan.stl", "bracket.STEP", "motor-RECOVERED.f3d"])
    truthy("and says where it looked", listing["searched"])

    sent.clear()
    srv._bridge_request = bridge({"ok": True, "result": {"ok": True, "occurrences": ["motor:1"]},
                                  "stdout": ""})
    raises("an unknown file is refused", lambda: srv.fusion_import("nope.f3d"), ToolError,
           "motor-RECOVERED.f3d")
    check("without anything reaching Fusion", sent, [])

    out = srv.fusion_import("motor-RECOVERED")
    check("a bare name imports", out["result"]["occurrences"], ["motor:1"])
    truthy("with the path passed as data", str(motor) in sent[-1])
    truthy("the import body ran", "_fx_import" in sent[-1])
    truthy("named after the file unless told otherwise", "motor-RECOVERED" in sent[-1])
    rec = xp.default_log().read()[-1]
    check("the log records it as an import", (rec["tool"], rec["ok"]), ("fusion_import", True))
    check("of which file", rec.get("file"), str(motor))
finally:
    srv._bridge_request = real_bridge
    os.environ.pop("ARGES_AUTOSAVE", None)


import shutil  # noqa: E402
shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
