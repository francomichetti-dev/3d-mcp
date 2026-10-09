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

SANDBOX = Path(tempfile.mkdtemp(prefix="arges-import-test-")).resolve()
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
# Review finding: a relative path was checked against this process's working
# directory and then handed to Fusion as given, where it means something else.
here = os.getcwd()
os.chdir(SANDBOX)
try:
    (SANDBOX / "parts").mkdir(exist_ok=True)
    (SANDBOX / "parts" / "rel.step").write_bytes(b"x")
    got = common.resolve_import("parts/rel.step", roots)
finally:
    os.chdir(here)
truthy("a relative path is made absolute before Fusion sees it", got.is_absolute())
check("pointing at the same file (macOS reports /var as /private/var)",
      got.resolve(), (SANDBOX / "parts" / "rel.step").resolve())

same_name = make(desktop / "motor-RECOVERED.f3d", 1)
check("with the same name in two folders, the newest wins",
      common.resolve_import("motor-RECOVERED.f3d", roots), same_name)
same_name.unlink()


# ------------------------------------------------------ inside Fusion ----
# The snippets, run as Fusion would run them, against stand-ins built to match
# what was measured on Fusion 2704 with an expired subscription (2026-10-08):
#
#   importManager into an unsaved design, f3d or STEP   RuntimeError
#   importToNewDocument from a local file               "... while expired"
#   TemporaryBRepManager.createFromFile on a STEP       "invalid argument filename"
#   copying bodies from another open document          works
#   meshBodies.add of an STL inside a base feature     works
#   a size read in the same call as the import         0, stale; read it next call
print("Bringing it in")


class Collection(list):
    @property
    def count(self):
        return len(self)

    def item(self, i):
        return self[i]


def box(lo, hi):
    return NS(minPoint=NS(x=lo[0], y=lo[1], z=lo[2]), maxPoint=NS(x=hi[0], y=hi[1], z=hi[2]))


added = []


class Attributes(dict):
    """A component's attributes, as Fusion keeps them: by group and name."""

    def add(self, group, name, value):
        self[(group, name)] = NS(value=value)

    def itemByName(self, group, name):
        return self.get((group, name))


class Bodies(Collection):
    """A component's bodies: add() appends a new, generically named body, as
    Fusion does (a copy arrives as Body1, Body2...)."""

    def add(self, body, base=None):
        added.append(("body", body))
        new = NS(name="Body" + str(len(self) + 1))
        self.append(new)
        return new


def new_component_factory(root, occ_name):
    def add_new_component(matrix):
        comp = NS(name="Component1", attributes=Attributes(),
                  meshBodies=NS(add=lambda path, units, base=None:
                                added.append(("mesh", path, units)) or Collection([NS()])),
                  bRepBodies=Bodies(),
                  features=NS(baseFeatures=NS(add=lambda: NS(
                      startEdit=lambda: added.append("start"),
                      finishEdit=lambda: added.append("finish")))))
        occ = NS(name=occ_name, component=comp, bRepBodies=Collection(),
                 boundingBox=box((0, 0, 0), (0, 0, 0)))
        occ.deleteMe = lambda: root.occurrences.remove(occ)
        root.occurrences.append(occ)
        return occ
    return add_new_component


def occurrence(name, bodies=1):
    comp = NS(name=name.split(":")[0], meshBodies=Collection(), attributes=Attributes())
    return NS(name=name, component=comp, bRepBodies=Collection([NS()] * bodies),
              boundingBox=box((0, 0, 0), (3.7, 7.5, 3.7)))


class Root:
    def __init__(self, occ_name="new:1"):
        self.occurrences = Collection([occurrence("chassis:1")])
        self.occurrences.addNewComponent = new_component_factory(self, occ_name)
        self.bRepBodies = Collection()
        self.nested = Collection()

    @property
    def allOccurrences(self):
        return Collection([*self.occurrences, *self.nested])


