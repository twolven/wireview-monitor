# Start the monitor at sign-in (HKCU Run). Does not touch any other app's startup entries.
# Remove with:  .\Install-Startup.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$runPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if ($Remove) {
    Remove-ItemProperty -LiteralPath $runPath -Name 'WireViewMonitor' -ErrorAction SilentlyContinue
    return
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$script = Join-Path $PSScriptRoot 'monitor.pyw'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $script)) {
    throw 'The monitor or its Python environment is missing.'
}
New-ItemProperty -LiteralPath $runPath -Name 'WireViewMonitor' -PropertyType String -Value ('"{0}" "{1}"' -f $python, $script) -Force | Out-Null
