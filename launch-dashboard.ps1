[CmdletBinding()]
param(
    [string]$EnvironmentName = 'telemetry',
    [ValidateRange(1, 65535)]
    [int]$Port = 6767,
    [string]$CondaExe,
    [string]$BrowserPath,
    [switch]$CreateDesktopShortcut,
    [switch]$NoBrowser,
    [switch]$Stop,
    [switch]$ManagedSession
)

$ErrorActionPreference = 'Stop'
if (-not $ManagedSession) {
    $application = Join-Path $PSScriptRoot 'Telemetry.exe'
    if (-not (Test-Path -LiteralPath $application)) { throw 'Run build-app.ps1 to build the lifecycle-managed launcher first.' }
    $forward = @()
    foreach ($entry in $PSBoundParameters.GetEnumerator()) {
        if ($entry.Value -is [switch] -and -not $entry.Value) { continue }
        $forward += "-$($entry.Key)"
        if ($entry.Value -isnot [switch]) { $forward += [string]$entry.Value }
    }
    & $application @forward | Out-Host
    exit $LASTEXITCODE
}
$url = "http://127.0.0.1:$Port"
$dataDirectory = Join-Path $PSScriptRoot 'data'
$serverProcess = $null
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
Start-Transcript -Path (Join-Path $dataDirectory 'dashboard.launcher.log') -Force | Out-Null

function Find-Conda {
    if ($CondaExe) {
        if (-not (Test-Path -LiteralPath $CondaExe -PathType Leaf)) {
            throw "Conda executable not found: $CondaExe"
        }
        return $CondaExe
    }
    $command = Get-Command conda.exe -ErrorAction SilentlyContinue
    $candidates = @(
        $env:CONDA_EXE
        $(if ($command) { $command.Source })
        "$env:ProgramData\anaconda3\Scripts\conda.exe"
        "$env:ProgramData\miniconda3\Scripts\conda.exe"
        "$env:USERPROFILE\anaconda3\Scripts\conda.exe"
        "$env:USERPROFILE\miniconda3\Scripts\conda.exe"
        "$env:LOCALAPPDATA\miniconda3\Scripts\conda.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    throw 'Conda was not found. Run from an Anaconda Prompt or supply -CondaExe with its full path.'
}

function Find-AppBrowser {
    $path = $BrowserPath
    if (-not $path) {
        $choice = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice'
        $key = Get-Item "Registry::HKEY_CLASSES_ROOT\$($choice.ProgId)\shell\open\command"
        $commandLine = $key.GetValue('')
        if ($commandLine -match '^"([^"]+)"') {
            $path = $Matches[1]
        } elseif ($commandLine -match '^(\S+\.exe)(?:\s|$)') {
            $path = $Matches[1]
        } else {
            throw "Cannot resolve the default browser executable from: $commandLine"
        }
    }
    $path = [Environment]::ExpandEnvironmentVariables($path)
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Browser executable not found: $path"
    }
    if ([IO.Path]::GetFileName($path) -notin @('msedge.exe', 'chrome.exe', 'brave.exe')) {
        throw 'The default browser does not support this app-mode launcher. Supply -BrowserPath pointing to Microsoft Edge, Chrome, or Brave.'
    }
    return $path
}

