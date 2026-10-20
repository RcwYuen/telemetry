[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$application = Join-Path $PSScriptRoot 'Telemetry.exe'
$url = 'http://127.0.0.1:6767/api/stats'

Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class DashboardTestWindows {
    private delegate bool Callback(IntPtr window, IntPtr state);
    [DllImport("user32.dll")] private static extern bool EnumWindows(Callback callback, IntPtr state);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll", SetLastError=true)] private static extern bool PostMessage(IntPtr window, uint message, IntPtr w, IntPtr l);
    public static bool CloseAppWindow(int[] pids) {
        var owned = new HashSet<int>(pids);
        bool closed = false;
        EnumWindows(delegate(IntPtr window, IntPtr state) {
            uint pid; GetWindowThreadProcessId(window, out pid);
            if (owned.Contains((int)pid) && IsWindowVisible(window)) {
                if (!PostMessage(window, 0x0010, IntPtr.Zero, IntPtr.Zero))
                    throw new System.ComponentModel.Win32Exception();
                closed = true;
            }
            return true;
        }, IntPtr.Zero);
        return closed;
    }
}
'@

function Assert($condition, [string]$message) {
    if (-not $condition) { throw $message }
    Write-Host "PASS: $message"
}

function Wait-Ready($supervisor, $previousPid = 0) {
    $deadline = (Get-Date).AddSeconds(75)
    do {
        if ($supervisor.HasExited) { throw "Supervisor exited early: $($supervisor.ExitCode)" }
        $pidFile = Join-Path $PSScriptRoot 'data\dashboard.pid'
        if (Test-Path -LiteralPath $pidFile) {
            $serverId = [int](Get-Content -LiteralPath $pidFile)
            $server = Get-CimInstance Win32_Process -Filter "ProcessId = $serverId"
            $owner = $null
            if ($server) { $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($server.ParentProcessId)" }
            if ($serverId -ne $previousPid -and $owner -and $owner.ParentProcessId -eq $supervisor.Id) {
                $response = $null
                $request = [Net.HttpWebRequest]::Create($url)
                $request.Timeout = 2000
                $request.Proxy = $null
                try { $response = $request.GetResponse() }
                catch [Net.WebException] {
                    if ($_.Exception.Response) { $_.Exception.Response.Dispose() }
                    elseif ($_.Exception.Status -ne [Net.WebExceptionStatus]::ConnectFailure) { throw }
                }
                if ($response) {
                    $reader = New-Object IO.StreamReader($response.GetResponseStream())
                    try { $stats = $reader.ReadToEnd() | ConvertFrom-Json }
                    finally { $reader.Dispose(); $response.Dispose() }
                    return @{ Pid = $serverId; Stats = $stats }
                }
            }
        }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    throw 'Dashboard readiness timed out.'
}

function Get-SessionProcesses($supervisor) {
    $all = @(Get-CimInstance Win32_Process)
    $ids = @($supervisor.Id)
    do {
        $children = @($all | Where-Object { $_.ParentProcessId -in $ids -and $_.ProcessId -notin $ids })
        $ids += @($children | ForEach-Object { $_.ProcessId })
    } while ($children.Count)
    return @($all | Where-Object { $_.ProcessId -in $ids })
}

function Assert-Clean($supervisor, $owned) {
    Assert ($supervisor.WaitForExit(20000)) 'Supervisor exits within 20 seconds'
    $deadline = (Get-Date).AddSeconds(10)
    do {
        $remaining = @($owned | Where-Object {
            $original = $_
            $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($original.ProcessId)"
            $current -and $current.CreationDate -eq $original.CreationDate
        })
        if (-not $remaining.Count) { break }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    Assert ($remaining.Count -eq 0) 'All captured session descendants exit'
}

$started = @()
$python = Join-Path $env:USERPROFILE '.conda\envs\telemetry\python.exe'
$unrelated = Start-Process -FilePath $python -ArgumentList '-c', '"import time; time.sleep(600)"' -PassThru
try {
    $first = Start-Process -FilePath $application -ArgumentList '-NoBrowser', '-CreateDesktopShortcut' -PassThru
    $started += $first
    $initial = Wait-Ready $first
    $owned = Get-SessionProcesses $first
    $page = Invoke-WebRequest -Uri 'http://127.0.0.1:6767/' -UseBasicParsing
    Assert ($page.Content -match '<title>Telemetry</title>' -and $page.Content -match '<h1>TELEMETRY</h1>') 'App title and header use Telemetry'
    $icon = Invoke-WebRequest -Uri 'http://127.0.0.1:6767/favicon.ico' -UseBasicParsing
    Assert ($icon.StatusCode -eq 200 -and $icon.Headers['Content-Type'] -eq 'image/x-icon') 'Renamed Telemetry icon is served'
    Assert ((Get-Item -LiteralPath $application).VersionInfo.ProductName -eq 'Telemetry') 'Executable product name is Telemetry'
    $shortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Telemetry.lnk'
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    Assert ($shortcut.TargetPath -eq $application -and $shortcut.IconLocation -eq "$(Join-Path $PSScriptRoot 'assets\telemetry.ico'),0") 'Telemetry desktop shortcut targets the renamed app and icon'
    Assert ($initial.Stats.power.history[0].energy_wh -eq 0) 'First sample starts at zero energy'

    $second = Start-Process -FilePath $application -ArgumentList '-NoBrowser' -PassThru
    $started += $second
    $replacement = Wait-Ready $second $initial.Pid
    Assert-Clean $first $owned
    Assert ($replacement.Pid -ne $initial.Pid) 'Relaunch creates a new server PID'
    Assert ($replacement.Stats.power.history[0].energy_wh -eq 0) 'Relaunch resets energy'
    $owned = Get-SessionProcesses $second
    $stopper = Start-Process -FilePath $application -ArgumentList '-Stop' -PassThru
    Assert ($stopper.WaitForExit(25000)) 'Explicit stop command completes'
    Assert ($stopper.ExitCode -eq 0) 'Explicit stop succeeds'
    Assert-Clean $second $owned

    $windowed = Start-Process -FilePath $application -PassThru
    $started += $windowed
    $null = Wait-Ready $windowed
    $deadline = (Get-Date).AddSeconds(20)
    do {
        $owned = Get-SessionProcesses $windowed
        $browserIds = [int[]]@($owned | Where-Object { $_.Name -in @('msedge.exe','chrome.exe','brave.exe') } | ForEach-Object { $_.ProcessId })
        $closed = $browserIds.Count -gt 0 -and [DashboardTestWindows]::CloseAppWindow($browserIds)
        if ($closed) { break }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    Assert $closed 'Close only this session browser window with WM_CLOSE'
    Assert-Clean $windowed $owned
    Assert ($windowed.ExitCode -eq 0) 'Closing the browser ends the app successfully'

    $forced = Start-Process -FilePath $application -ArgumentList '-NoBrowser' -PassThru
    $started += $forced
    $null = Wait-Ready $forced
    $owned = Get-SessionProcesses $forced
    Stop-Process -Id $forced.Id
    Assert-Clean $forced $owned
    Assert (-not $unrelated.HasExited) 'Unrelated telemetry-environment Python process survives all cleanup'

    $direct = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        "`"$(Join-Path $PSScriptRoot 'launch-dashboard.ps1')`"", '-NoBrowser'
    ) -PassThru
    $started += $direct
    $deadline = (Get-Date).AddSeconds(75)
    do {
        $managed = @(Get-CimInstance Win32_Process | Where-Object {
            $_.Name -eq 'Telemetry.exe' -and $_.ParentProcessId -eq $direct.Id
        })
        if ($managed.Count) { break }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    Assert ($managed.Count -eq 1) 'Direct PowerShell entry point routes through the EXE supervisor'
    $managedProcess = Get-Process -Id $managed[0].ProcessId
    $started += $managedProcess
    $null = Wait-Ready $managedProcess
    $owned = Get-SessionProcesses $managedProcess
    & $env:ComSpec /c "`"$(Join-Path $PSScriptRoot 'launch-dashboard.cmd')`" -Stop"
    Assert ($LASTEXITCODE -eq 0) 'CMD stop command succeeds'
    Assert-Clean $managedProcess $owned
    Assert ($direct.WaitForExit(10000)) 'Direct PowerShell wrapper exits with its session'
    Assert ($direct.ExitCode -eq 0) 'Direct PowerShell wrapper forwards successful exit code'
    Write-Host 'All Windows launcher lifecycle tests passed.'
} finally {
    foreach ($process in $started) {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id }
        $process.Dispose()
    }
    if (-not $unrelated.HasExited) { $unrelated.Kill(); $unrelated.WaitForExit() }
    $unrelated.Dispose()
}