class ImportManager:
    def __init__(self, root, modern=True, accept=True, raises=None):
        self.root, self.accept, self.raises, self.calls = root, accept, raises, []
        if not modern:
            self.importToTarget2 = None

    def createFusionArchiveImportOptions(self, path):
        self.calls.append(("archive", path))
        return NS(kind="archive", path=path)

    def createSTEPImportOptions(self, path):
        self.calls.append(("step", path))
        return NS(kind="step", path=path)

    def importToTarget2(self, options, target):
        if self.raises:
            raise RuntimeError(self.raises)
        new = occurrence("motor:1", bodies=2)
        target.occurrences.append(new)
        return Collection([new])

    def importToTarget(self, options, target):
        if self.raises:
            raise RuntimeError(self.raises)
        if not self.accept:
            return False
        target.occurrences.append(occurrence("motor:1", bodies=2))
        return True


def fusion(root, im=None, parametric=True, documents=(), from_file=None):
    design = NS(rootComponent=root, designType=1 if parametric else 0, attributes=Attributes())
    active = NS(name="Untitled", products=NS(itemByProductType=lambda t: design), dataFile=None)

    def create_from_file(path):
        if from_file is None:
            raise RuntimeError("3 : invalid argument filename")
        return Collection(from_file)

    fake_adsk = NS(fusion=NS(Design=NS(cast=lambda product: product),
                             DesignTypes=NS(ParametricDesignType=1, DirectDesignType=0),
                             MeshUnits=NS(MillimeterMeshUnit="mm-unit", CentimeterMeshUnit="cm-unit",
                                          MeterMeshUnit="m-unit", InchMeshUnit="in-unit",
                                          FootMeshUnit="ft-unit"),
                             TemporaryBRepManager=NS(get=lambda: NS(
                                 copy=lambda body: ("copy", body),
                                 createFromFile=create_from_file))),
                   core=NS(Matrix3D=NS(create=lambda: "identity")))
    app = NS(importManager=im or ImportManager(root), activeProduct=design,
             activeDocument=active, documents=Collection([active, *documents]))
    return {"adsk": fake_adsk, "app": app, "ui": NS(), "design": design}


def run(body, params, ns):
    exec(compile(common.snippet(body, params), "<fusion_execute>", "exec"), ns, ns)
    return ns["result"]


def run_import(params, ns):
    """IMPORT_BODY with the parameters the server builds."""
    return run(common.IMPORT_BODY, common.import_params(
        params["path"], params["format"], params["name"], params["units"]), ns)


root = Root()
ns = fusion(root)
out = run_import({"path": str(motor), "format": "f3d", "name": "Motor", "units": "mm"}, ns)
check("an archive imports through the archive options", ns["app"].importManager.calls,
      [("archive", str(motor))])
check("and reports the occurrence it made", out["occurrences"], ["motor:1"])
check("renamed as asked", root.occurrences[-1].component.name, "Motor")
check("through importManager", out["method"], "importManager")
check("nothing that was there before is reported as new", "chassis:1" in out["occurrences"], False)
check("no size from the same call: it would be stale", "size_mm" in out, False)

root = Root()
out = run_import({"path": str(motor), "format": "f3d", "name": "", "units": "mm"},
          fusion(root, im=ImportManager(root, modern=False)))
check("an older Fusion without importToTarget2 still reports what arrived",
      out["occurrences"], ["motor:1"])
check("and keeps the imported name when none is given", root.occurrences[-1].component.name, "motor")

# The expired-subscription case, measured live: importManager refuses.
root = Root()
refusing = ImportManager(root, raises="2 : InternalValidationError : pResult")
try:
    run_import({"path": str(motor), "format": "f3d", "name": "", "units": "mm"},
        fusion(root, im=refusing))
    message = ""
except RuntimeError as exc:
    message = str(exc)
truthy("a refused archive raises, rather than claiming success", "refused" in message)
truthy("saying what still works: copying from an open tab", "from_document" in message)
truthy("or a mesh export", "STL" in message)
check("and the message names no file, so one lesson matches every file",
      str(motor) in message or "motor-RECOVERED" in message, False)

