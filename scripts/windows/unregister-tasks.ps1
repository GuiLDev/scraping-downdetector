<#
.SYNOPSIS
  Remove as tarefas BankMonitor-Collect e BankMonitor-Dashboard do Agendador de Tarefas.

.PARAMETER DryRun
  So imprime o que seria removido.
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

foreach ($name in 'BankMonitor-Collect', 'BankMonitor-Dashboard') {
    if ($DryRun) {
        Write-Host "[DryRun] Removeria a tarefa: $name"
        continue
    }
    $task = Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Tarefa inexistente (nada a fazer): $name"
        continue
    }
    Unregister-ScheduledTask -TaskPath '\' -TaskName $name -Confirm:$false
    Write-Host "Tarefa removida: $name"
}
if ($DryRun) { Write-Host "[DryRun] Nada foi removido." }
