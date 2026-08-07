# Run the 3d-mcp broker so it survives the terminal that started it.
#
# Start-Process is not enough: Windows puts the session in a job object
# and kills the whole tree when the connection closes, so the broker died every
# time. A scheduled task is owned by the task scheduler instead, not by us.

param([switch]$Stop)

$ErrorActionPreference = "Stop"
$TaskName = "3d-mcp-broker"
$Dir      = "$env:USERPROFILE\3d-mcp"
$Py       = "$env:USERPROFILE\.rhinocode\py39-rh8\python.exe"
$Script   = "$Dir\run_broker.py"
$Log      = "$Dir\broker.log"

function Stop-Broker {
    # "task does not exist" is the normal first-run state, but a native
    # command writing to stderr under ErrorActionPreference=Stop becomes a
    # terminating NativeCommandError. Suppress locally rather than globally.
    $old = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    schtasks /end /tn $TaskName 2>&1 | Out-Null
    schtasks /delete /tn $TaskName /f 2>&1 | Out-Null
    $ErrorActionPreference = $old
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*run_broker*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    "stopped"
}

if ($Stop) { Stop-Broker; exit 0 }

Stop-Broker | Out-Null

if (-not (Test-Path $Py))  { "ERROR: no Rhino python at $Py"; exit 1 }
if (-not (Test-Path $Dir)) { "ERROR: no $Dir"; exit 1 }

# Written here rather than shipped, so the paths are always this machine's.
@"
import sys, time, traceback
sys.path.insert(0, r'$Dir')
log = open(r'$Log', 'a', encoding='utf-8', buffering=1)
try:
    import broker
    server, b = broker.serve(port=7656)
    log.write('%s  broker listening on 127.0.0.1:7656\n' % time.strftime('%H:%M:%S'))
    while True:
        time.sleep(5)
except Exception:
    log.write(traceback.format_exc())
    raise
"@ | Set-Content -Path $Script -Encoding ascii

# /it so it runs in the interactive session, which is where Rhino lives and
# where %USERPROFILE% resolves to the right token file.
$old = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
schtasks /create /tn $TaskName /f /sc ONCE /st 00:00 /it /rl LIMITED `
    /tr "`"$Py`" `"$Script`"" 2>&1 | Out-Null
schtasks /run /tn $TaskName 2>&1 | Out-Null
$ErrorActionPreference = $old

Start-Sleep -Seconds 4

$listening = (netstat -an | Select-String ":7656.*LISTENING")
if ($listening) {
    "broker RUNNING on 127.0.0.1:7656"
    if (Test-Path $Log) { Get-Content $Log -Tail 3 }
} else {
    "broker did NOT come up"
    if (Test-Path $Log) { Get-Content $Log -Tail 15 }
    schtasks /query /tn $TaskName /fo LIST | Select-String "Status|Last Result"
}
