<#
    3d-mcp - one-time Windows setup for a Rhino 8 host.

    Enables OpenSSH Server, authorises ONE key, finds Rhino, and prints a block
    to paste back. Run it once, in an ADMINISTRATOR PowerShell:

        Right-click Start -> "Terminal (Admin)" / "Windows PowerShell (Admin)"
        cd to this file's folder, then:
            powershell -ExecutionPolicy Bypass -File .\windows-rhino-setup.ps1

    What it changes on this machine, and nothing else:
      * installs and starts the OpenSSH Server Windows feature
      * adds ONE public key to the administrators' authorized_keys
      * adds a firewall rule for port 22, PRIVATE networks only

    What it deliberately does NOT do:
      * it does not disable password login or touch any other sshd setting
      * it does not open port 22 to public networks or to the internet
      * it does not install or start anything from the 3d-mcp project

    Read this before running: whoever holds the matching private key can run
    commands on this machine as you. Only run it if you meant to grant that.
    Undo instructions are printed at the end.
#>

[CmdletBinding()]
param(
    # The single key allowed in. Replace only if you were sent a different one.
    [string]$PublicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIqYIdUVxuAaNyBH5jsYjzsyDd32vqXy+zdTFrZojf1k 3d-mcp-rhino-bridge@Francos-MacBook-Air",

    # Report only. Changes nothing - good for looking first.
    [switch]$ReportOnly
)

$ErrorActionPreference = "Stop"
$report = [ordered]@{}

