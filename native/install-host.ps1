# Registers the Claude KB capture native messaging host for the Chromium
# browsers found on this machine (current user only). Generates host.cmd and the
# host manifest from this machine's values, then writes the HKCU registry
# entries for each browser actually installed.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-host.ps1 `
#       -ExtensionId <id shown on the extension's card> `
#       -ScriptsDir "C:\path\to\claude-kb"
#
#   ... -Uninstall      remove the registry entries from every known browser
#   ... -All            register for every known browser, installed or not
#
# ExtensionId and ScriptsDir are the only per-machine inputs. The interpreter is
# taken from config.json in ScriptsDir when present, otherwise from PATH; pass
# -Python to override. Everything else the host needs is resolved at run time by
# kb_config.
#
# WINDOWS ONLY. host.cmd is a batch file and this script is PowerShell; macOS
# and Linux would need their own launcher and their own registration (a JSON
# file in a per-browser directory rather than the registry).
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

  [switch]$All,

  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$name = "com.claude_kb.export"
$manifestPath = Join-Path $here "$name.json"
$hostCmd = Join-Path $here "host.cmd"
$template = Join-Path $here "host.cmd.example"

# Every Chromium browser uses the SAME native-host manifest format; only the
# registry path differs. Firefox does not - see below.
$browsers = @(
  @{ Name = "Chrome";  Key = "HKCU:\Software\Google\Chrome\NativeMessagingHosts";                  Exe = "chrome.exe" },
  @{ Name = "Edge";    Key = "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts";                 Exe = "msedge.exe" },
  @{ Name = "Brave";   Key = "HKCU:\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts";    Exe = "brave.exe" },
  @{ Name = "Vivaldi"; Key = "HKCU:\Software\Vivaldi\NativeMessagingHosts";                        Exe = "vivaldi.exe" },
  @{ Name = "Opera";   Key = "HKCU:\Software\Opera Software\Opera Stable\NativeMessagingHosts";    Exe = "opera.exe" }
)

function Test-BrowserInstalled([string]$exe) {
  # App Paths is the registration Windows itself uses to resolve a bare exe
  # name, so it is a better signal than guessing install directories.
  foreach ($root in @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
                      "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")) {
    $p = Join-Path $root $exe
    if (Test-Path $p) {
      $v = (Get-ItemProperty -Path $p -ErrorAction SilentlyContinue)."(default)"
      if ($v -and (Test-Path $v)) { return $v }
      return "registered"
    }
  }
  # Fall back to the usual locations: a portable or per-user install may have
  # no App Paths entry.
  $bases = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)
  $subs = @(
    "Google\Chrome\Application", "Microsoft\Edge\Application",
    "BraveSoftware\Brave-Browser\Application", "Vivaldi\Application",
    "Programs\Opera", "Opera"
  )
  foreach ($b in $bases) {
    if (-not $b) { continue }
    foreach ($s in $subs) {
      $cand = Join-Path (Join-Path $b $s) $exe
      if (Test-Path $cand) { return $cand }
    }
  }
  return $null
}

function Test-FirefoxInstalled() {
  if (Test-Path "HKCU:\Software\Mozilla\Mozilla Firefox") { return $true }
  if (Test-Path "HKLM:\Software\Mozilla\Mozilla Firefox") { return $true }
  foreach ($b in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($b -and (Test-Path (Join-Path $b "Mozilla Firefox\firefox.exe"))) { return $true }
  }
  return $false
}

if ($Uninstall) {
  $removed = 0
  foreach ($b in $browsers) {
    $key = Join-Path $b.Key $name
    if (Test-Path $key) {
      Remove-Item -Path $key -Force -Recurse
      Write-Host ("[OK] removed from {0}" -f $b.Name)
      $removed++
    }
  }
  if ($removed -eq 0) { Write-Host "[--] nothing was registered" }
  Write-Host "[OK] Done. host.cmd and the manifest were left in place."
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

# ---- register with each browser that is actually here ---------------------
$registered = @()
$skipped = @()
foreach ($b in $browsers) {
  $found = Test-BrowserInstalled $b.Exe
  if (-not $found -and -not $All) {
    $skipped += $b.Name
    continue
  }
  $key = Join-Path $b.Key $name
  New-Item -Path $key -Force | Out-Null
  Set-ItemProperty -Path $key -Name "(default)" -Value $manifestPath
  $registered += $b.Name
}

if ($registered.Count -eq 0) {
  Write-Host "[X] No supported browser was detected, so nothing was registered."
  Write-Host "    Re-run with -All to register anyway (harmless if unused),"
  Write-Host "    or check that your browser is one of: $($browsers.Name -join ', ')"
} else {
  Write-Host ("[OK] Registered for: {0}" -f ($registered -join ", "))
}
if ($skipped.Count -gt 0) {
  Write-Host ("[--] Skipped, not installed: {0}" -f ($skipped -join ", "))
}

Write-Host ""
Write-Host "     host name  $name"
Write-Host "     origin     $origin"
Write-Host "     manifest   $manifestPath"
Write-Host "     host.cmd   $hostCmd"
Write-Host "     python     $PythonAbs"
Write-Host "     scripts    $ScriptsDirAbs"

# ---- browsers that are not supported, said plainly -----------------------
if (Test-FirefoxInstalled) {
  Write-Host ""
  Write-Host "[!] Firefox is installed and is NOT supported."
  Write-Host "    It uses a different registry path AND a different manifest schema"
  Write-Host "    (allowed_extensions with an add-on id, not allowed_origins with a"
  Write-Host "    chrome-extension:// URL), and this extension declares no Firefox id."
  Write-Host "    Nothing was written for it. Use a Chromium browser."
}

Write-Host ""
Write-Host "IMPORTANT: the extension is loaded unpacked, so its ID is derived from"
Write-Host "the folder it was loaded from. If you MOVE or RENAME that folder, the"
Write-Host "ID changes, this registration stops matching, and the popup will report"
Write-Host "the host as unavailable. Keep the extension at a stable path - and if"
Write-Host "you do move it, re-run this script with the new ID."
Write-Host ""
Write-Host "Reload the extension. If it reports the host as disconnected, run"
Write-Host "host.cmd directly in a terminal - it should sit waiting for input"
Write-Host "rather than exiting with an error."
