# OPTIONAL. Creates a scheduled task that runs kb_ingest.py unattended.
#
# Nothing requires this. The normal workflow is running kb_open.py when you want
# an update and finishing with kb_ingest.py; this task only removes that second
# command, and picks up anything left in incoming/ overnight. kb_open.py handles
# a missing task by naming the command that finishes the job, so skipping this is
# a supported setup rather than an incomplete one.
#
# Note what it creates: schtasks without stored credentials produces a task with
# Logon Mode "Interactive only", which runs ONLY while its own account is logged
# on interactively. A daily ingest will not happen on a machine you log out of.
# See the check at the end, and `schtasks /change /ru /rp` if you need better.
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

# Principals that run without anyone being logged on. If the task runs as one of
# these, an interactive session is irrelevant.
$script:ServicePrincipals = @("system", "localsystem", "local service",
                              "network service")

function Get-AccountLeaf {
  # "HOST\alice", "alice" and "alice@example.com" are the same account here.
  # schtasks reports the bare name while the interactive session reports a
  # qualified one, so comparing them raw produces a false failure on a machine
  # that is working perfectly.
  param([string]$Account)
  if (-not $Account) { return "" }
  $a = $Account.Trim()
  if ($a.Contains("\")) { $a = $a.Substring($a.LastIndexOf("\") + 1) }
  if ($a.Contains("@")) { $a = $a.Substring(0, $a.IndexOf("@")) }
  return $a.ToLowerInvariant()
}

function Test-TaskCanFire {
  <#
    Can this task actually run, or is it registered and inert?

    schtasks creates a task with Logon Mode "Interactive only" unless it is
    given stored credentials. Such a task runs ONLY while its run-as account is
    logged on interactively. Register as an account that is not the interactive
    user and the task is created cleanly, verifies cleanly, and then never
    fires - a silent stall, which is the failure mode this project keeps
    finding.

    Returns @{ Status = "ok" | "warn" | "fail"; Message = "..." }.
  #>
  param(
    [string]$LogonMode,
    [string]$RunAsUser,
    [string]$InteractiveUser
  )

  $runLeaf = Get-AccountLeaf $RunAsUser
  $intLeaf = Get-AccountLeaf $InteractiveUser

  if ($runLeaf -and ($script:ServicePrincipals -contains $runLeaf)) {
    return @{ Status = "ok"
              Message = "runs as the service principal '$RunAsUser', which does not need an interactive session." }
  }

  if (-not $LogonMode) {
    return @{ Status = "warn"
              Message = "could not read the task's Logon Mode, so whether it can fire is unverified." }
  }

  # Anything other than "Interactive only" - normally "Interactive/Background" -
  # runs whether or not anyone is logged on.
  if ($LogonMode -notmatch "Interactive only") {
    return @{ Status = "ok"
              Message = "Logon Mode is '$LogonMode', so it runs whether or not anyone is logged on." }
  }

  if (-not $intLeaf) {
    return @{ Status = "warn"
              Message = ("Logon Mode is 'Interactive only' and the interactive user could not be " +
                         "determined, so whether this task can fire is UNVERIFIED. It will run only " +
                         "while '$RunAsUser' is logged on interactively.") }
  }

  if ($runLeaf -ne $intLeaf) {
    return @{ Status = "fail"
              Message = ("the task is registered to run as '$RunAsUser' with Logon Mode " +
                         "'Interactive only', but the interactive user is '$InteractiveUser'. " +
                         "It will NEVER FIRE: an interactive-only task runs only while its own " +
                         "account is logged on interactively.") }
  }

  return @{ Status = "warn"
            Message = ("Logon Mode is 'Interactive only', so the task runs only while " +
                       "'$RunAsUser' is logged on. It will NOT run at its scheduled time if " +
                       "that account is logged off.") }
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

# The log lives under the KB ROOT, which is a different directory from the
# scripts - the recommended layout keeps the working directory outside the repo
# precisely so a stray `git add .` cannot publish the database. Reporting
# <scripts>/ingest.log would send someone to a path that does not exist.
$KbRoot = $null
if ($env:CLAUDE_KB_ROOT) { $KbRoot = $env:CLAUDE_KB_ROOT }
elseif ($cfgObj -and $cfgObj.root) { $KbRoot = $cfgObj.root }
if ($KbRoot) {
  $LogPath = Join-Path $KbRoot "ingest.log"
} else {
  $LogPath = "<root>\ingest.log  (root is not configured yet - see config.json)"
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

# ---- can it actually fire? -----------------------------------------------
# Verifying the command line proves the task would do the right thing IF it ran.
# It says nothing about whether it CAN run. Those are different questions and
# only one of them was being asked.
function Get-TaskField {
  param([string]$Text, [string]$Field)
  foreach ($line in ($Text -split "`n")) {
    if ($line -match ("^\s*" + [regex]::Escape($Field) + "\s*:\s*(.+?)\s*$")) {
      return $Matches[1]
    }
  }
  return ""
}

$logonMode = Get-TaskField $verify.Text "Logon Mode"
$runAsUser = Get-TaskField $verify.Text "Run As User"
$interactive = ""
try {
  $interactive = (Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).UserName
} catch {
  $interactive = ""
}

Write-Host ("     runs as   {0}" -f $(if ($runAsUser) { $runAsUser } else { "(not reported)" }))
Write-Host ("     logon     {0}" -f $(if ($logonMode) { $logonMode } else { "(not reported)" }))

$fire = Test-TaskCanFire -LogonMode $logonMode -RunAsUser $runAsUser -InteractiveUser $interactive
Write-Host ""
switch ($fire.Status) {
  "ok" {
    Write-Host ("[OK] The task can fire: {0}" -f $fire.Message)
  }
  "warn" {
    Write-Host ("[!] {0}" -f $fire.Message)
  }
  "fail" {
    Write-Host ("[X] THE TASK CANNOT FIRE - {0}" -f $fire.Message)
    Write-Host ""
    Write-Host "    The task exists, so nothing is half-created, but it will not run"
    Write-Host "    on its schedule and kb_open.py will not be able to trigger it."
    Write-Host ""
    Write-Host "    Fix it either way:"
    Write-Host "      - re-run this script while logged in as the account that should"
    Write-Host "        run the task, or"
    Write-Host "      - give the task stored credentials so it does not need an"
    Write-Host "        interactive session:"
    Write-Host ("          schtasks /change /tn `"{0}`" /ru <user> /rp <password>" -f $TaskName)
    throw "Registered a task that cannot fire - see above."
  }
}

Write-Host ""
Write-Host "Run it once now to confirm it works:"
Write-Host ("    schtasks /run /tn `"{0}`"" -f $TaskName)
Write-Host ("Then check the last line of {0}" -f $LogPath)
Write-Host "With an empty incoming/ it should say 'no new export' - that is a"
Write-Host "clean result, not a failure."
