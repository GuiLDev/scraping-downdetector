# Operação (agendamento, logs e backup)

Runbook curto. O resumo de instalação está no [README](../README.md).

## Windows (Agendador de Tarefas)

Registrar (usuário atual, só com o usuário logado; não armazena senha):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\register-tasks.ps1 -DryRun   # mostra o que faria
powershell -ExecutionPolicy Bypass -File .\scripts\windows\register-tasks.ps1           # registra ou atualiza
```

| Tarefa | Ação | Gatilho |
|---|---|---|
| `BankMonitor-Collect` | `.venv\Scripts\pythonw.exe -P -m bankmon collect --trigger scheduled` | a cada 1 h, a partir do próximo minuto cheio |
| `BankMonitor-Dashboard` | `.venv\Scripts\pythonw.exe -P -m bankmon serve` | no logon |

- Ambas têm `WorkingDirectory` na raiz do projeto.
- A coleta usa `MultipleInstances=IgnoreNew`, limite de 30 min e `StartWhenAvailable` (executa depois, se o PC estava desligado ou suspenso na hora marcada).
- Usa-se `pythonw.exe` (sem janela). Por isso não há saída de console: consulte `data\logs\bankmon.log`, `python -P -m bankmon status` e `LastTaskResult`.
- `-P` (Python 3.11+) tira a pasta de trabalho de `sys.path`: sem ele um `random.py` ou `logging.py` na raiz do projeto sobreporia a biblioteca padrão.
- O projeto deve ficar dentro de `%USERPROFILE%`: o script recusa outro local (exceto `-AllowOutsideProfile`, com aviso) e avisa se o `.venv` der escrita a `Users`, `Authenticated Users` ou `Everyone`. As tarefas ficam na raiz (`-TaskPath '\'`).
- Conferir: `Get-ScheduledTask -TaskPath '\' -TaskName 'BankMonitor-*' | Get-ScheduledTaskInfo`.
- Rodar uma coleta agora: `Start-ScheduledTask -TaskName BankMonitor-Collect`.
- Remover: `.\scripts\windows\unregister-tasks.ps1` (aceita `-DryRun`).
- Sem Agendador: `scripts\windows\start-dashboard.cmd`.

Limitação: o PC precisa estar ligado e o usuário logado. Em suspensão a coleta não roda; ao acordar, `StartWhenAvailable` dispara uma execução atrasada (as horas perdidas ficam sem dados e aparecem como lacuna).

## Linux (systemd --user)

Caminho de instalação assumido: `~/bankmon` (as unidades usam `%h/bankmon`). Em outro caminho, edite `WorkingDirectory`, `ExecStart` e `ReadWritePaths` nas três unidades.

```bash
cd ~/bankmon
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install -e . --no-deps --no-build-isolation
.venv/bin/python -P -m bankmon init-db        # cria data/ antes de iniciar as unidades

mkdir -p ~/.config/systemd/user
cp scripts/linux/bankmon-*.service scripts/linux/bankmon-collect.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bankmon-collect.timer bankmon-serve.service
loginctl enable-linger "$USER"              # roda sem sessão aberta (após boot)
```

Conferir: `systemctl --user list-timers bankmon-collect.timer`, `systemctl --user status bankmon-serve`, `journalctl --user -u bankmon-collect`.
Remover: `systemctl --user disable --now bankmon-collect.timer bankmon-serve.service` e apagar as unidades.

### Hardening e limitações

Diretivas aplicadas nas unidades: `UMask=0077`, `NoNewPrivileges=yes`, `PrivateTmp=yes`, `PrivateUsers=yes`, `ProtectSystem=strict` (sistema de arquivos somente leitura, exceto `data/` via `ReadWritePaths`), `SystemCallFilter=@system-service` e `RestrictAddressFamilies` (`AF_UNIX` no collect, porque a Etapa 1 não usa rede; `AF_UNIX AF_INET AF_INET6` no serve). O serve ainda limita reinícios (`StartLimitIntervalSec=600`, `StartLimitBurst=5`).

- Em unidades `--user`, `ProtectSystem=` e afins só funcionam junto com `PrivateUsers=yes` (systemd.exec(5)); por isso ele está nas duas unidades.
- **Etapa 2:** o coletor real precisará de rede; troque o `RestrictAddressFamilies` do collect para `AF_UNIX AF_INET AF_INET6`.
- `data/` precisa existir antes de iniciar (o `-` em `ReadWritePaths=-...` só evita falha de montagem). `BANKMON_DATA_DIR` fora de `~/bankmon/data` exige ajustar `ReadWritePaths`.
- Conferir a exposição: `systemd-analyze --user security bankmon-serve.service` (e `bankmon-collect.service`).
- Fallback: se a unidade falhar com `status=226/NAMESPACE` (kernel sem user namespaces não privilegiados) ou com erro de diretiva desconhecida (systemd antigo), remova as diretivas não suportadas, começando por `ProtectSystem`, `PrivateUsers`, `PrivateTmp` e `ReadWritePaths`, e depois `SystemCallFilter` e `RestrictAddressFamilies`. Mantenha `NoNewPrivileges` e `UMask`.
- Diretivas que exigem privilégio (`ProtectHome`, `CapabilityBoundingSet` em alguns casos) foram omitidas: não são confiáveis em unidades de usuário.
- As unidades não foram executadas em Linux nesta fase; valide com `systemd-analyze --user verify` e `security`.

## Logs e backup

- Logs: `data/logs/bankmon.log` (rotativo, 1 MB x 5). Não contêm segredos.
- Backup do banco: `python -P -m bankmon backup` grava `data/backups/<banco>-<UTC>.db` (cópia consistente via API de backup do SQLite; nunca sobrescreve). Recomenda-se agendar semanalmente ou antes de atualizações.
- Restaurar: pare o dashboard e a coleta, copie o backup sobre `data/bankmon-demo.db` (ou `bankmon.db`) e reinicie.