root = Root(occ_name="bracket:1")
solids = [NS(name="b1"), NS(name="b2")]
out = run_import({"path": str(bracket), "format": "step", "name": "Bracket", "units": "mm"},
          fusion(root, im=ImportManager(root, raises="2 : InternalValidationError : pResult"),
                 from_file=solids))
check("a refused STEP falls back to reading the file's solids directly",
      out["method"], "createFromFile")
check("into a component of its own", out["occurrences"], ["bracket:1"])

root = Root()
raises("and when that fails too, it says so, with the same guidance",
       lambda: run_import({"path": str(bracket), "format": "step", "name": "",
                                       "units": "mm"},
                   fusion(root, im=ImportManager(root, raises="2 : InternalValidationError : pResult"))),
       RuntimeError, "from_document")

root = Root()
refused = ImportManager(root, modern=False, accept=False)
raises("an importToTarget that returns False is a refusal too",
       lambda: run_import({"path": str(motor), "format": "f3d", "name": "", "units": "mm"},
                   fusion(root, im=refused)), RuntimeError, "refused")
raises("a file that vanished on the way is an error",
       lambda: run_import({"path": str(SANDBOX / "gone.f3d"), "format": "f3d",
                                       "name": "", "units": "mm"}, fusion(Root())),
       RuntimeError, "gone")

# Review finding (Codex, 2026-10-08): an importManager that "succeeds" with
# nothing to show for it reported ok with no occurrences, and was autosaved.
class EmptyImport(ImportManager):
    def importToTarget2(self, options, target):
        return Collection()


root = Root()
raises("an import that brings nothing in is a refusal, not a success",
       lambda: run_import({"path": str(motor), "format": "f3d", "name": "", "units": "mm"},
                          fusion(root, im=EmptyImport(root))), RuntimeError, "nothing")

mesh_file = make(downloads / "scan.stl", 1)
added.clear()
root = Root(occ_name="scan:1")
out = run_import({"path": str(mesh_file), "format": "mesh", "name": "Scan", "units": "in"},
          fusion(root))
check("a mesh goes in through a base feature in a parametric design",
      added, ["start", ("mesh", str(mesh_file), "in-unit"), "finish"])
check("as its own component", out["occurrences"], ["scan:1"])
check("by meshBodies.add", out["method"], "mesh")

# Review finding: a failed add skipped finishEdit, leaving the base feature
# open and a half-made component in the design.
added.clear()
root = Root(occ_name="broken:1")


def failing_factory(root):
    make = new_component_factory(root, "broken:1")

    def add_new_component(matrix):
        occ = make(matrix)
        occ.component.meshBodies.add = lambda *a: (_ for _ in ()).throw(RuntimeError("bad mesh"))
        return occ
    return add_new_component


root.occurrences.addNewComponent = failing_factory(root)
raises("a mesh that fails to load still raises",
       lambda: run_import({"path": str(mesh_file), "format": "mesh", "name": "", "units": "mm"},
                          fusion(root)), RuntimeError, "bad mesh")
check("but the base feature's edit is closed", added, ["start", "finish"])
check("and the half-made component is removed",
      [o.name for o in root.occurrences], ["chassis:1"])


# ------------------------------------------------------- no duplicates ----
# The replay left the motor and tank-wheels stacked three deep at the origin,
# under different names, where they looked like one part and hid each other's
# edits. Asked to make sure it never happens again: each import now records
# its source on the component, and a second import of the same source is
# refused unless asked for.
print("Never a second copy by accident")

root = Root()
ns = fusion(root)
run_import({"path": str(motor), "format": "f3d", "name": "Motor", "units": "mm"}, ns)
check("an import records where it came from",
      root.occurrences[-1].component.attributes.itemByName("Arges", "source").value,
      "file:" + str(motor))
raises("the same file again is refused, whatever it is called",
       lambda: run(common.IMPORT_BODY, dict(common.import_params(motor, "f3d", "Motor again", "mm")), ns),
       RuntimeError, "allow_duplicate")
check("and nothing was added", [o.name for o in root.occurrences], ["chassis:1", "motor:1"])
params = common.import_params(motor, "f3d", "Motor again", "mm", allow_duplicate=True)
run(common.IMPORT_BODY, params, ns)
check("unless a second copy is asked for", len(root.occurrences), 3)

