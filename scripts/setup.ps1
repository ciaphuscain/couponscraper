<#
.SYNOPSIS
  One-command CouponHunter setup for Windows. Safe to re-run; never deletes data.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -AutoStart

.PARAMETER AutoStart
  Also register a Task Scheduler entry that starts CouponHunter at logon (no admin needed).
.PARAMETER Demo
  Also load clearly-labelled SYNTHETIC demo data.
#>
[CmdletBinding()]
param([switch]$AutoStart, [switch]$Demo)

# Native tools (pip, py) may write warnings to stderr; under "Stop" Windows PowerShell 5.1 can turn
# those into terminating errors. Use "Continue" and check $LASTEXITCODE explicitly instead.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)   # repository root
function Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }

# 1. Find a real Python 3.11+ (the 'py' launcher avoids the Microsoft Store 'python' stub).
#    No pipes after native commands here: in Windows PowerShell 5.1, `| Select-Object -First 1`
#    stops the native process early and leaves $LASTEXITCODE non-zero.
function Get-PythonVersion([string]$exe, [string[]]$pre) {
    try {
        $out = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $text = ("$out").Trim()
        if ($text -match '^(\d+)\.(\d+)$') {
            if ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11)) { return $text }
        }
    } catch { }
    return $null
}

Step "Finding Python 3.11+"
$pyCmd = $null
$ver = $null
$candidates = @(
    @{ Exe = "py"; Pre = @("-3") },
    @{ Exe = "python"; Pre = @() }
)
foreach ($c in $candidates) {
    if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
    $v = Get-PythonVersion $c.Exe $c.Pre
    Write-Host ("    {0} {1}: {2}" -f $c.Exe, ($c.Pre -join " "), $(if ($v) { $v } else { "not usable" }))
    if ($v) { $pyCmd = @($c.Exe) + $c.Pre; $ver = $v; break }
}
if (-not $pyCmd) {
    throw "Python 3.11+ not found. Install it with:  winget install --id Python.Python.3.13 -e   then open a new PowerShell window and re-run this script."
}
Write-Host "    using $($pyCmd -join ' ') ($ver)"

# 2. Virtual environment + package (no third-party runtime dependencies).
Step "Creating/updating .venv"
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    $pyArgs = @($pyCmd | Select-Object -Skip 1) + @("-m", "venv", ".venv")
    & $pyCmd[0] @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}
$venvPy = (Resolve-Path ".\.venv\Scripts\python.exe").Path

# Windows locks a running .exe, so a running server would make the reinstall fail
# (typical when re-running setup after `git pull`). Stop our logon task, restart it after.
$restartTask = $false
$task = Get-ScheduledTask -TaskName "CouponHunter" -ErrorAction SilentlyContinue
if ($task -and $task.State -eq "Running") {
    Write-Host "    stopping the running CouponHunter task for the update"
    Stop-ScheduledTask -TaskName "CouponHunter"
    $restartTask = $true
}
$exePath = Join-Path (Get-Location).Path ".venv\Scripts\couponhunter.exe"
foreach ($i in 1..10) {
    $running = @(Get-Process -Name couponhunter -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exePath })
    if ($running.Count -eq 0) { break }
    if ($restartTask) { Start-Sleep -Seconds 1; continue }
    throw "CouponHunter is running from this folder (PID $($running[0].Id)). Stop it (Ctrl+C in its window) and re-run setup."
}
& $venvPy -m pip install --disable-pip-version-check -q -e .
if ($LASTEXITCODE -ne 0) { throw "pip install failed (see the messages above)." }
$ch = (Resolve-Path ".\.venv\Scripts\couponhunter.exe").Path

# 3. Data folder, database, API token (idempotent).
Step "Initialising data folder"
if ($Demo) { & $ch init --demo } else { & $ch init }
if ($LASTEXITCODE -ne 0) { throw "couponhunter init failed." }

# 4. Optional: start at logon.
if ($AutoStart) {
    Step "Registering logon task 'CouponHunter'"
    if (Get-ScheduledTask -TaskName "CouponHunter" -ErrorAction SilentlyContinue) {
        Write-Host "    already registered (leaving it unchanged)"
        if ($restartTask) { Start-ScheduledTask -TaskName "CouponHunter"; Write-Host "    restarted" }
    } else {
        # Run python.exe directly (not pip's couponhunter.exe launcher, which spawns a child process):
        # stopping the task then really stops the server, and updates never hit a locked .exe.
        $action   = New-ScheduledTaskAction -Execute $venvPy -Argument "-m couponhunter serve" -WorkingDirectory (Get-Location).Path
        $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
        Register-ScheduledTask -TaskName "CouponHunter" -Action $action -Trigger $trigger -Settings $settings `
            -ErrorAction Stop | Out-Null
        Start-ScheduledTask -TaskName "CouponHunter" -ErrorAction Stop
        Write-Host "    registered and started"
    }
}

if ($restartTask -and -not $AutoStart) {
    Start-ScheduledTask -TaskName "CouponHunter"
    Write-Host "    restarted the CouponHunter task"
}

# 5. Health check and next steps.
Step "Checking the setup"
& $ch doctor --offline
$token = (& $ch token)
Write-Host ""
Write-Host "Next steps" -ForegroundColor Green
Write-Host "  1. Start the server (if you didn't use -AutoStart):  $ch serve"
Write-Host "  2. Dashboard: http://127.0.0.1:8765   (sign in with the token below)"
Write-Host "  3. Browser: open chrome://extensions, turn on Developer mode, Load unpacked ->"
Write-Host "     $((Resolve-Path .\extension).Path)"
Write-Host "     then paste the token in the extension's options."
Write-Host "  4. Optional phone companion: docs\09-operations.md section 4 (Telegram)."
Write-Host ""
Write-Host "API token: $token"
exit 0
