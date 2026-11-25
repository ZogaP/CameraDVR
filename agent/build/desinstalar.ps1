# Remove o agente (executar como Administrador).
$ErrorActionPreference = "SilentlyContinue"
Stop-ScheduledTask -TaskName "CameraDVR"
Unregister-ScheduledTask -TaskName "CameraDVR" -Confirm:$false
Get-Process CameraDVR, ffmpeg, mediamtx | Where-Object { $_.Path -like "$env:ProgramFiles\CameraDVR\*" } | Stop-Process -Force
Remove-NetFirewallRule -DisplayName "CameraDVR RTSP", "CameraDVR ONVIF", "CameraDVR Descoberta"
Remove-Item -Recurse -Force "$env:ProgramFiles\CameraDVR"
Write-Host "CameraDVR removido. Dados locais em $env:ProgramData\CameraDVR (apague se desejar)."