# Moving the motor out of the root used to hide its stamp from the guard.
root = Root()
ns = fusion(root)
run(common.IMPORT_BODY, common.import_params(motor, "f3d", "Motor"), ns)
imported = root.occurrences.pop()
root.nested.append(imported)
raises("a nested import still counts under another name",
       lambda: run(common.IMPORT_BODY, common.import_params(motor, "f3d", "Another motor"), ns),
       RuntimeError, "motor:1 is already")
run(common.IMPORT_BODY, common.import_params(motor, "f3d", "Another motor", allow_duplicate=True), ns)
check("a deliberate second copy of a nested import is allowed", len(root.allOccurrences), 3)

class CountingAttributes(Attributes):
    def itemByName(self, group, name):
        reads.append((group, name))
        return super().itemByName(group, name)


root = Root()
reads = []
root.occurrences[0].component.attributes = CountingAttributes()
instance = occurrence("chassis:2")
instance.component = root.occurrences[0].component
root.nested.append(instance)
run(common.IMPORT_BODY, common.import_params(motor, "f3d", "Motor"), fusion(root))
check("repeated instances read their component's stamps once",
      reads, [("Arges", "source"), ("Arges", "sources")])

alias = downloads / "motor-alias.f3d"
alias.symlink_to(motor)
check("a symlink resolves to the original file", common.resolve_import(str(alias), roots), motor)
check("so does a symlink found by name", common.resolve_import(alias.name, roots), motor)
check("even parameters built directly use the real path",
      common.import_params(alias, "f3d")["path"], str(motor))
root = Root()
ns = fusion(root)
run(common.IMPORT_BODY, common.import_params(motor, "f3d", "Motor"), ns)
raises("a file alias cannot hide a second import under another name",
       lambda: run(common.IMPORT_BODY, common.import_params(alias, "f3d", "Alias"), ns),
       RuntimeError, "allow_duplicate=true")
run(common.IMPORT_BODY, common.import_params(alias, "f3d", "Alias", allow_duplicate=True), ns)
check("a deliberate import through an alias is allowed", len(root.occurrences), 3)
alias.unlink()

root = Root()
root.occurrences[0].component.attributes.add("Arges", "source", "file:" + str(motor))
ns = fusion(root)
raises("an old single-value stamp still counts",
       lambda: run(common.IMPORT_BODY, common.import_params(motor, "f3d", "Motor"), ns),
       RuntimeError, "chassis:1 is already")
run(common.IMPORT_BODY, common.import_params(motor, "f3d", "Motor", allow_duplicate=True), ns)
check("an old stamp also permits a deliberate second copy", len(root.occurrences), 2)

root = Root(occ_name="chassis:2")
raises("a component of the same name already there counts too, sources recorded or not",
       lambda: run_import({"path": str(bracket), "format": "step", "name": "chassis", "units": "mm"},
                          fusion(root, im=ImportManager(root))), RuntimeError, "chassis:1")
try:
    run_import({"path": str(bracket), "format": "step", "name": "chassis", "units": "mm"},
               fusion(Root(), im=ImportManager(Root())))
    last_line = ""
except RuntimeError as exc:
    last_line = str(exc).splitlines()[-1]
check("the refusal's last line names nothing, so one lesson covers every case",
      "chassis" in last_line, False)


# -------------------------------------------- copying from an open tab ----
print("Copying from another open document")


def document(name, bodies=1, saved=False, body_names=None):
    names = body_names or [f"{name}-body{i}" for i in range(bodies)]
    src_root = NS(bRepBodies=Collection([NS(name=n, boundingBox=box((0, 0, 0), (3.6, 3.6, 8.05)))
                                         for n in names]),
                  allOccurrences=Collection())
    des = NS(rootComponent=src_root, attributes=Attributes())
    return NS(name=name, products=NS(itemByProductType=lambda t: des),
              dataFile=NS(id="x") if saved else None)


