# get_python.ps1 -- download a portable CPython (embeddable) and lay it out as
# a standard venv so that .venv\Scripts\python.exe exists.
#
# WHY THIS EXISTS
#   tools\python_bootstrap.py can do everything, but it needs *a* Python to run.
#   On a machine with no Python at all, nothing can execute the .py file, so this
#   script is the last-resort bootstrap. It is intentionally kept small: download +
#   extract + minimal venv layout. The heavy lifting (wheel install, verification,
#   marker file) is then handed back to python_bootstrap.py --finalize.
#
# ASCII-ONLY ON PURPOSE: Windows PowerShell 5.1 reads .ps1 as ANSI unless the file
# has a BOM, so any non-ASCII text here would come out garbled on a GBK console.
#
# USAGE (normally called by tools\get_python.bat)
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\get_python.ps1 -Root "D:\app"
#   ... -Version 3.13.9       pin an exact version
#   ... -BaseUrl http://intranet/python    custom source (file:// and UNC also OK)

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$Version = "",
    [string]$BaseUrl = "",
    [switch]$Offline
)

$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
# Invoke-WebRequest draws a progress bar that floods a redirected console (one line
# per 10 KB on a 11 MB download). Silence it.
$ProgressPreference = "SilentlyContinue"

$Root      = (Resolve-Path -LiteralPath $Root).Path
$DistDir   = Join-Path $Root "vendor\python"
$VenvDir   = Join-Path $Root ".venv"
$ScriptsD  = Join-Path $VenvDir "Scripts"
$MinBytes  = 5MB
$UserAgent = "qq-todo-python-bootstrap/1.0 (powershell provisioner)"
$Mirrors   = @(
    "https://www.python.org/ftp/python/{0}/python-{0}-embed-amd64.zip",
    "https://registry.npmmirror.com/-/binary/python/{0}/python-{0}-embed-amd64.zip",
    "https://mirrors.huaweicloud.com/python/{0}/python-{0}-embed-amd64.zip",
    "https://mirrors.aliyun.com/python-release/windows/python-{0}-embed-amd64.zip"
)
$IndexUrl  = "https://registry.npmmirror.com/-/binary/python/"

function Write-Step([string]$msg) { Write-Host "  $msg" }

function Get-LocalZip {
    if (-not (Test-Path -LiteralPath $DistDir)) { return $null }
    $cand = Get-ChildItem -LiteralPath $DistDir -Filter "python-3.*-embed-amd64.zip" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending
    if (-not $cand) { return $null }
    if ($Version -ne "") {
        $want = "python-$Version-embed-amd64.zip"
        foreach ($c in $cand) { if ($c.Name -eq $want) { return $c } }
        return $null
    }
    return $cand[0]
}

function Get-RemoteVersion {
    # Enumerate version dirs, keep 3.10-3.13, then confirm the embed asset really
    # exists -- some security-only releases ship source only (e.g. 3.13.18).
    Write-Step "Querying available versions ..."
    $names = @()
    foreach ($idx in @($IndexUrl, "https://www.python.org/ftp/python/")) {
        try {
            $resp = Invoke-WebRequest -Uri $idx -UseBasicParsing -TimeoutSec 25 -UserAgent $UserAgent
            $names = [regex]::Matches($resp.Content, '3\.1[0-3]\.\d+') |
                     ForEach-Object { $_.Value } | Sort-Object -Unique
            if ($names.Count -gt 0) { break }
        } catch { }
    }
    if ($names.Count -eq 0) { return "" }

    $sorted = $names | ForEach-Object {
        $p = $_ -split '\.'
        [pscustomobject]@{ Text = $_; Key = ([int]$p[0]) * 1000000 + ([int]$p[1]) * 1000 + ([int]$p[2]) }
    } | Sort-Object Key -Descending

    $probes = 0
    foreach ($v in $sorted) {
        if ($probes -ge 6) { break }
        $probes++
        try {
            $list = Invoke-WebRequest -Uri "$IndexUrl$($v.Text)/" -UseBasicParsing -TimeoutSec 25 -UserAgent $UserAgent
            if ($list.Content -match "python-$([regex]::Escape($v.Text))-embed-amd64\.zip") {
                return $v.Text
            }
            Write-Step "  $($v.Text) has no embed-amd64 build, skipped"
        } catch { }
    }
    return ""
}

function Save-Zip([string]$ver) {
    if (-not (Test-Path -LiteralPath $DistDir)) { New-Item -ItemType Directory -Force -Path $DistDir | Out-Null }
    $dest = Join-Path $DistDir "python-$ver-embed-amd64.zip"

    $urls = @()
    if ($BaseUrl -ne "") { $urls += ($BaseUrl.TrimEnd('/') + "/python-$ver-embed-amd64.zip") }
    foreach ($m in $Mirrors) { $urls += ($m -f $ver) }

    foreach ($u in $urls) {
        Write-Step "Downloading $u"
        try {
            if ($u -like "file://*" -or $u -like "\\*") {
                $src = $u -replace '^file:///', ''
                Copy-Item -LiteralPath $src -Destination $dest -Force
            } else {
                Invoke-WebRequest -Uri $u -OutFile $dest -UseBasicParsing -TimeoutSec 180 -UserAgent $UserAgent
            }
            $len = (Get-Item -LiteralPath $dest).Length
            if ($len -lt $MinBytes) {
                Write-Step "  [FAIL] only $len bytes (looks like an error page)"
                Remove-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue
                continue
            }
            return $dest
        } catch {
            Write-Step "  [FAIL] $($_.Exception.Message)"
        }
    }
    return $null
}

