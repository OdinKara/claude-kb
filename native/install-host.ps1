# Registers the Claude KB capture native messaging host for Chrome and Edge
# (current user only). Generates host.cmd and the host manifest from this
# machine's values, then writes the HKCU registry entries.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-host.ps1 `
#       -ExtensionId <id shown on the extension's card> `
#       -ScriptsDir "C:\path\to\claude-kb"
#
# ExtensionId and ScriptsDir are the only per-machine inputs. The interpreter is
# taken from config.json in ScriptsDir when present, otherwise from PATH; pass
# -Python to override. Everything else the host needs is resolved at run time by
# kb_config.
#
# NOTE: this file is deliberately pure ASCII. Windows PowerShell 5.1 reads a
# BOM-less UTF-8 script as Windows-1252, so a stray non-ASCII character here
# turns into mojibake and can break parsing.
param(
  [Parameter(Mandatory = $true)]
  [string]$ExtensionId,

  [Parameter(Mandatory = $true)]
  [string]$ScriptsDir,

  [string]$Python,

  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$name = "com.claude_kb.export"
$manifestPath = Join-Path $here "$name.json"
$hostCmd = Join-Path $here "host.cmd"
$template = Join-Path $here "host.cmd.example"

$keys = @(
  "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$name",
  "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$name"
)

if ($Uninstall) {
  foreach ($key in $keys) {
    if (Test-Path $key) {
      Remove-Item -Path $key -Force -Recurse
      Write-Host "[OK] removed $key"
    }
  }
  Write-Host "[OK] Native host unregistered. host.cmd and the manifest were left in place."
  exit 0
}

Write-Host "==== Claude KB native host ===="

# ---- validate the extension id -------------------------------------------
# Chrome extension ids are 32 characters from a-p. Catching a bad one here is
# far cheaper than a silent "host disconnected" later.
if ($ExtensionId -notmatch '^[a-p]{32}$') {
  throw "ExtensionId does not look like a Chrome/Edge extension id (expected 32 chars a-p): $ExtensionId"
}

# ---- validate the scripts directory --------------------------------------
if (-not (Test-Path $ScriptsDir)) {
  throw "ScriptsDir not found: $ScriptsDir"
}
$ScriptsDirAbs = (Resolve-Path $ScriptsDir).Path
foreach ($required in @("claude_kb.py", "kb_config.py", "kb_ingest.py")) {
  if (-not (Test-Path (Join-Path $ScriptsDirAbs $required))) {
    throw "ScriptsDir does not contain ${required}: $ScriptsDirAbs"
  }
}

# ---- resolve the interpreter ---------------------------------------------
if (-not $Python) {
  $cfg = Join-Path $ScriptsDirAbs "config.json"
  if (Test-Path $cfg) {
    try {
      $Python = (Get-Content $cfg -Raw | ConvertFrom-Json).python
    } catch {
      $Python = $null
    }
  }
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

# ---- generate host.cmd ----------------------------------------------------
if (-not (Test-Path $template)) {
  throw "host.cmd.example not found: $template"
}
$utf8 = New-Object System.Text.UTF8Encoding $false
$cmdText = [System.IO.File]::ReadAllText($template)
$cmdText = $cmdText.Replace("{{PYTHON}}", $PythonAbs).Replace("{{SCRIPTS}}", $ScriptsDirAbs)
[System.IO.File]::WriteAllText($hostCmd, $cmdText, $utf8)

# ---- generate the host manifest ------------------------------------------
$origin = "chrome-extension://$ExtensionId/"
$pathJson = $hostCmd.Replace("\", "\\")
$json = "{`"name`":`"$name`",`"description`":`"Claude KB capture native host`",`"path`":`"$pathJson`",`"type`":`"stdio`",`"allowed_origins`":[`"$origin`"]}"
[System.IO.File]::WriteAllText($manifestPath, $json, $utf8)

# ---- register -------------------------------------------------------------
foreach ($key in $keys) {
  New-Item -Path $key -Force | Out-Null
  Set-ItemProperty -Path $key -Name "(default)" -Value $manifestPath
}

Write-Host "[OK] Native host registered"
Write-Host "     host name  $name"
Write-Host "     origin     $origin"
Write-Host "     manifest   $manifestPath"
Write-Host "     host.cmd   $hostCmd"
Write-Host "     python     $PythonAbs"
Write-Host "     scripts    $ScriptsDirAbs"
Write-Host ""
Write-Host "Reload the extension. If it reports the host as disconnected, run"
Write-Host "host.cmd directly in a terminal - it should sit waiting for input"
Write-Host "rather than exiting with an error."
