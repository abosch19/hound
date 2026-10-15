<#
.SYNOPSIS
    Instala (o actualiza) Hound en Windows Server como servicio.

.DESCRIPTION
    - Descarga uv y, con el, un Python 3.12 propio (no toca el Python del sistema).
    - Instala Hound y sus dependencias en un entorno aislado.
    - Instala Tesseract (OCR) con los idiomas indicados.
    - Descarga el modelo de embeddings una sola vez.
    - Registra el servicio "Hound" (arranque automatico y reinicio si falla) y abre el puerto
      en el firewall solo para la red de la VPN.

    Volver a ejecutarlo actualiza Hound y conserva la configuracion, el indice y los tokens.

    Uso rapido (PowerShell como administrador):
        irm https://raw.githubusercontent.com/abosch19/hound/main/install.ps1 | iex

    Con parametros:
        & ([scriptblock]::Create((irm https://raw.githubusercontent.com/abosch19/hound/main/install.ps1))) `
            -SharePath D:\Compartido -AllowFrom 10.8.0.0/24

    Los parametros tambien se pueden pasar con variables de entorno HOUND_SHARE_PATH,
    HOUND_ALLOW_FROM, HOUND_PORT, HOUND_SHARE_NAME, HOUND_UNC, HOUND_VERSION.
#>
[CmdletBinding()]
param(
    # Ruta local de la carpeta compartida (p. ej. D:\Compartido). Si se omite, se detecta.
    [string]$SharePath = $env:HOUND_SHARE_PATH,
    # Nombre del recurso compartido SMB (para aplicar tambien sus permisos). Si se omite, se detecta.
    [string]$ShareName = $env:HOUND_SHARE_NAME,
    # Ruta UNC que ven los usuarios (por defecto \\NOMBRE-DEL-SERVIDOR\RECURSO).
    [string]$Unc = $env:HOUND_UNC,
    # Puerto del servidor MCP.
    [int]$Port = $(if ($env:HOUND_PORT) { [int]$env:HOUND_PORT } else { 8765 }),
    # Red(es) desde la que se permite conectar, p. ej. "10.8.0.0/24" (la de la VPN). Separadas por comas.
    [string]$AllowFrom = $env:HOUND_ALLOW_FROM,
    # Rama o etiqueta de GitHub a instalar.
    [string]$Version = $(if ($env:HOUND_VERSION) { $env:HOUND_VERSION } else { "main" }),
    # Instalar desde una carpeta local con el codigo (para pruebas sin conexion a GitHub).
    [string]$Source = "",
    [string]$Repo = "abosch19/hound",
    [string]$InstallDir = "$env:ProgramFiles\Hound",
    [string]$DataDir = "$env:ProgramData\Hound",
    # Idiomas de OCR (codigos de Tesseract).
    [string[]]$OcrLanguages = @("spa", "eng"),
    [switch]$NoOcr,
    [switch]$NoService
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest es muy lento con la barra de progreso
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$UvVersion = "0.9.26"
$WinSWVersion = "v2.12.0"
$TesseractUrl = "https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe"
$PythonVersion = "3.12"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" }
function Warn($msg) { Write-Host "    AVISO: $msg" -ForegroundColor Yellow }

function Download($url, $dest) {
    Info "Descargando $url"
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

# --- comprobaciones -----------------------------------------------------------------

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Ejecuta este script en una consola de PowerShell abierta como administrador."
}
if (-not [Environment]::Is64BitOperatingSystem) { throw "Hound necesita Windows de 64 bits." }

Step "Carpeta compartida"
if (-not $SharePath) {
    $shares = @(Get-SmbShare -ErrorAction SilentlyContinue | Where-Object {
        -not $_.Special -and $_.Path -and $_.Name -notmatch '\$$' -and $_.Name -notin @("NETLOGON", "SYSVOL", "print$")
    })
    if ($shares.Count -eq 1) {
        $SharePath = $shares[0].Path
        if (-not $ShareName) { $ShareName = $shares[0].Name }
        Info "Detectada: $($shares[0].Name) -> $SharePath"
    } elseif ($shares.Count -gt 1) {
        Info "Recursos compartidos en este servidor:"
        for ($i = 0; $i -lt $shares.Count; $i++) { Info ("  [{0}] {1} -> {2}" -f ($i + 1), $shares[$i].Name, $shares[$i].Path) }
        $choice = Read-Host "    Cual quieres indexar? (numero)"
        $sel = $shares[[int]$choice - 1]
        $SharePath = $sel.Path
        if (-not $ShareName) { $ShareName = $sel.Name }
    } else {
        $SharePath = Read-Host "    Ruta local de la carpeta compartida (p. ej. D:\Compartido)"
    }
}
if (-not (Test-Path -LiteralPath $SharePath -PathType Container)) { throw "No existe la carpeta $SharePath" }
if (-not $ShareName) {
    $match = Get-SmbShare -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ($_.Path.TrimEnd('\') -eq $SharePath.TrimEnd('\')) } | Select-Object -First 1
    if ($match) { $ShareName = $match.Name }
}
if (-not $Unc -and $ShareName) { $Unc = "\\$env:COMPUTERNAME\$ShareName" }
Info "Carpeta: $SharePath"
Info "Ruta UNC: $(if ($Unc) { $Unc } else { '(no detectada)' })"

New-Item -ItemType Directory -Force -Path $InstallDir, $DataDir, "$InstallDir\bin", "$DataDir\logs" | Out-Null
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("hound-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

# Si el servicio ya existe, se para para poder actualizar los ficheros.
$svcExe = "$InstallDir\service\HoundService.exe"
$existing = Get-Service -Name "Hound" -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -ne "Stopped") {
    Step "Parando el servicio Hound para actualizar"
    Stop-Service -Name "Hound" -Force
    Start-Sleep -Seconds 3
}

# Rutas de mas de 260 caracteres (habituales en carpetas compartidas con muchos niveles).
$fs = "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem"
if ((Get-ItemProperty -Path $fs -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled -ne 1) {
    Step "Activando el soporte de rutas largas de Windows (LongPathsEnabled)"
    Set-ItemProperty -Path $fs -Name LongPathsEnabled -Value 1 -Type DWord
}

# --- Python y Hound -------------------------------------------------------------------

Step "Instalando uv $UvVersion y Python $PythonVersion"
$uv = "$InstallDir\bin\uv.exe"
if (-not (Test-Path $uv) -or -not ((& $uv --version) -match [regex]::Escape($UvVersion))) {
    Download "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" "$tmp\uv.zip"
    Expand-Archive -Path "$tmp\uv.zip" -DestinationPath "$tmp\uv" -Force
    Copy-Item "$tmp\uv\uv.exe", "$tmp\uv\uvx.exe" "$InstallDir\bin\" -Force
}
$env:UV_PYTHON_INSTALL_DIR = "$InstallDir\python"
$env:UV_CACHE_DIR = "$InstallDir\cache"
$env:UV_NO_PROGRESS = "1"
& $uv python install $PythonVersion
if ($LASTEXITCODE) { throw "No se pudo instalar Python" }
$venv = "$InstallDir\venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    & $uv venv $venv --python $PythonVersion
    if ($LASTEXITCODE) { throw "No se pudo crear el entorno de Python" }
}

Step "Instalando Hound ($(if ($Source) { $Source } else { "$Repo@$Version" }))"
$pkg = if ($Source) { (Resolve-Path $Source).Path } else { "https://github.com/$Repo/archive/$Version.zip" }
& $uv pip install --python "$venv\Scripts\python.exe" --reinstall-package hound $pkg
if ($LASTEXITCODE) { throw "No se pudo instalar Hound" }
$hound = "$venv\Scripts\hound.exe"

# Acceso directo "hound" en el PATH del sistema.
Set-Content -Path "$InstallDir\bin\hound.cmd" -Value "@echo off`r`n`"$venv\Scripts\python.exe`" -m hound %*" -Encoding ASCII
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
if (($machinePath -split ";") -notcontains "$InstallDir\bin") {
    [Environment]::SetEnvironmentVariable("Path", "$machinePath;$InstallDir\bin", "Machine")
}
$env:Path = "$env:Path;$InstallDir\bin"
[Environment]::SetEnvironmentVariable("HOUND_CONFIG", "$DataDir\config.toml", "Machine")
$env:HOUND_CONFIG = "$DataDir\config.toml"

# --- OCR --------------------------------------------------------------------------------

if (-not $NoOcr) {
    Step "Tesseract (OCR)"
    $tessDir = "$env:ProgramFiles\Tesseract-OCR"
    if (-not (Test-Path "$tessDir\tesseract.exe")) {
        Download $TesseractUrl "$tmp\tesseract-setup.exe"
        Info "Instalando Tesseract..."
        $p = Start-Process -FilePath "$tmp\tesseract-setup.exe" -ArgumentList "/S" -Wait -PassThru
        if ($p.ExitCode -ne 0 -or -not (Test-Path "$tessDir\tesseract.exe")) { throw "La instalacion de Tesseract fallo" }
    } else {
        Info "Ya instalado en $tessDir"
    }
    foreach ($lang in $OcrLanguages) {
        $file = "$tessDir\tessdata\$lang.traineddata"
        if (-not (Test-Path $file)) {
            Download "https://github.com/tesseract-ocr/tessdata/raw/main/$lang.traineddata" $file
        }
    }
}

# --- configuracion y modelo -------------------------------------------------------------

Step "Configuracion"
$config = "$DataDir\config.toml"
if (-not (Test-Path $config)) {
    $initArgs = @("--config", $config, "init", "--share", $SharePath, "--port", $Port)
    if ($Unc) { $initArgs += @("--unc", $Unc) }
    if ($ShareName) { $initArgs += @("--share-name", $ShareName) }
    & $hound @initArgs
    if ($LASTEXITCODE) { throw "No se pudo crear la configuracion" }
    if (-not $NoOcr) {
        $text = (Get-Content $config -Raw -Encoding UTF8) -replace 'languages = "spa\+eng"', ('languages = "' + ($OcrLanguages -join "+") + '"')
        [IO.File]::WriteAllText($config, $text, (New-Object Text.UTF8Encoding $false))   # sin BOM
    }
} else {
    Info "Se conserva la configuracion existente: $config"
}

Step "Modelo de embeddings (se descarga una sola vez)"
& $hound --config $config model download
if ($LASTEXITCODE) { throw "No se pudo descargar el modelo" }

# Solo administradores y SYSTEM pueden leer el indice y la configuracion (contienen fragmentos de
# documentos y hashes de tokens).
& icacls $DataDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

# --- servicio ---------------------------------------------------------------------------

if (-not $NoService) {
    Step "Servicio de Windows"
    New-Item -ItemType Directory -Force -Path "$InstallDir\service" | Out-Null
    if (-not (Test-Path $svcExe)) {
        Download "https://github.com/winsw/winsw/releases/download/$WinSWVersion/WinSW-x64.exe" $svcExe
    }
    $xml = @"
<service>
  <id>Hound</id>
  <name>Hound</name>
  <description>Buscador semantico de la carpeta compartida (servidor MCP e indexador).</description>
  <executable>$venv\Scripts\python.exe</executable>
  <arguments>-m hound --config "$config" run</arguments>
  <env name="HOUND_CONFIG" value="$config"/>
  <workingdirectory>$DataDir</workingdirectory>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="120 sec"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>30 sec</stoptimeout>
  <logpath>$DataDir\logs</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
</service>
"@
    Set-Content -Path "$InstallDir\service\HoundService.xml" -Value $xml -Encoding UTF8
    if (-not (Get-Service -Name "Hound" -ErrorAction SilentlyContinue)) {
        & $svcExe install
        if ($LASTEXITCODE) { throw "No se pudo registrar el servicio" }
    }
    Start-Service -Name "Hound"
    Info "Servicio Hound en marcha (arranque automatico, se reinicia si falla)."

    Step "Firewall"
    Get-NetFirewallRule -DisplayName "Hound MCP" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    $remote = if ($AllowFrom) { $AllowFrom -split "\s*,\s*" } else { @("LocalSubnet") }
    New-NetFirewallRule -DisplayName "Hound MCP" -Direction Inbound -Protocol TCP -LocalPort $Port `
        -RemoteAddress $remote -Action Allow -Profile Domain, Private | Out-Null
    Info "Puerto $Port abierto para: $($remote -join ', ')"
    if (-not $AllowFrom) {
        Warn "No has indicado -AllowFrom: solo se permite la subred local. Para la VPN, vuelve a ejecutar con -AllowFrom <red de la VPN>, p. ej. 10.8.0.0/24."
    }
}

Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue

Step "Comprobacion"
& $hound --config $config doctor

Write-Host ""
Write-Host "Hound instalado." -ForegroundColor Green
Write-Host "  Configuracion: $config"
Write-Host "  Registros:     $DataDir\logs"
Write-Host "  El indice se esta construyendo en segundo plano. Progreso: hound status"
Write-Host ""
Write-Host "Siguiente paso: crea un token para cada usuario (abre una consola nueva para tener 'hound' en el PATH):"
Write-Host "  hound token create USUARIO --host $env:COMPUTERNAME"
