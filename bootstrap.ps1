$ErrorActionPreference = 'Stop'
try {
    $launcherRoot = $PSScriptRoot
    $settingsPath = Join-Path $env:USERPROFILE '.harness-launcher\settings.json'
    $applicationPath = $env:DSH_EXECUTABLE
    if (Test-Path -LiteralPath $settingsPath) {
        $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($settings.executable) { $applicationPath = [string]$settings.executable }
    }
    # A first launch has no settings yet. Use the same installer and standard
    # folders as launcher.py so Harness's bundled runtime can bootstrap it.
    $applicationPaths = @($applicationPath)
    $uninstallKeys = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    foreach ($entry in @(Get-ItemProperty -Path $uninstallKeys -ErrorAction SilentlyContinue)) {
        if ($entry.DisplayName -notlike 'DeepSeek Harness*') { continue }
        if ($entry.DisplayIcon) { $applicationPaths += ([string]$entry.DisplayIcon -replace ',\s*-?\d+$', '').Trim().Trim('"') }
        if ($entry.InstallLocation) { $applicationPaths += Join-Path ([string]$entry.InstallLocation).Trim().Trim('"') 'DeepSeek Harness.exe' }
        if ($entry.UninstallString -match '^\s*"?([^"]+?\.exe)') {
            $applicationPaths += Join-Path (Split-Path -Parent $Matches[1]) 'DeepSeek Harness.exe'
        }
    }
    foreach ($base in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (!$base) { continue }
        $applicationPaths += Join-Path $base 'Programs\DeepSeek Harness\DeepSeek Harness.exe'
        $applicationPaths += Join-Path $base 'DeepSeek Harness\DeepSeek Harness.exe'
    }
    $applicationPaths += Join-Path (Split-Path -Parent $launcherRoot) 'DeepSeek Harness.exe'
    $pythonCandidates = @()
    foreach ($application in $applicationPaths) {
        if ($application -and (Test-Path -LiteralPath $application -PathType Leaf)) {
            $pythonCandidates += Join-Path (Split-Path -Parent $application) 'resources\runtime\primary-runtime\dependencies\python\pythonw.exe'
        }
    }
    $pythonCandidates += @(Get-Command pythonw.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    $runtimePython = $null
    foreach ($candidatePython in @($pythonCandidates | Select-Object -Unique)) {
        if (!(Test-Path -LiteralPath $candidatePython -PathType Leaf)) { continue }
        try {
            $probe = Start-Process -FilePath $candidatePython -ArgumentList '-c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"' -PassThru -Wait -WindowStyle Hidden
            if ($probe.ExitCode -eq 0) { $runtimePython = $candidatePython; break }
        } catch {
            # An unusable runtime must not hide another working installation.
        }
    }
    if (!$runtimePython) {
        throw 'Python 3.12+ was not found. Set DSH_EXECUTABLE to your DeepSeek Harness.exe path, or install Python 3.12+ and add it to PATH.'
    }
    $entryPath = Join-Path $launcherRoot 'launcher.py'
    Start-Process -FilePath $runtimePython -ArgumentList ('"' + $entryPath + '"') -WorkingDirectory $launcherRoot -WindowStyle Hidden
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Harness Launcher') | Out-Null
}