function Stop-LegacyDashboard {
    $scriptPath = [regex]::Escape((Join-Path $PSScriptRoot 'dashboard.py'))
    $processes = @(Get-CimInstance Win32_Process)
    $servers = @($processes | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and
        $_.CommandLine -match "(?:^|\s)`"?$scriptPath`"?(?=\s|$)"
    })
    function Stop-OwnedTree($process) {
        foreach ($child in $processes | Where-Object {
            $_.ParentProcessId -eq $process.ProcessId -and $_.CreationDate -ge $process.CreationDate
        }) { Stop-OwnedTree $child }
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ProcessId)"
        if ($current -and $current.CreationDate -eq $process.CreationDate) {
            Stop-Process -Id $process.ProcessId -ErrorAction Stop
            Write-Host "Stopped previous dashboard process $($process.ProcessId)."
        }
    }
    foreach ($server in $servers) { Stop-OwnedTree $server }
}

function Wait-Dashboard {
    $deadline = (Get-Date).AddSeconds(60)
    do {
        if ($serverProcess -and $serverProcess.HasExited) {
            $details = Get-Content -LiteralPath (Join-Path $dataDirectory 'dashboard.stderr.log') -Tail 15 -ErrorAction SilentlyContinue
            throw "Dashboard exited with code $($serverProcess.ExitCode). $($details -join [Environment]::NewLine)"
        }
        $request = [Net.HttpWebRequest]::Create("$url/api/stats")
        $request.Timeout = 3000
        $request.Proxy = $null
        $response = $null
        try { $response = $request.GetResponse() } catch [System.Net.WebException] {
            if ($_.Exception.Response) {
                $response = $_.Exception.Response
            } elseif ($_.Exception.Status -eq [System.Net.WebExceptionStatus]::ConnectFailure) {
                Write-Verbose "Waiting for local server: $($_.Exception.Message)"
            } else { throw }
        }
        if ($response) {
            try {
                $reader = New-Object IO.StreamReader($response.GetResponseStream())
                try { $stats = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
                $status = [int]$response.StatusCode
            } finally { $response.Dispose() }
            if ($status -eq 200) {
                if ($null -eq $stats.system -or $null -eq $stats.power -or $null -eq $stats.gpus) {
                    throw "Port $Port returned an unexpected telemetry response."
                }
                return
            }
            if ($status -ne 503 -or $stats.error -ne 'Telemetry is warming up; retry shortly') {
                throw "Dashboard returned HTTP ${status}: $($stats.error)"
            }
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Dashboard did not become ready within 60 seconds. Check $dataDirectory\dashboard.stderr.log."
}

try {
    Stop-LegacyDashboard
    # Conda's hook configures activation correctly even outside an Anaconda Prompt.
    $conda = Find-Conda
    $hook = & $conda 'shell.powershell' 'hook'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to initialize the Conda PowerShell hook.' }
    Invoke-Expression ($hook -join [Environment]::NewLine)
    conda activate $EnvironmentName
    if ($LASTEXITCODE -ne 0) { throw "Unable to activate Conda environment '$EnvironmentName'." }
    $python = Join-Path $env:CONDA_PREFIX 'python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw "Python is missing from '$EnvironmentName'." }
    & $python -c 'import psutil'
    if ($LASTEXITCODE -ne 0) {
        throw "Dashboard dependency missing. Activate '$EnvironmentName' and run: python -m pip install -r requirements.txt"
    }
    Write-Host "Activated Conda environment: $env:CONDA_DEFAULT_ENV"
    if (-not $NoBrowser -or $CreateDesktopShortcut) { $browser = Find-AppBrowser }

        New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
        $serverProcess = Start-Process -FilePath $python -ArgumentList @(
            '-u', "`"$(Join-Path $PSScriptRoot 'dashboard.py')`"", '--port', $Port
        ) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $dataDirectory 'dashboard.stdout.log') `
            -RedirectStandardError (Join-Path $dataDirectory 'dashboard.stderr.log')
        Write-Host "Started dashboard server, PID $($serverProcess.Id)."
    Wait-Dashboard
    if ($serverProcess) {
        Set-Content -LiteralPath (Join-Path $dataDirectory 'dashboard.pid') -Value $serverProcess.Id
    }

    if ($CreateDesktopShortcut) {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut((Join-Path $desktop 'Telemetry.lnk'))
        $application = Join-Path $PSScriptRoot 'Telemetry.exe'
        if (-not (Test-Path -LiteralPath $application)) {
            throw 'App executable is missing. Run build-app.ps1 before creating the desktop shortcut.'
        }
        $shortcut.TargetPath = $application
        $shortcut.Arguments = "-EnvironmentName `"$EnvironmentName`" -Port $Port -CondaExe `"$conda`""
        if ($BrowserPath) { $shortcut.Arguments += " -BrowserPath `"$browser`"" }
        $shortcut.WorkingDirectory = $PSScriptRoot
        $icon = Join-Path $PSScriptRoot 'assets\telemetry.ico'
        if (-not (Test-Path -LiteralPath $icon)) {
            throw 'App icon is missing. Run build-app.ps1 to generate the icon and executable.'
        }
        $shortcut.IconLocation = "$icon,0"
        $shortcut.Description = 'Telemetry hardware performance console'
        $shortcut.Save()
        Write-Host "Created desktop shortcut: $desktop\Telemetry.lnk"
    }
    if (-not $NoBrowser) {
        $profile = Join-Path $dataDirectory 'app-browser-profile'
        $browserProcess = Start-Process -FilePath $browser -ArgumentList @(
            "--app=$url", '--window-size=1440,960', "--user-data-dir=`"$profile`"",
            '--no-first-run', '--no-default-browser-check', '--disable-background-mode'
        ) -PassThru
        Write-Host "Opened dashboard in app mode: $url"
        while (-not $browserProcess.WaitForExit(500)) {
            if ($serverProcess.HasExited) { throw "Dashboard server exited with code $($serverProcess.ExitCode)." }
        }
        if ($browserProcess.ExitCode -ne 0) { throw "App browser exited with code $($browserProcess.ExitCode)." }
    } else {
        Write-Host "Dashboard ready: $url (headless; stop with Telemetry.exe -Stop)"
        $serverProcess.WaitForExit()
        if ($serverProcess.ExitCode -ne 0) { throw "Dashboard server exited with code $($serverProcess.ExitCode)." }
    }
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    if (-not $NoBrowser) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            $_.Exception.Message, 'Telemetry - launch failed', 'OK', 'Error'
        ) | Out-Null
    }
    exit 1
} finally {
    # The supervising EXE closes its Windows Job Object when this script exits,
    # terminating the server, browser, and all their remaining descendants.
    if ($serverProcess) {
        if (-not $serverProcess.HasExited) {
            $serverProcess.Kill()
            if (-not $serverProcess.WaitForExit(10000)) { throw 'Dashboard server did not stop within 10 seconds.' }
        }
        $pidFile = Join-Path $dataDirectory 'dashboard.pid'
        if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
    }
    Stop-Transcript | Out-Null
}
