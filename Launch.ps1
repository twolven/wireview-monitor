$ErrorActionPreference = 'Stop'
Start-Process -FilePath (Join-Path $PSScriptRoot '.venv/Scripts/pythonw.exe') -ArgumentList ('"' + (Join-Path $PSScriptRoot 'monitor.pyw') + '"') -WorkingDirectory $PSScriptRoot
