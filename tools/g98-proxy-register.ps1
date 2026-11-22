$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs = Join-Path $here 'G98Proxy.vbs'
$startup = [Environment]::GetFolderPath('Startup')
$target = Join-Path $startup 'G98Proxy.vbs'
if (-not (Test-Path -LiteralPath $vbs)) { throw "missing $vbs" }
Copy-Item -LiteralPath $vbs -Destination $target -Force
Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $target + '"') -WindowStyle Hidden
Start-Sleep -Seconds 3
Get-Item -LiteralPath $target | Select-Object FullName,Length | Format-List
Write-Host 'G98Proxy installed in Startup and launched.'
