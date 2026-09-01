# Creates a shortcut for Coding Plan Tray.
# By default the shortcut is created on the desktop.
# Use -Startup to create it in the user's Startup folder instead.

param(
    # Optional: full path to pythonw.exe/python.exe. If omitted, the script
    # resolves a suitable interpreter from PATH automatically.
    [string]$PythonExe = "",

    # Optional: create the shortcut in the Startup folder so the app starts at logon.
    [switch]$Startup
)

$ErrorActionPreference = "Stop"

# Project files are always resolved relative to this script, so the repository
# can live in any directory without editing this file.
$ProjectDir = $PSScriptRoot
$MainPy = Join-Path $ProjectDir "main.py"
$IconPath = Join-Path $ProjectDir "assets\icon.ico"

if (-not (Test-Path -LiteralPath $MainPy)) {
    throw "main.py was not found under: $ProjectDir"
}
if (-not (Test-Path -LiteralPath $IconPath)) {
    Write-Warning "Icon not found at: $IconPath. The shortcut will use Python's default icon."
}

function Resolve-PythonExe {
    # Prefer windowless interpreters so launching the app does not open a console.
    foreach ($name in @("pythonw.exe", "pyw.exe", "python.exe", "py.exe")) {
        $command = Get-Command -Name $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source) {
            return $command.Source
        }
    }

    throw "Python was not found on PATH. Install Python or pass -PythonExe 'C:\path\to\pythonw.exe'."
}

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Resolve-PythonExe
}
else {
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "Python executable not found: $PythonExe"
    }
}

$shortcutDir = if ($Startup) {
    [Environment]::GetFolderPath("Startup")
} else {
    [Environment]::GetFolderPath("Desktop")
}

$shortcutPath = Join-Path $shortcutDir "Coding Plan Tray.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $PythonExe
$shortcut.Arguments = '"' + $MainPy + '"'
$shortcut.WorkingDirectory = $ProjectDir
if (Test-Path -LiteralPath $IconPath) {
    $shortcut.IconLocation = "$IconPath,0"
}
$shortcut.Description = "Kimi & GLM Coding Plan quota monitor"
$shortcut.Save()

Write-Host "Shortcut created: $shortcutPath"
Write-Host "Python executable: $PythonExe"
Write-Host "Target script: $MainPy"

if ((Split-Path -Leaf $PythonExe) -in @("python.exe", "py.exe")) {
    Write-Warning "Using $($PythonExe) may keep a console window open. Prefer pythonw.exe or pyw.exe when available."
}

