# Instala o agente no PC (executar como Administrador, dentro da pasta dist\CameraDVR).
#   powershell -ExecutionPolicy Bypass -File instalar.ps1 -Servidor https://licencas.seudominio.com.br [-Chave CDVR-...]
# Abre o assistente: chave de licença -> leitura e ACEITE dos Termos de Uso (nome, CPF, cargo) -> dados do DVR.
param(
    [string]$Chave = "",
    [Parameter(Mandatory = $true)][string]$Servidor
)
$ErrorActionPreference = "Stop"
$dest = "$env:ProgramFiles\CameraDVR"

New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Recurse -Force "$PSScriptRoot\*" $dest

# Libera as portas da "câmera" na rede local (RTSP, ONVIF e descoberta automática).
New-NetFirewallRule -DisplayName "CameraDVR RTSP"  -Direction Inbound -Protocol TCP -LocalPort 8554 -Profile Domain,Private -Action Allow -ErrorAction SilentlyContinue | Out-Null
New-NetFirewallRule -DisplayName "CameraDVR ONVIF" -Direction Inbound -Protocol TCP -LocalPort 8000 -Profile Domain,Private -Action Allow -ErrorAction SilentlyContinue | Out-Null
New-NetFirewallRule -DisplayName "CameraDVR Descoberta" -Direction Inbound -Protocol UDP -LocalPort 3702 -Profile Domain,Private -Action Allow -ErrorAction SilentlyContinue | Out-Null

# O agente roda na sessão do usuário e precisa atualizar o estado da licença.
$data = "$env:ProgramData\CameraDVR"
New-Item -ItemType Directory -Force -Path $data | Out-Null
icacls $data /grant "*S-1-5-32-545:(OI)(CI)M" /T | Out-Null

$argv = @("--servidor", $Servidor, "--instalar")
if ($Chave) { $argv += @("--chave", $Chave) }
$p = Start-Process -FilePath "$dest\CameraDVR.exe" -ArgumentList $argv -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "Instalação não concluída (termos não aceitos ou licença não ativada)." }

# A captura de tela precisa rodar na sessão do usuário logado (serviços do Windows não enxergam a tela),
# por isso usamos uma tarefa agendada "ao fazer logon" de qualquer usuário.
$action    = New-ScheduledTaskAction -Execute "$dest\CameraDVR.exe"
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Highest   # grupo Usuários
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "CameraDVR" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Instalado. O agente inicia agora e a cada logon."
Start-ScheduledTask -TaskName "CameraDVR"
