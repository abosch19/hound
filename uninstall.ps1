<#
.SYNOPSIS
    Desinstala Hound.

.DESCRIPTION
    Para y elimina el servicio, la regla del firewall, el programa y el acceso del PATH.
    Por defecto CONSERVA los datos (configuracion, indice, tokens, registros) en %ProgramData%\Hound;
    con -Purge tambien los borra. Nunca toca la carpeta compartida.
    Tesseract no se desinstala (puede usarlo otro programa): se quita desde "Aplicaciones".

    irm https://raw.githubusercontent.com/abosch19/hound/main/uninstall.ps1 | iex
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "$env:ProgramFiles\Hound",
    [string]$DataDir = "$env:ProgramData\Hound",
    [switch]$Purge
)

$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Ejecuta este script en una consola de PowerShell abierta como administrador."
}

if (Get-Service -Name "Hound" -ErrorAction SilentlyContinue) {
    Write-Host "Parando y eliminando el servicio..."
    Stop-Service -Name "Hound" -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    $svcExe = "$InstallDir\service\HoundService.exe"
    if (Test-Path $svcExe) { & $svcExe uninstall | Out-Null } else { & sc.exe delete Hound | Out-Null }
}

Get-NetFirewallRule -DisplayName "Hound MCP" -ErrorAction SilentlyContinue | Remove-NetFirewallRule

$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$newPath = ($machinePath -split ";" | Where-Object { $_ -and $_ -ne "$InstallDir\bin" }) -join ";"
[Environment]::SetEnvironmentVariable("Path", $newPath, "Machine")
[Environment]::SetEnvironmentVariable("HOUND_CONFIG", $null, "Machine")

if (Test-Path $InstallDir) {
    Write-Host "Eliminando $InstallDir..."
    Remove-Item -Recurse -Force $InstallDir
}

if ($Purge) {
    if (Test-Path $DataDir) {
        Write-Host "Eliminando datos en $DataDir..."
        Remove-Item -Recurse -Force $DataDir
    }
} else {
    Write-Host "Se conservan la configuracion, el indice y los tokens en $DataDir (usa -Purge para borrarlos)."
}
Write-Host "Hound desinstalado." -ForegroundColor Green
