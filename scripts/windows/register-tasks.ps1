<#
.SYNOPSIS
  Registra as tarefas do Bank Monitor no Agendador de Tarefas do usuario atual.

.DESCRIPTION
  Cria (ou atualiza, se ja existirem) duas tarefas:
    BankMonitor-Collect    coleta a cada 1 h: pythonw -P -m bankmon collect --trigger scheduled
    BankMonitor-Dashboard  dashboard no logon: pythonw -P -m bankmon serve

  Decisoes:
  - Executa so com o usuario logado (LogonType Interactive, RunLevel Limited). NAO usa
    "executar sem login": isso exigiria armazenar a senha do usuario. Sem privilegios elevados.
  - Usa pythonw.exe (sem console) em vez de python.exe com a tarefa oculta: nao ha janela
    piscando nem dependencia da opcao "Hidden". O custo e nao haver saida no console; o
    resultado fica em data\logs\bankmon.log, no banco e em LastTaskResult (Get-ScheduledTaskInfo).
  - Arquivo mantido em ASCII puro para o Windows PowerShell 5.1 nao corromper acentos.

.PARAMETER ProjectRoot
  Raiz do projeto (padrao: duas pastas acima deste script).

.PARAMETER DryRun
  So imprime o que seria feito; nao registra nada.

.EXAMPLE
  .\register-tasks.ps1 -DryRun
  .\register-tasks.ps1
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [switch]$DryRun,
    # Permite um ProjectRoot fora de %USERPROFILE% (nao recomendado: ver README).
    [switch]$AllowOutsideProfile
)

$ErrorActionPreference = 'Stop'

$CollectTask = 'BankMonitor-Collect'
$DashboardTask = 'BankMonitor-Dashboard'

# $PSScriptRoot em valor padrao de parametro vem vazio com `powershell -File` (PS 5.1): resolve aqui.
if (-not $ProjectRoot) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $ProjectRoot = Join-Path $scriptDir '..\..'
}
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "ProjectRoot nao encontrado: $ProjectRoot"
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath

# O projeto deve ficar dentro do perfil do usuario: fora dele (ex.: C:\bankmon) o diretorio costuma
# herdar escrita para "Usuarios Autenticados", o que permitiria trocar o .venv ou o codigo que a
# tarefa executa (escalada de privilegio local dentro da conta do usuario).
$profileRoot = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') + '\'
$rootFull = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') + '\'
if (-not $rootFull.StartsWith($profileRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    if (-not $AllowOutsideProfile) {
        throw "ProjectRoot ($ProjectRoot) esta fora de $env:USERPROFILE. Mova o projeto para dentro do perfil ou use -AllowOutsideProfile sabendo dos riscos."
    }
    Write-Warning "ProjectRoot fora de $env:USERPROFILE (-AllowOutsideProfile): confirme que apenas voce tem escrita em $ProjectRoot."
}

$pythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
$config = Join-Path $ProjectRoot 'config\institutions.toml'

# Validacoes antes de qualquer registro.
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "Ambiente virtual nao encontrado: $pythonw. Crie o .venv e instale as dependencias (ver README)."
}
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    throw "Configuracao nao encontrada: $config"
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
# -P: nao poe o diretorio de trabalho (a raiz do projeto) em sys.path[0]; impede que um
# random.py ou logging.py na raiz sobreponha a biblioteca padrao.
$collectArgs = '-P -m bankmon collect --trigger scheduled'
$serveArgs = '-P -m bankmon serve'

# Proximo minuto cheio.
$now = Get-Date
$start = $now.Date.AddHours($now.Hour).AddMinutes($now.Minute + 1)

Write-Host "Projeto : $ProjectRoot"
Write-Host "Usuario : $user (somente com o usuario logado; sem senha armazenada)"
Write-Host ""
Write-Host "Tarefa: $CollectTask"
Write-Host "  Acao      : $pythonw $collectArgs"
Write-Host "  Diretorio : $ProjectRoot"
Write-Host ("  Gatilho   : a cada 1 hora, por tempo indeterminado, a partir de {0:yyyy-MM-dd HH:mm}" -f $start)
Write-Host "  Opcoes    : MultipleInstances=IgnoreNew, ExecutionTimeLimit=30 min, StartWhenAvailable"
Write-Host "Tarefa: $DashboardTask"
Write-Host "  Acao      : $pythonw $serveArgs"
Write-Host "  Diretorio : $ProjectRoot"
Write-Host "  Gatilho   : no logon de $user"
Write-Host "  Opcoes    : MultipleInstances=IgnoreNew, sem limite de tempo, reinicia ate 3x se falhar"
Write-Host ""

if ($DryRun) {
    Write-Host "[DryRun] Nada foi registrado."
    return
}

# Alerta se o .venv der escrita a grupos amplos (permitiria trocar o interpretador executado).
$venvDir = Join-Path $ProjectRoot '.venv'
$broad = 'Users', 'Authenticated Users', 'Everyone'
$writeRights = [System.Security.AccessControl.FileSystemRights]'WriteData, AppendData, Modify, FullControl'
foreach ($ace in (Get-Acl -LiteralPath $venvDir).Access) {
    $who = $ace.IdentityReference.Value.Split('\')[-1]
    if ($ace.AccessControlType -eq 'Allow' -and ($broad -contains $who) -and (($ace.FileSystemRights -band $writeRights) -ne 0)) {
        Write-Warning "ACL do .venv da escrita a '$($ace.IdentityReference.Value)'. Restrinja antes de usar em producao."
    }
}

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

# --- Coleta horaria ---
$collectAction = New-ScheduledTaskAction -Execute "`"$pythonw`"" -Argument $collectArgs -WorkingDirectory $ProjectRoot
$collectTrigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
$collectSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# --- Dashboard no logon ---
$serveAction = New-ScheduledTaskAction -Execute "`"$pythonw`"" -Argument $serveArgs -WorkingDirectory $ProjectRoot
$serveTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$serveSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# -Force: se a tarefa ja existir, e atualizada (idempotente).
Register-ScheduledTask -TaskPath '\' -TaskName $CollectTask -Action $collectAction -Trigger $collectTrigger `
    -Settings $collectSettings -Principal $principal -Force `
    -Description 'Downdetector Banking Monitor: coleta horaria' | Out-Null
Register-ScheduledTask -TaskPath '\' -TaskName $DashboardTask -Action $serveAction -Trigger $serveTrigger `
    -Settings $serveSettings -Principal $principal -Force `
    -Description 'Downdetector Banking Monitor: dashboard local (127.0.0.1)' | Out-Null

Write-Host "Tarefas registradas."
Write-Host ""
Write-Host "Conferir:"
Write-Host "  Get-ScheduledTask -TaskPath '\' -TaskName 'BankMonitor-*' | Get-ScheduledTaskInfo"
Write-Host "  Start-ScheduledTask -TaskName $CollectTask    # roda uma coleta agora"
Write-Host "Remover:"
Write-Host "  .\scripts\windows\unregister-tasks.ps1"
