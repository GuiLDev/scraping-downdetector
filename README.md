# Downdetector Banking Monitor

Dashboard local que mostra, para uma lista configurável de bancos, carteiras e infraestrutura (como o Pix), os indicadores de instabilidade do Downdetector, com histórico e transições de estado. Apoia o time na leitura rápida de incidentes e nos comunicados.

> **Os indicadores são do Downdetector e não são confirmação oficial de indisponibilidade** de nenhuma instituição.

## Status atual

- **Etapa 1 (concluída): dashboard, banco, CLI e agendamento, com dados de demonstração.** Tudo o que aparece hoje é sintético e vem marcado como "DADOS DE DEMONSTRAÇÃO". Nenhum dado do Downdetector é coletado.
- **Etapa 2 (coletor real): pendente de autorização do Downdetector.** O site responde 403 com desafio do Cloudflare para clientes automatizados, e este projeto **não contorna proteções** (sem evasão de Cloudflare/CAPTCHA, sem navegador headless para burlar bloqueio, sem proxy rotativo, sem reaproveitar cookies de terceiros). O coletor real só será implementado por um canal autorizado. Detalhes em [`docs/etapa2-viabilidade-coletor.md`](docs/etapa2-viabilidade-coletor.md).

## Requisitos e instalação

Python 3.12 ou superior (testado com 3.14). Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.lock
.venv\Scripts\python -m pip install -e . --no-deps --no-build-isolation

.venv\Scripts\python -P -m bankmon init-db
.venv\Scripts\python -P -m bankmon check-config
.venv\Scripts\python -P -m bankmon seed-demo      # histórico sintético de 7 dias
.venv\Scripts\python -P -m bankmon serve          # http://127.0.0.1:8765/
```

Linux: o mesmo com `.venv/bin/python`. Todas as dependências, inclusive pytest e as ferramentas de build (setuptools), vêm de `requirements.lock` com hashes; por isso o projeto é instalado com `--no-deps --no-build-isolation` (nada é baixado fora do lock). Para regenerar o lock:

```
pip-compile --allow-unsafe --generate-hashes --extra dev --all-build-deps --output-file=requirements.lock pyproject.toml
```

**Windows: mantenha o projeto dentro de `%USERPROFILE%`.** Fora do perfil (por exemplo `C:\bankmon`), o diretório costuma herdar permissão de escrita para "Usuários Autenticados", e outro usuário da máquina poderia trocar o `.venv` ou o código que a tarefa agendada executa.

Os comandos agendados usam `python -P -m bankmon ...`: `-P` (Python 3.11+) impede que a pasta de trabalho entre em `sys.path`, evitando que um arquivo como `random.py` na raiz sobreponha a biblioteca padrão. Use `-P` também nos seus comandos manuais, se quiser a mesma proteção.

## Comandos da CLI

`python -P -m bankmon <comando>` (ou `bankmon <comando>` com o venv ativo):

| Comando | O que faz |
|---|---|
| `serve` | Sobe o dashboard em `server.host:server.port` (padrão `127.0.0.1:8765`). |
| `init-db` | Cria o banco e sincroniza categorias e instituições a partir do TOML. |
| `check-config` | Valida o TOML e mostra um resumo. Não cria banco nem logs. |
| `collect [--trigger scheduled\|cli]` | Executa uma coleta com o coletor ativo e grava a execução, mesmo em caso de falha. Usa lock para não sobrepor execuções. |
| `status` | Mostra no terminal o estado atual de cada instituição. |
| `seed-demo [--days N]` | Gera histórico sintético (só com o coletor `demo`). |
| `backup` | Copia o banco para `data/backups/` sem sobrescrever backups existentes. |

Códigos de saída:

| Código | Significado |
|---|---|
| 0 | Sucesso. |
| 1 | Falha da execução ou do comando (coleta com fonte inacessível/erro, banco inválido, `seed-demo` fora do modo demo). |
| 2 | Coleta já em andamento (lock ocupado) ou erro de uso da linha de comando. |
| 3 | Configuração inválida (`config/institutions.toml`). |

## Configuração

Arquivo: [`config/institutions.toml`](config/institutions.toml). Valide sempre com `check-config` e rode `init-db` depois de alterar.

- `[collector] active`: `"demo"` (sintético) ou `"downdetector"` (stub da Etapa 2).
- `[schedule] interval_minutes`: intervalo esperado entre coletas (60).
- `[schedule] stale_after_minutes`: depois desse tempo sem coleta válida, tudo vira "Sem dados" (90; deve ficar entre `interval_minutes` e 4x o intervalo, no máximo 1440).
- `[server] host/port`: só IP literal de loopback (`127.0.0.1` ou `::1`); outro valor é recusado.
- `[[categories]]`: `id`, `name`, `order`.
- `[[institutions]]`: `id` (minúsculas, dígitos, `-`, `_`), `name`, `category`, `dd_label` (nome no Downdetector) e `enabled`.

**Adicionar** uma instituição: inclua um bloco `[[institutions]]` com uma categoria existente, rode `check-config` e `init-db`. **Remover**: apague o bloco, ou use `enabled = false` para parar de monitorar mantendo o histórico.

Variáveis de ambiente (opcionais, sem segredos; veja `.env.example`):

- `BANKMON_CONFIG`: caminho alternativo do TOML.
- `BANKMON_DATA_DIR`: diretório de dados (padrão `data/`).

**Demo × real:** os bancos são separados (`data/bankmon-demo.db` no modo demo, `data/bankmon.db` no modo real), então dados sintéticos nunca se misturam com dados reais.

## Estados

| Estado | Significado |
|---|---|
| Crítico | O Downdetector indica problema crítico para a instituição. |
| Atenção | O Downdetector indica sinais de problema. |
| Sem alerta | Coleta válida e recente, sem alerta para a instituição. |
| Sem dados | Não há coleta válida e recente (fonte falhou ou dado vencido), ou a instituição não veio na coleta. |
| Não monitorado | A instituição não está listada no Downdetector. |

Regras:

- **Nunca verde sem dado válido:** "Sem alerta" só aparece com uma coleta bem-sucedida dentro de `stale_after_minutes`. Se a execução mais recente falhou, todas as instituições ficam "Sem dados".
- **"Indisponível" só para a fonte:** a indisponibilidade do Downdetector (inacessível, não autorizado, erro) aparece como aviso sobre a fonte, nunca como estado de um banco.
- As cores e rótulos são **indicadores do Downdetector, não confirmação oficial**. Confirme em canais oficiais antes de comunicar um incidente.

## Dashboard

Somente leitura, em `http://127.0.0.1:8765/`:

