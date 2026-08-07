# Remove the scratch files this project left in Downloads.
#
# Deletes by an explicit list of names, never by pattern. A glob like *.ps1
# would take whatever else happens to be in there, and this is somebody's
# personal Downloads folder, not a scratch directory.
#
# Anything not on the list is left alone and reported, so a file that should
# have been included shows up rather than being silently missed.

$ErrorActionPreference = "Stop"
$Downloads = "$env:USERPROFILE\Downloads"

# Every file this project wrote there, across the whole session.
$Ours = @(
    # environment probes
    "devcheck.ps1", "pycheck.ps1", "sdk.ps1", "findpy.ps1", "fw.ps1", "lic.ps1",
    "crash.ps1", "cc.ps1", "cc2.ps1", "cc3.ps1", "diag.ps1", "install.ps1",
    # start / restart helpers
    "runbroker.ps1", "launchapp.ps1", "chatapp.ps1", "relaunch.ps1",
    "bootstrap.ps1", "boot2.ps1", "restore.ps1", "watch.ps1",
    # encoding and protocol tests
    "enc.ps1", "enc2.ps1", "enc3.ps1", "mcpenc.ps1", "mcp2.ps1", "mcptest.ps1",
    "bom.ps1", "bom2.ps1", "perm.ps1", "perm2.ps1", "hist.ps1", "st.ps1",
    "apitest.ps1", "runner.ps1",
    # the python those wrote out
    "enc.py", "enc2.py", "enc3.py", "mcpenc.py", "mcp2.py", "mcptest.py",
    "perm.py", "perm2.py", "hist.py", "st.py", "apitest.py", "runner.py",
    "check.py", "cleanup.py", "verify_intact.py", "mcp_utf8_test.py",
    "real_rhino_test.py", "permtest.txt",
    # early Rhino spike scripts, before there was a project folder
    "rhino-bridge-prototype.py", "rhino-bridge-start.py", "rhino-idle-test.py",
    "rhino-marshal-diagnose.py", "rhino-timer-test.py", "rhino-timer-stop.py",
    "rhino-probe2.py", "rhino-car-test.py", "windows-rhino-setup.ps1",
    "rhino-bridge-info.json", "rhino-idle-ticks.txt", "rhino-timer-ticks.txt",
    "rhino-marshal-marks.txt", "rhino-bridge-start-output.txt",
    "rhino-idle-test-output.txt", "rhino-timer-test-output.txt",
    "rhino-timer-stop-output.txt", "rhino-marshal-output.txt",
    "rhino-bridge-idle-output.txt", "mcp-real-shot.png"
)

$removed = 0
$missing = 0
foreach ($name in $Ours) {
    $path = Join-Path $Downloads $name
    if (Test-Path $path) {
        Remove-Item $path -Force
        $removed++
    } else {
        $missing++
    }
}

"removed $removed file(s); $missing were already gone"
""
"Still in Downloads (yours - nothing here was touched):"
$left = Get-ChildItem $Downloads -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
if ($left) {
    $left | Select-Object -First 25 |
        ForEach-Object { "  {0,-44} {1}" -f $_.Name, $_.LastWriteTime.ToString("yyyy-MM-dd") }
    if ($left.Count -gt 25) { "  ... and {0} more" -f ($left.Count - 25) }
} else {
    "  (empty)"
}
""
"If anything above looks like ours rather than yours, say so and it will be added to the list."
