# Create (or recreate) the desktop shortcut "Personal Knowledge Base".
# Run this again after moving the project folder.
# ASCII only on purpose: Windows PowerShell 5.1 reads .ps1 files as ANSI.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$script = Join-Path $root "launcher\launch_pkb.py"
$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop "Personal Knowledge Base.lnk"

$pythonw = "D:\python\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($found) { $pythonw = $found.Source }
}
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found: install Python or add it to PATH." }

$quote = [char]34
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = $quote + $script + $quote
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,167"
$shortcut.Description = "Personal Knowledge Base 1.0"
$shortcut.Save()

Write-Host "shortcut created: $link"
Write-Host "target: $pythonw $script"