motor_tab = document("Untitled", body_names=["Motor_XD37"])
other_untitled = document("Untitled", bodies=0)
wheels = document("tank-wheels", bodies=11, saved=True)
added.clear()
root = Root(occ_name="motor:1")
ns = fusion(root, documents=[wheels, motor_tab, other_untitled])

listing_out = run(common.OPEN_DOCUMENTS_BODY, {}, ns)
check("open documents are listed with references that tell same-named ones apart",
      [(d["ref"], d["bodies"]) for d in listing_out["documents"]],
      [("tank-wheels", 11), ("Untitled #2", 1), ("Untitled #3", 0)])
# The replay that found this: the model saw "Untitled #2, 1 body" and could
# not tell that tab was the motor, so the motor never arrived.
motor_entry = [d for d in listing_out["documents"] if d["ref"] == "Untitled #2"][0]
check("each tab says what is in it, so the right one can be recognised",
      motor_entry["body_names"], ["Motor_XD37"])
check("and how big it is", motor_entry["size_mm"], [36.0, 36.0, 80.5])
check("the design being imported into is not offered as a source",
      any(d["ref"] == "Untitled #1" for d in listing_out["documents"]), False)

out = run(common.COPY_BODY, {"document": "Untitled #2", "name": "motor"}, ns)
check("copying takes the bodies of the named tab",
      [a for a in added if isinstance(a, str) or a[0] == "body"],
      ["start", ("body", ("copy", motor_tab.products.itemByProductType("d").rootComponent.bRepBodies[0])),
       "finish"])
check("into a new component", out["occurrences"], ["motor:1"])
# The replay that found this: tank-wheels arrived as Body1..Body11, so the
# organising step that followed had nothing to go on.
check("the copies keep their names", out["bodies"], ["Motor_XD37"])
raises("copying the same tab again is refused too, under any name",
       lambda: run(common.COPY_BODY, {"document": "Untitled #2", "name": "motor-two"}, ns),
       RuntimeError, "allow_duplicate")
run(common.COPY_BODY, {"document": "Untitled #2", "name": "motor-two", "allow_duplicate": True}, ns)
check("unless asked for", [o.name for o in root.occurrences][-2:], ["motor:1", "motor:1"])
# Verified live: a name set inside the base feature's edit does not survive
# finishEdit, so the result reads the names back rather than trusting them.
check("as read back from the component, not as intended",
      [b.name for b in root.occurrences[-1].component.bRepBodies], ["Motor_XD37"])
check("and says it is geometry without history", out["method"], "copied bodies (no history)")
raises("an ambiguous name is refused, listing the references",
       lambda: run(common.COPY_BODY, {"document": "Untitled", "name": ""}, ns),
       RuntimeError, "Untitled #2")
raises("so is copying the design into itself",
       lambda: run(common.COPY_BODY, {"document": "Untitled #1", "name": ""}, ns),
       RuntimeError, "into itself")
raises("and a tab that is not open",
       lambda: run(common.COPY_BODY, {"document": "gearbox", "name": ""}, ns),
       RuntimeError, "tank-wheels")

# A tab reference can change without the design changing, or be reused by a
# different design. It cannot be the identity recorded on a copied component.
earlier = document("Untitled", bodies=0)
source_tab = document("Untitled")
root = Root(occ_name="first:1")
ns = fusion(root, documents=[earlier, source_tab])
run(common.COPY_BODY, {"document": "Untitled #3", "name": "First"}, ns)
stamp = source_tab.products.itemByProductType("d").attributes.itemByName(
    common.DOC_ATTR_GROUP, common.DOC_ATTR_NAME)
truthy("an unsaved source receives a stable chat identity", stamp and stamp.value.startswith("doc:"))
check("the copied source uses that identity",
      root.occurrences[-1].component.attributes.itemByName("Arges", "source").value,
      "document:" + stamp.value if stamp else "missing identity")
ns["app"].documents.remove(earlier)
raises("renumbering an unsaved source cannot permit a second copy",
       lambda: run(common.COPY_BODY, {"document": "Untitled #2", "name": "Renumbered"}, ns),
       RuntimeError, "first:1 is already")