function Expand-Minimal([string]$zipPath) {
    # Lay out a *minimal* runtime: everything from the embed zip goes into
    # .venv\Scripts so python.exe / pythonXY.dll / *.pyd / pythonXY.zip stay together.
    if (Test-Path -LiteralPath $VenvDir) { Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path $ScriptsD | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $VenvDir "Lib\site-packages") | Out-Null

    $tmp = Join-Path $Root ("work\.ps-extract-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        Expand-Archive -LiteralPath $zipPath -DestinationPath $tmp -Force
        if (-not (Test-Path -LiteralPath (Join-Path $tmp "python.exe"))) {
            throw "the zip has no python.exe, refusing to use it"
        }
        Get-ChildItem -LiteralPath $tmp | Move-Item -Destination $ScriptsD -Force
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    # ._pth pins sys.path; while it exists site-packages is never on the path
    Get-ChildItem -LiteralPath $ScriptsD -Filter "python3*._pth" -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue

    $verOut = ""
    try {
        $verOut = (& (Join-Path $ScriptsD "python.exe") -c "import sys;print(sys.version.split()[0])" 2>$null | Select-Object -Last 1)
    } catch { }
    if (-not $verOut) { throw "python.exe cannot start after extraction" }

    # IMPORTANT: must be UTF-8 **without BOM**.
    #   - Set-Content -Encoding ASCII would turn a Chinese install path into "?????"
    #     and CPython's getpath then dies with "OSError: [Errno 22] Invalid argument".
    #   - PowerShell 5.1's -Encoding UTF8 prepends a BOM, which corrupts the first
    #     "home = ..." key just the same way.
    $cfg = @(
        "home = $ScriptsD",
        "include-system-site-packages = false",
        "version = $verOut",
        "executable = $(Join-Path $ScriptsD 'python.exe')",
        "command = qq-todo portable runtime (embeddable, provisioned by get_python.ps1)"
    ) -join "`r`n"
    $cfgPath = Join-Path $VenvDir "pyvenv.cfg"
    [System.IO.File]::WriteAllText($cfgPath, $cfg, (New-Object System.Text.UTF8Encoding($false)))

    # Prove that it still starts with pyvenv.cfg in place (this is what catches a
    # mangled "home" path immediately instead of 3 steps later).
    $recheck = ""
    try {
        $recheck = (& (Join-Path $ScriptsD "python.exe") -c "import sys;print(sys.prefix)" 2>$null | Select-Object -Last 1)
    } catch { }
    if (-not $recheck) {
        throw "python.exe cannot start after writing pyvenv.cfg -- check that the install path has no unencodable characters"
    }

    Write-Step "[OK] minimal runtime ready: Python $verOut"
    return $verOut
}

# ---------------------------------------------------------------- main
Write-Host "=============================================================="
Write-Host "  Portable Python provisioner (no Python on this machine)"
Write-Host "=============================================================="

$zip = Get-LocalZip
if ($zip) {
    Write-Step "[OK] using bundled asset: $($zip.Name) (no network needed)"
} else {
    if ($Offline) {
        Write-Step "[FAIL] -Offline was requested but no zip found under vendor\python"
        exit 2
    }
    $ver = $Version
    if ($ver -eq "") { $ver = Get-RemoteVersion }
    if ($ver -eq "") {
        Write-Step "[FAIL] cannot determine a downloadable version (network blocked?)"
        Write-Step "       Workaround: on a machine with internet run"
        Write-Step "         python tools\python_bootstrap.py --fetch --pack"
        Write-Step "       then copy the zip into vendor\python\ and run this again."
        exit 3
    }
    Write-Step "Target version: Python $ver embeddable (win-amd64)"
    $got = Save-Zip $ver
    if (-not $got) {
        Write-Step "[FAIL] every download source failed"
        exit 4
    }
    $zip = Get-Item -LiteralPath $got
    Write-Step "[OK] downloaded: $($zip.FullName)"
}

try {
    $null = Expand-Minimal $zip.FullName
} catch {
    Write-Step "[FAIL] $($_.Exception.Message)"
    exit 5
}

# Hand over to the Python side: it installs wheels, writes the marker, verifies.
$py = Join-Path $ScriptsD "python.exe"
$boot = Join-Path $Root "tools\python_bootstrap.py"
if (Test-Path -LiteralPath $boot) {
    Write-Step "Finalizing (installing dependencies offline) ..."
    & $py $boot --finalize
    if ($LASTEXITCODE -ne 0) {
        Write-Step "[FAIL] python_bootstrap --finalize returned $LASTEXITCODE"
        exit 6
    }
}
exit 0