- `/`: visão geral com cartões agrupados em "Com alerta", "Sem alerta identificado" e "Sem cobertura". Filtros: `category`, `state` e busca `q` por nome.
- `/history`: histórico de transições e linha do tempo. Filtro de período: `24h`, `7d` ou `30d`.
- `/institution/<id>`: detalhe de uma instituição, com o mesmo filtro de período.

O servidor não tem JavaScript externo nem recursos de terceiros (bibliotecas e fontes ficam em `static/`, ver `static/VENDORED.md`).

## Agendamento

Runbook completo em [`docs/operacao.md`](docs/operacao.md).

**Windows** (Agendador de Tarefas, usuário atual, só com o usuário logado e sem armazenar senha):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\register-tasks.ps1 -DryRun   # só mostra o que faria
powershell -ExecutionPolicy Bypass -File .\scripts\windows\register-tasks.ps1           # cria/atualiza BankMonitor-Collect e BankMonitor-Dashboard
Get-ScheduledTask -TaskPath '\' -TaskName 'BankMonitor-*' | Get-ScheduledTaskInfo   # conferir
.\scripts\windows\unregister-tasks.ps1          # remover
```

O `-ExecutionPolicy Bypass` vale só para essa execução; não altere a política da máquina. O script recusa um projeto fora de `%USERPROFILE%` (há `-AllowOutsideProfile`, com aviso) e avisa se a ACL do `.venv` der escrita a `Users`, `Authenticated Users` ou `Everyone`.

A coleta roda a cada 1 h e o dashboard sobe no logon, ambos com `pythonw.exe` (sem janela). Sem o Agendador: `scripts\windows\start-dashboard.cmd`.

**Linux** (systemd `--user`, instalado em `~/bankmon`):

```bash
cp scripts/linux/bankmon-*.service scripts/linux/bankmon-collect.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bankmon-collect.timer bankmon-serve.service
loginctl enable-linger "$USER"    # para rodar sem sessão aberta
```

## Backup e logs

- Backups: `python -P -m bankmon backup` grava em `data/backups/`.
- Logs: `data/logs/bankmon.log` (rotativo). Com `pythonw` não há saída de console; consulte esse arquivo.
- `data/` está no `.gitignore`: bancos, logs e backups nunca vão para o repositório.

## Segurança

- O servidor escuta **somente em loopback** e **não tem autenticação**. **Não exponha** a porta em rede (sem port-forward, proxy reverso ou `host` diferente de loopback).
- O dashboard envia Content-Security-Policy restritiva (`default-src 'self'`, sem scripts ou estilos inline, sem frames e sem formulários), autoescape do Jinja e SQL sempre parametrizado.
- A Etapa 1 não usa nenhum segredo. `.env` fica fora do git (`.gitignore`); `.env.example` só documenta nomes de variáveis. Se a Etapa 2 exigir credenciais de um canal autorizado, elas entram por variável de ambiente, nunca no código.
- Textos vindos de coletores são sanitizados antes de gravar ou exibir.

## Estrutura do projeto

```
config/institutions.toml   configuração (categorias, instituições, agenda)
src/bankmon/               cli, config, db, runner (lock), status (regras), collectors/, web/
scripts/windows/           register-tasks.ps1, unregister-tasks.ps1, start-dashboard.cmd
scripts/linux/             unidades systemd --user (collect service/timer, serve service)
docs/                      operação e viabilidade da Etapa 2
tests/                     testes (pytest)
data/                      banco, logs e backups (ignorado pelo git)
.claude/agents/            agentes do projeto
```

Agentes do projeto (`.claude/agents/`): `scraping-specialist` (Etapa 2, só por canal autorizado), `incident-analyzer` (regras de estado, transições e resumos para comunicados) e `dashboard-specialist` (interface).

## Testes

```powershell
.venv\Scripts\python -m pytest
```

Os testes usam diretórios temporários e nunca tocam em `data/`. O pytest já vem de `requirements.lock`.

## Limitações e próximos passos

- Só há dados de demonstração; o coletor real depende de autorização do Downdetector (Etapa 2).
- Uma única máquina, um usuário, sem autenticação: não é um serviço para rede.
- No Windows, com o PC desligado ou suspenso não há coleta; as horas perdidas ficam como lacuna.
- Próximos passos: coletor autorizado, confirmação dos `dd_label` reais e, se necessário, alertas e controle de acesso.