run(common.COPY_BODY, {"document": "Untitled #2", "name": "Renumbered", "allow_duplicate": True}, ns)
check("a renumbered source still permits a deliberate second copy", len(root.occurrences), 3)
replacement = document("Untitled")
ns["app"].documents.append(replacement)
try:
    run(common.COPY_BODY, {"document": "Untitled #3", "name": "Different"}, ns)
    message = ""
except RuntimeError as exc:
    message = str(exc)
check("a reused reference is not falsely refused", message, "")
check("a different tab reusing the old reference is allowed", len(root.occurrences), 4)
replacement_stamp = replacement.products.itemByProductType("d").attributes.itemByName(
    common.DOC_ATTR_GROUP, common.DOC_ATTR_NAME)
truthy("different unsaved tabs receive different identities",
       stamp and replacement_stamp and stamp.value != replacement_stamp.value)

saved = document("Saved motor", saved=True)
root = Root()
ns = fusion(root, documents=[saved])
run(common.COPY_BODY, {"document": "Saved motor", "name": "Saved"}, ns)
check("a saved source keeps its dataFile identity",
      root.occurrences[-1].component.attributes.itemByName("Arges", "source").value,
      "document:file:x")
check("a saved source needs no new chat stamp", saved.products.itemByProductType("d").attributes, {})

stamped = document("Stamped motor")
stamped.products.itemByProductType("d").attributes.add(
    common.DOC_ATTR_GROUP, common.DOC_ATTR_NAME, "doc:existing")
root = Root()
ns = fusion(root, documents=[stamped])
run(common.COPY_BODY, {"document": "Stamped motor", "name": "Stamped"}, ns)
check("a source with a chat stamp keeps it",
      root.occurrences[-1].component.attributes.itemByName("Arges", "source").value,
      "document:doc:existing")

# A -> B -> C used to forget A, allowing A to be added to C a second time.
source_a = document("A")
root_b = Root(occ_name="copied-a:1")
root_b.occurrences.clear()
ns_b = fusion(root_b, documents=[source_a])
ns_b["app"].activeDocument.name = "B"
run(common.COPY_BODY, {"document": "A", "name": "A in B"}, ns_b)
copied_a = root_b.occurrences.pop()
copied_a.bRepBodies = copied_a.component.bRepBodies
root_b.nested.append(copied_a)
root_c = Root(occ_name="copied-b:1")
ns_c = fusion(root_c, documents=[ns_b["app"].activeDocument, source_a])
run(common.COPY_BODY, {"document": "B", "name": "B in C"}, ns_c)
raises("a copy of a nested copy retains the original tab's provenance",
       lambda: run(common.COPY_BODY, {"document": "A", "name": "A again"}, ns_c),
       RuntimeError, "copied-b:1 is already")
raises("a source assembly containing the same geometry is refused too",
       lambda: run(common.COPY_BODY, {"document": "B", "name": "B again"}, ns_c),
       RuntimeError, "allow_duplicate=true")
run(common.COPY_BODY, {"document": "A", "name": "A again", "allow_duplicate": True}, ns_c)
check("the original tab can still be copied deliberately", len(root_c.occurrences), 3)

# File provenance must travel too, including old stamps on nested components.
source_b = document("File assembly", bodies=0)
file_occ = occurrence("old-motor:1")
file_occ.bRepBodies[0].name = "Motor"
file_occ.component.attributes.add("Arges", "source", "file:" + str(motor))
source_b.products.itemByProductType("d").rootComponent.allOccurrences.append(file_occ)
root = Root(occ_name="assembly:1")
ns = fusion(root, documents=[source_b])
run(common.COPY_BODY, {"document": "File assembly", "name": "Assembly"}, ns)
raises("an imported file's old stamp survives copying the assembly",
       lambda: run(common.IMPORT_BODY, common.import_params(motor, "f3d", "Motor again"), ns),
       RuntimeError, "allow_duplicate=true")
run(common.IMPORT_BODY, common.import_params(motor, "f3d", "Motor again", allow_duplicate=True), ns)
check("the inherited file can still be imported deliberately", len(root.occurrences), 3)

