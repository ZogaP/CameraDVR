# Gera o pacote do agente para Windows (rodar em uma máquina Windows com Python 3.11+).
#   powershell -ExecutionPolicy Bypass -File build\build.ps1
# Saída: dist\CameraDVR\ (CameraDVR.exe + bin\ffmpeg.exe + bin\mediamtx.exe + instalar.ps1)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$MediaMtxVersion = "v1.12.2"

if (-not (Test-Path "cameradvr_agent\license_public.pem")) {
    throw "Falta cameradvr_agent\license_public.pem (gere com server\scripts\gen_keys.py e copie a chave PUBLICA)."
}

python -m pip install --upgrade pip pyinstaller -r requirements.txt

New-Item -ItemType Directory -Force -Path bin, build\tmp | Out-Null

if (-not (Test-Path "bin\mediamtx.exe")) {
    $zip = "build\tmp\mediamtx.zip"
    Invoke-WebRequest "https://github.com/bluenviron/mediamtx/releases/download/$MediaMtxVersion/mediamtx_${MediaMtxVersion}_windows_amd64.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath build\tmp\mediamtx -Force
    Copy-Item build\tmp\mediamtx\mediamtx.exe bin\
}
if (-not (Test-Path "bin\ffmpeg.exe")) {
    # Build LGPL/GPL "essentials" do gyan.dev. Verifique a licença antes de redistribuir comercialmente.
    $zip = "build\tmp\ffmpeg.zip"
    Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath build\tmp\ffmpeg -Force
    Get-ChildItem build\tmp\ffmpeg -Recurse -Filter ffmpeg.exe | Select-Object -First 1 | Copy-Item -Destination bin\
}

pyinstaller --noconfirm --clean --name CameraDVR --noconsole `
    --add-data "cameradvr_agent\license_public.pem;cameradvr_agent" `
    --hidden-import pynput.mouse._win32 --hidden-import pystray._win32 `
    cameradvr_agent\__main__.py

Copy-Item -Recurse -Force bin dist\CameraDVR\
Copy-Item build\instalar.ps1, build\desinstalar.ps1 dist\CameraDVR\
Write-Host "Pronto: dist\CameraDVR"
