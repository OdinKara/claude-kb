# Creates the scheduled task that runs kb_ingest.py unattended.
#
# The task is what kb_open.py triggers at the end of a collection run, and what
# picks up anything left in incoming/ overnight. Without it, a fresh install
# gets all the way through downloading the export - spending its one-time URLs -
# and then has nothing to hand the parts to.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-task.ps1 `
#       -ScriptsDir "C:\path\to\claude-kb" -Time 06:00
#
#   ... -Uninstall     remove the task
#
# Everything is resolved the same way the Python side resolves it: the task name
# from "task" in config.json, the interpreter from "python", both falling back
# to the same defaults kb_config uses. Passing them explicitly overrides.
#
# WINDOWS ONLY. On macOS or Linux, use cron or a systemd timer to run
# kb_ingest.py on the same schedule; the pipeline does not care what starts it.
#
# NOTE: pure ASCII on purpose. Windows PowerShell 5.1 reads a BOM-less UTF-8
# script as Windows-1252, so a stray non-ASCII character can break parsing.
param(
  [string]$ScriptsDir,
  [string]$Python,
  [string]$TaskName,
  [string]$Time = "06:00",
  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# Run schtasks and get back (exit code, text) WITHOUT letting its stderr into
# PowerShell's error stream.
#
# Windows PowerShell 5.1 turns `nativeexe 2>&1` into NativeCommandError
# ErrorRecords, one per stderr line, and with $ErrorActionPreference = "Stop"
# that terminates the script even when the executable exited 0. schtasks writes
# to stderr routinely - "cannot find the file specified" for a task that does
# not exist is an ordinary answer here, not a failure. Letting cmd.exe perform
# the redirection keeps all of it as plain text.
function Invoke-Schtasks {
  param([string[]]$Arguments)
  $quoted = $Arguments | ForEach-Object {
    if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
  }
  $line = "schtasks " + ($quoted -join " ") + " 2>&1"
  $text = cmd /c $line
  return @{ Code = $LASTEXITCODE; Text = ($text -join "`n") }
}

if (-not $ScriptsDir) {
  $ScriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not (Test-Path $ScriptsDir)) {
  throw "ScriptsDir not found: $ScriptsDir"
}
$ScriptsDirAbs = (Resolve-Path $ScriptsDir).Path

$ingest = Join-Path $ScriptsDirAbs "kb_ingest.py"
if (-not (Test-Path $ingest)) {
  throw "kb_ingest.py not found in ${ScriptsDirAbs} - is this the right directory?"
}

# ---- resolve the task name and interpreter the way kb_config does ---------
$cfg = Join-Path $ScriptsDirAbs "config.json"
$cfgObj = $null
if (Test-Path $cfg) {
  try { $cfgObj = Get-Content $cfg -Raw | ConvertFrom-Json } catch { $cfgObj = $null }
}
if (-not $TaskName) {
  if ($env:CLAUDE_KB_TASK) { $TaskName = $env:CLAUDE_KB_TASK }
  elseif ($cfgObj -and $cfgObj.task) { $TaskName = $cfgObj.task }
  else { $TaskName = "ClaudeKB-Ingest" }
}

if ($Uninstall) {
  $q = Invoke-Schtasks @("/query", "/tn", $TaskName)
  if ($q.Code -ne 0) {
    Write-Host "[--] No task named '$TaskName' to remove."
    exit 0
  }
  $null = Invoke-Schtasks @("/delete", "/tn", $TaskName, "/f")
  Write-Host "[OK] Removed the scheduled task '$TaskName'."
  exit 0
}

Write-Host "==== Claude KB ingest task ===="

if (-not $Python) {
  if ($env:CLAUDE_KB_PYTHON) { $Python = $env:CLAUDE_KB_PYTHON }
  elseif ($cfgObj -and $cfgObj.python) { $Python = $cfgObj.python }
}
if (-not $Python) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { $Python = $cmd.Source }
}
if (-not $Python) {
  throw "Could not determine a Python interpreter. Pass -Python with the full path."
}
if (-not (Test-Path $Python)) {
  throw "Python interpreter not found: $Python"
}
$PythonAbs = (Resolve-Path $Python).Path

if ($Time -notmatch '^\d{2}:\d{2}$') {
  throw "Time must look like HH:MM (24 hour), got: $Time"
}

# ---- create ---------------------------------------------------------------
# Both paths are quoted inside the /tr value because either can contain spaces,
# and getting that quoting wrong is the classic way this task ends up created
# but broken. Building it here rather than asking anyone to transcribe it.
$run = '"{0}" "{1}"' -f $PythonAbs, $ingest

$existing = Invoke-Schtasks @("/query", "/tn", $TaskName)
$replacing = ($existing.Code -eq 0)
if ($replacing) {
  Write-Host "[--] A task named '$TaskName' already exists; replacing it."
}

$created = Invoke-Schtasks @("/create", "/tn", $TaskName, "/tr", $run,
                             "/sc", "daily", "/st", $Time, "/f")
if ($created.Code -ne 0) {
  Write-Host $created.Text
  throw "schtasks refused to create the task."
}

# ---- verify, rather than trust the exit code -----------------------------
$verify = Invoke-Schtasks @("/query", "/tn", $TaskName, "/fo", "list", "/v")
if ($verify.Code -ne 0) {
  throw ("The task was reported as created but cannot be queried back: " + $verify.Text)
}
if ($verify.Text -notmatch "kb_ingest\.py") {
  throw ("The task exists but does not appear to run kb_ingest.py: " + $verify.Text)
}
Write-Host ("[OK] Scheduled task '{0}' created" -f $TaskName)
Write-Host ("     runs      {0}" -f $run)
Write-Host ("     schedule  daily at {0}" -f $Time)
Write-Host ""
Write-Host "Run it once now to confirm it works:"
Write-Host ("    schtasks /run /tn `"{0}`"" -f $TaskName)
Write-Host ("Then check the last line of {0}\ingest.log" -f $ScriptsDirAbs)
Write-Host "With an empty incoming/ it should say 'no new export' - that is a"
Write-Host "clean result, not a failure."
