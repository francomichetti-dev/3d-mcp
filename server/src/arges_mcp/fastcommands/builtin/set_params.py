"""Create or update user parameters in one call, by expression.

args: {"set": {"wall": "2 mm", "boss_od": "m3_insert + 2 * wall",
"holes": {"expression": "4", "unit": "", "comment": "count"}}, "unit": "mm"}.
A plain string is the expression. New parameters get "unit" (default "mm");
pass "" for a unitless count, or "deg" for an angle. A dict sets one
parameter's unit and comment as well.
returns: {"parameters": [{"name", "expression", "unit", "created"}],
"errors": {name: message}}. One bad expression does not stop the others.
Changing an existing parameter rebuilds everything that references it. That
is the point, but say so first when it is one you did not create. Check
timeline afterwards for features that failed to recompute.
"""
# Runs inside Fusion. Expressions go in as strings, which keeps them
# unit-aware: "2 mm" is right whatever the document's display units are.
READONLY = False


def run(p):
    up = design.userParameters
    default_unit = str(p.get("unit", "mm"))
    done, errors = [], {}
    for name, spec in (p.get("set") or {}).items():
        if isinstance(spec, dict):
            expression = str(spec.get("expression", ""))
            unit = str(spec.get("unit", default_unit))
            comment = str(spec.get("comment", ""))
        else:
            expression, unit, comment = str(spec), default_unit, ""
        try:
            param = up.itemByName(name)
            created = param is None
            if created:
                param = up.add(name, adsk.core.ValueInput.createByString(expression),
                               unit, comment)
            else:
                param.expression = expression
                if comment:
                    param.comment = comment
            done.append({"name": name, "expression": param.expression,
                         "unit": param.unit, "created": created})
        except Exception as exc:                          # noqa: BLE001
            errors[name] = str(exc)
    return {"parameters": done, "errors": errors}
