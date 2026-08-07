"""Rhino's per-session scratch space.

`sticky` is the only reason this exists: it is where the poller parks its timer
so a second run can stop the first. Attaching that to the Rhino module instead
does not work — it is a .NET namespace and rejects setattr with "type does not
support setting attributes", which cost real time to discover.
"""

sticky = {}
