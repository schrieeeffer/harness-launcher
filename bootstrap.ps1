$ErrorActionPreference = 'Stop'
try {
    $launcherRoot = $PSScriptRoot
    $settingsPath = Join-Path $env:USERPROFILE '.harness-launcher\settings.json'
    $applicationPath = $env:DSH_EXECUTABLE
    if (Test-Path -LiteralPath $settingsPath) {
        $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($settings.executable) { $applicationPath = [string]$settings.executable }
    }
    $runtimePython = $null
    if ($applicationPath) {
        $candidatePython = Join-Path (Split-Path -Parent $applicationPath) 'resources\runtime\primary-runtime\dependencies\python\pythonw.exe'
        if (Test-Path -LiteralPath $candidatePython) { $runtimePython = $candidatePython }
    }
    if (!$runtimePython) {
        $fallback = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if (!$fallback) { throw 'Python 3.12+ was not found. Set DSH_EXECUTABLE to your DeepSeek Harness.exe path, or install Python and add it to PATH.' }
        $runtimePython = $fallback.Source
    }
    $entryPath = Join-Path $launcherRoot 'launcher.py'
    Start-Process -FilePath $runtimePython -ArgumentList ('"' + $entryPath + '"') -WorkingDirectory $launcherRoot -WindowStyle Hidden
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Harness Launcher') | Out-Null
}
