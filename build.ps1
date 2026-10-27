# SPDX-License-Identifier: 0BSD

<#
.SYNOPSIS
    Thin PowerShell wrapper around build.py (Mir Tankov mod packager).

.DESCRIPTION
    Finds a Python 3.10+ interpreter and forwards every switch to build.py.
    By default it only builds dist\<id>_<version>.mtmod and writes NOTHING
    into the game folder.

    Python is looked up in this order: -Python, $env:WOT_PYTHON, the 'py -3'
    launcher, then 'python' and 'python3' from PATH. The Microsoft Store
    placeholder (...\WindowsApps\python.exe) and Python 2 are skipped.

    All the real logic lives in build.py -- keep it that way, so the command
    line and PowerShell entry points can never drift apart.

.EXAMPLE
    .\build.ps1
    Build the package.

.EXAMPLE
    .\build.ps1 -List
    Build (mod_*.pyc comes from tools\py27c.py -- no Python 2.7 needed) and
    print the archive listing.

.EXAMPLE
    .\build.ps1 -Compiler py27 -Py2 C:\Python27\python.exe
    Compile the entry point with a real Python 2.7 interpreter instead.

.EXAMPLE
    .\build.ps1 -Install
    Build, then copy the .mtmod into <game>\mods\<version>\.

.EXAMPLE
    .\build.ps1 -Install -GameDir C:\Games\Tanki
    The same with an explicit game folder (the one with paths.xml).

.EXAMPLE
    .\build.ps1 -Dev
    Deploy loose files into <game>\res_mods\<version>\ (fast iteration, no archive).

.EXAMPLE
    .\build.ps1 -Uninstall
    Remove whatever -Install / -Dev put into the game folder.

.EXAMPLE
    .\build.ps1 -List --no-dir-entries
    Any other build.py option is passed through as is (see python build.py --help).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1 -List
    The same when Windows refuses to run scripts ("running scripts is disabled
    on this system").
#>
# PositionalBinding = $false: a bare build.py flag such as --no-syntax-check
# must land in $Rest, not in -Compiler or another parameter by position.
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Install,
    [switch]$Dev,
    [switch]$Uninstall,
    [switch]$Clean,
    [switch]$List,
    [switch]$KeepPy,
    [switch]$NoPy2,
    [switch]$CompileAll,
    [switch]$NoSyntaxCheck,
    [switch]$NoDirEntries,
    [ValidateSet('auto', 'builtin', 'py27')]
    [string]$Compiler,
    [string]$Py2,
    [string]$Src,
    [string]$Dist,
    [string]$Name,
    [string]$GameDir,
    [string]$GameVersion,
    [string]$Python,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$buildPy = Join-Path $scriptDir 'build.py'

if (-not (Test-Path $buildPy)) {
    Write-Error "build.py not found next to build.ps1 (looked for $buildPy)"
    exit 1
}

# --- locate python 3.10+ ---------------------------------------------------
function Test-Python3 {
    param([string[]]$Command)
    # Windows PowerShell 5.1 turns native stderr into errors; keep the probe quiet.
    $ErrorActionPreference = 'Continue'
    $exe = $Command[0]
    $prefix = @()
    if ($Command.Count -gt 1) { $prefix = $Command[1..($Command.Count - 1)] }
    try {
        & $exe @prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$tried = New-Object System.Collections.Generic.List[string]
$pythonCmd = $null

$explicit = $Python
if (-not $explicit -and $env:WOT_PYTHON) { $explicit = $env:WOT_PYTHON }
if ($explicit) {
    if (Test-Python3 -Command @($explicit)) {
        $pythonCmd = @($explicit)
    } else {
        Write-Error "'$explicit' (-Python / WOT_PYTHON) is not a working Python 3.10+."
        exit 1
    }
}

if ($null -eq $pythonCmd) {
    $candidates = New-Object System.Collections.Generic.List[object]
    $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $launcher) { $candidates.Add(@($launcher.Source, '-3')) }
    foreach ($nameOnPath in @('python', 'python3')) {
        foreach ($cmd in @(Get-Command $nameOnPath -CommandType Application -All -ErrorAction SilentlyContinue)) {
            # The Microsoft Store placeholder only prints "Python was not found".
            if ($cmd.Source -like '*\WindowsApps\*') { $tried.Add("$($cmd.Source) (Microsoft Store placeholder, skipped)"); continue }
            $candidates.Add(@($cmd.Source))
        }
    }
    foreach ($cand in $candidates) {
        $label = $cand -join ' '
        if (Test-Python3 -Command $cand) { $pythonCmd = $cand; break }
        $tried.Add("$label (not Python 3.10+)")
    }
}

if ($null -eq $pythonCmd) {
    $msg = 'Python 3.10+ required, none found. Install it from python.org (the "py" launcher is enough), or pass -Python <path to python.exe>.'
    if ($tried.Count -gt 0) { $msg += ' Tried: ' + ($tried -join '; ') }
    Write-Error $msg
    exit 1
}

# --- forward the arguments -------------------------------------------------
$argv = New-Object System.Collections.Generic.List[string]
$argv.Add($buildPy)

if ($Install)   { $argv.Add('--install') }
if ($Dev)       { $argv.Add('--dev') }
if ($Uninstall) { $argv.Add('--uninstall') }
if ($Clean)     { $argv.Add('--clean') }
if ($List)      { $argv.Add('--list') }
if ($KeepPy)    { $argv.Add('--keep-py') }
if ($NoPy2)      { $argv.Add('--no-py2') }
if ($CompileAll) { $argv.Add('--compile-all') }
if ($NoSyntaxCheck) { $argv.Add('--no-syntax-check') }
if ($NoDirEntries)  { $argv.Add('--no-dir-entries') }

if ($Compiler)    { $argv.Add('--compiler');     $argv.Add($Compiler) }
if ($Py2)         { $argv.Add('--py2');          $argv.Add($Py2) }
if ($Src)         { $argv.Add('--src');          $argv.Add($Src) }
if ($Dist)        { $argv.Add('--dist');         $argv.Add($Dist) }
if ($Name)        { $argv.Add('--name');         $argv.Add($Name) }
if ($GameDir)     { $argv.Add('--game-dir');     $argv.Add($GameDir) }
if ($GameVersion) { $argv.Add('--game-version'); $argv.Add($GameVersion) }

if ($Rest) { foreach ($item in $Rest) { $argv.Add($item) } }

$exe = $pythonCmd[0]
$prefix = @()
if ($pythonCmd.Count -gt 1) { $prefix = $pythonCmd[1..($pythonCmd.Count - 1)] }

& $exe @prefix @argv
exit $LASTEXITCODE