function Say([string]$text) { Write-Host $text }
function Step([string]$text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Ok([string]$text)   { Write-Host "  [ok]   $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "  [warn] $text" -ForegroundColor Yellow }
function Bad([string]$text)  { Write-Host "  [FAIL] $text" -ForegroundColor Red }

Say "3d-mcp - Windows / Rhino 8 host setup"
Say ("=" * 62)

# ----------------------------------------------------------- privileges ----
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin -and -not $ReportOnly) {
    Bad "This needs an ADMINISTRATOR PowerShell."
    Say ""
    Say "  Right-click Start -> Terminal (Admin), then run it again."
    Say "  Or run with -ReportOnly to just collect information, no changes."
    exit 1
}

# ----------------------------------------------------------------- host ----
Step "This machine"
$report.Hostname = $env:COMPUTERNAME
$report.User     = $env:USERNAME
$os = Get-CimInstance Win32_OperatingSystem
$report.OS       = "$($os.Caption) $($os.Version)"
Ok "$($report.Hostname)  user=$($report.User)"
Ok $report.OS

# Every usable address, so the right one can be picked from the other side.
$addresses = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object {
        $_.IPAddress -ne "127.0.0.1" -and
        $_.PrefixOrigin -ne "WellKnown" -and
        $_.IPAddress -notlike "169.254.*"
    } |
    ForEach-Object {
        $alias = $_.InterfaceAlias
        "$($_.IPAddress)  ($alias)"
    }
$report.Addresses = ($addresses -join "; ")
foreach ($a in $addresses) { Ok "address $a" }

# Tailscale is worth knowing about: it beats port-forwarding a router.
$tailscale = Get-Command tailscale.exe -ErrorAction SilentlyContinue
if ($tailscale) {
    try {
        $tsIp = (& tailscale.exe ip -4 2>$null | Select-Object -First 1)
        if ($tsIp) { $report.Tailscale = $tsIp; Ok "tailscale $tsIp  (best route - no router changes)" }
    } catch { }
} else {
    $report.Tailscale = "not installed"
}

# ------------------------------------------------------------------ ssh ----
Step "OpenSSH Server"
$capability = Get-WindowsCapability -Online -Name "OpenSSH.Server*" |
    Select-Object -First 1

if ($null -eq $capability) {
    Bad "This build of Windows does not offer the OpenSSH Server capability."
    $report.SSH = "unavailable"
} else {
    if ($capability.State -ne "Installed") {
        if ($ReportOnly) {
            Warn "not installed (report-only, leaving it)"
        } else {
            Say "  installing (this can take a minute)..."
            Add-WindowsCapability -Online -Name $capability.Name | Out-Null
            Ok "installed"
        }
    } else {
        Ok "already installed"
    }

    if (-not $ReportOnly) {
        Set-Service -Name sshd -StartupType Automatic
        Start-Service sshd
        Ok "sshd running, and set to start with Windows"
    }

    $svc = Get-Service sshd -ErrorAction SilentlyContinue
    $report.SSH = if ($svc) { "$($svc.Status), startup=$((Get-Service sshd).StartType)" } else { "not running" }

    # Port: whatever sshd is actually configured for, not an assumption.
    $sshdConfig = "$env:ProgramData\ssh\sshd_config"
    $port = 22
    if (Test-Path $sshdConfig) {
        $portLine = Select-String -Path $sshdConfig -Pattern '^\s*Port\s+(\d+)' |
            Select-Object -First 1
        if ($portLine) { $port = [int]$portLine.Matches[0].Groups[1].Value }
    }
    $report.Port = $port
    Ok "port $port"

    if (-not $ReportOnly) {
        $ruleName = "3d-mcp OpenSSH Server (private)"
        if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
            # Private profile only. This must not become reachable from a cafe
            # network or straight off the internet.
            New-NetFirewallRule -DisplayName $ruleName -Direction Inbound `
                -Protocol TCP -LocalPort $port -Action Allow -Profile Private | Out-Null
            Ok "firewall rule added (PRIVATE networks only)"
        } else {
            Ok "firewall rule already present"
        }
    }
}

# ------------------------------------------------------------------ key ----
Step "Authorising the key"
if ($ReportOnly) {
    Warn "report-only, key not installed"
} elseif ([string]::IsNullOrWhiteSpace($PublicKey) -or $PublicKey -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-)') {
    Bad "The -PublicKey value does not look like an SSH public key. Nothing installed."
} else {
    # Windows quirk: for a member of Administrators, sshd reads this file and
    # NOT the usual ~/.ssh/authorized_keys.
    $adminKeys = "$env:ProgramData\ssh\administrators_authorized_keys"
    $existing = if (Test-Path $adminKeys) { Get-Content $adminKeys -Raw } else { "" }

    $fingerprintOf = ($PublicKey -split '\s+')[1]
    if ($existing -like "*$fingerprintOf*") {
        Ok "this key is already authorised"
    } else {
        Add-Content -Path $adminKeys -Value $PublicKey -Encoding ascii
        Ok "key added to administrators_authorized_keys"
    }

    # sshd refuses the file unless only Administrators and SYSTEM can write it.
    icacls $adminKeys /inheritance:r | Out-Null
    icacls $adminKeys /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
    Ok "permissions tightened (Administrators + SYSTEM only)"

    $report.AuthorizedKeys = $adminKeys
    $report.KeyCount = (Get-Content $adminKeys | Where-Object { $_.Trim() }).Count
}

# ---------------------------------------------------------------- rhino ----
Step "Rhino 8"
$rhinoExe = @(
    "$env:ProgramFiles\Rhino 8\System\Rhino.exe",
    "${env:ProgramFiles(x86)}\Rhino 8\System\Rhino.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($rhinoExe) {
    $version = (Get-Item $rhinoExe).VersionInfo.ProductVersion
    $report.Rhino = "$version  ($rhinoExe)"
    Ok "found $version"
    Ok $rhinoExe
} else {
    $report.Rhino = "NOT FOUND in Program Files"
    Warn "Rhino 8 not found in the usual place - say where it is installed"
}

$report.RhinoRunning = if (Get-Process -Name Rhino -ErrorAction SilentlyContinue) { "yes" } else { "no" }
Ok "currently running: $($report.RhinoRunning)"

# Rhino 8 keeps its CPython under the user profile; the bridge will run there.
$rhinoCode = "$env:USERPROFILE\.rhinocode"
if (Test-Path $rhinoCode) {
    $pythons = Get-ChildItem $rhinoCode -Recurse -Filter "python.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 3 -ExpandProperty FullName
    if ($pythons) {
        $report.RhinoPython = ($pythons -join "; ")
        foreach ($p in $pythons) { Ok "python $p" }
    } else {
        $report.RhinoPython = "no python.exe under .rhinocode yet"
        Warn "no embedded python found - open Rhino once and run ScriptEditor, then re-run this"
    }
} else {
    $report.RhinoPython = ".rhinocode missing"
    Warn "$rhinoCode does not exist - open Rhino once and run the ScriptEditor command"
}

# Ports the bridge would want, so a clash is known now rather than later.
$busy = @()
foreach ($p in 7654, 7655) {
    if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) {
        $busy += $p
    }
}
$report.BridgePorts = if ($busy) { "IN USE: $($busy -join ', ')" } else { "7654 and 7655 free" }
Ok $report.BridgePorts

# --------------------------------------------------------------- output ----
Say ""
Say ("=" * 62)
Say "COPY EVERYTHING BELOW THIS LINE AND SEND IT BACK"
Say ("=" * 62)
Say ""
Say "----- 3d-mcp host report -----"
foreach ($key in $report.Keys) {
    "{0,-16}: {1}" -f $key, $report[$key]
}
Say "----- end -----"
Say ""
Say ("=" * 62)
Say ""
Say "To undo everything this did:"
Say "  Stop-Service sshd; Set-Service sshd -StartupType Disabled"
Say "  Remove-NetFirewallRule -DisplayName '3d-mcp OpenSSH Server (private)'"
Say "  notepad $env:ProgramData\ssh\administrators_authorized_keys   # delete the key line"
Say ""
Say "Reminder: whoever holds the matching private key can run commands as you."
Say "Removing that one line from authorized_keys revokes it immediately."