measured = run(common.MEASURE_BODY, {"occurrences": ["chassis:1"]},
               fusion(Root()))
check("the size is measured in a call of its own, in millimetres",
      measured["chassis:1"]["size_mm"], [37.0, 75.0, 37.0])


# -------------------------------------------------------------- wiring ----
print("The tool")

from fastmcp.exceptions import ToolError  # noqa: E402

from arges_mcp import experience as xp  # noqa: E402
from arges_mcp import server as srv  # noqa: E402

common.import_roots = lambda extra=(): roots
os.environ["ARGES_AUTOSAVE"] = "0"
real_bridge = srv._bridge_request
sent = []


def bridge(answer, docs=None):
    def request(method, path, payload=None):
        if path == "/health":
            return {"ok": True, "document": "tower", "bridge_version": "1"}
        code = (payload or {}).get("code", "")
        sent.append(code)
        if "_fx_open_documents" in code:
            return {"ok": True, "result": {"documents": docs or []}, "stdout": ""}
        if "_fx_measure" in code:
            return {"ok": True, "result": {"motor:1": {"bodies": 1, "meshes": 0,
                                                       "size_mm": [36.0, 36.0, 80.5]}},
                    "stdout": ""}
        return dict(answer)
    return request


try:
    srv._bridge_request = bridge({}, docs=[{"ref": "Untitled #2", "bodies": 1, "saved": False}])
    listing = srv.fusion_import()
    check("with no file, it lists what can be imported",
          [f["name"] for f in listing["files"]][:3],
          ["scan.stl", "bracket.STEP", "motor-RECOVERED.f3d"])
    truthy("and says where it looked", listing["searched"])
    check("and which other open tabs it can copy from",
          listing["open_documents"], [{"ref": "Untitled #2", "bodies": 1, "saved": False}])

    srv._bridge_request = bridge(RuntimeError("bridge down"))
    def down(method, path, payload=None):
        raise ToolError("Fusion not running")
    srv._bridge_request = down
    check("a listing still works with Fusion closed, just without the tabs",
          len(srv.fusion_import()["files"]) > 0, True)

    sent.clear()
    srv._bridge_request = bridge({"ok": True, "result": {"ok": True, "occurrences": ["motor:1"],
                                                         "method": "importManager"}, "stdout": ""})
    raises("an unknown file is refused", lambda: srv.fusion_import("nope.f3d"), ToolError,
           "motor-RECOVERED.f3d")
    raises("a file and a tab at once is refused",
           lambda: srv.fusion_import("motor", from_document="Untitled #2"), ToolError)
    check("without anything reaching Fusion", sent, [])

    out = srv.fusion_import("motor-RECOVERED")
    check("a bare name imports", out["result"]["occurrences"], ["motor:1"])
    truthy("with the path passed as data", any(str(motor) in c for c in sent))
    truthy("the import body ran", any("_fx_import" in c for c in sent))
    truthy("named after the file unless told otherwise", any("motor-RECOVERED" in c for c in sent))
    check("then measured in a fresh call, so the size is real",
          out["result"]["measured"]["motor:1"]["size_mm"], [36.0, 36.0, 80.5])
    rec = [r for r in xp.default_log().read() if r.get("tool") == "fusion_import"][-1]
    check("the log records it as an import", rec["ok"], True)
    check("of which file", rec.get("file"), str(motor))

    sent.clear()
    srv.fusion_import("motor-RECOVERED", allow_duplicate=True)
    truthy("allow_duplicate reaches Fusion", any('allow_duplicate\\": true' in c for c in sent))

    sent.clear()
    out = srv.fusion_import(from_document="Untitled #2", name="motor")
    truthy("from_document copies from the open tab", any("_fx_copy" in c for c in sent))
    check("and is measured the same way", out["result"]["measured"]["motor:1"]["size_mm"],
          [36.0, 36.0, 80.5])
finally:
    srv._bridge_request = real_bridge
    os.environ.pop("ARGES_AUTOSAVE", None)


import shutil  # noqa: E402
shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
