# Downdetector Banking Monitor

Dashboard local que mostra, para uma lista configurável de bancos, carteiras e infraestrutura (como o Pix), os indicadores de instabilidade do Downdetector, com histórico e transições de estado. Apoia o time na leitura rápida de incidentes e nos comunicados.

> **Os indicadores são do Downdetector e não são confirmação oficial de indisponibilidade** de nenhuma instituição.

## Status atual

- **Etapa 1 (concluída): dashboard, banco, CLI e agendamento, com dados de demonstração.** No modo padrão tudo é sintético e vem marcado como "DADOS DE DEMONSTRAÇÃO"; nenhum dado do Downdetector é coletado.
- **Importação offline de HTML salvo manualmente** (`import-html`, banco separado `bankmon-offline.db`): o parser lê um arquivo local que você mesmo salvou; não há acesso à rede.
- **Coleta pela aba do navegador (extensão), hoje só para origens locais configuradas** (páginas de teste em loopback, listadas em `[coleta_navegador]`). Nenhuma origem externa está autorizada.
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

Para os testes E2E (Playwright com o Edge instalado, `channel="msedge"`), instale também o lock próprio, com hashes:

```powershell
.venv\Scripts\python -m pip install --require-hashes -r requirements-e2e.lock
```

## Comandos da CLI

`python -P -m bankmon <comando>` (ou `bankmon <comando>` com o venv ativo):

| Comando | O que faz |
|---|---|
| `serve [--offline\|--navegador] [--abrir]` | Sobe o dashboard em `server.host:server.port` (padrão `127.0.0.1:8765`). `--navegador` liga a API da coleta pela aba do navegador. `--abrir` abre o dashboard no navegador padrão (só a URL de loopback do próprio servidor). Se a porta já estiver em uso, recusa com `Porta N já está em uso (outro BankMonitor aberto?)` (código de saída 1), em qualquer modo. |
| `extensao --alvo URL [--intervalo MIN \| --pontual]` | Gera a extensão do navegador pronta para carregar em `data/extensao-coletor/` (`--alvo` obrigatório, origem listada em `[coleta_navegador]`). |
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
| Sem dados | Não há coleta válida e recente (fonte falhou ou dado vencido), a instituição não veio na coleta, ou ela está configurada mas **não aparece na página inicial** do Downdetector (não listada). Ausência nunca significa normalidade. |
| Não monitorado | Reservado para o que está fora do escopo de monitoramento; não é usado para instituição ausente da página. |

Regras:

- **Nunca verde sem dado válido:** "Sem alerta" só aparece com uma coleta bem-sucedida dentro de `stale_after_minutes`. Se a execução mais recente falhou, todas as instituições ficam "Sem dados".
- **"Indisponível" só para a fonte:** a indisponibilidade do Downdetector (inacessível, não autorizado, erro) aparece como aviso sobre a fonte, nunca como estado de um banco.
- As cores e rótulos são **indicadores do Downdetector, não confirmação oficial**. Confirme em canais oficiais antes de comunicar um incidente.

## Dashboard

Somente leitura, em `http://127.0.0.1:8765/`:

- `/`: visão geral com cartões agrupados em "Com alerta", "Sem alerta identificado" e "Sem cobertura". Filtros: `category`, `state` e busca `q` por nome.
- `/history`: histórico de transições e linha do tempo. Filtro de período: `24h`, `7d` ou `30d`.
- `/institution/<id>`: detalhe de uma instituição, com o mesmo filtro de período.

**Dashboard ao vivo.** A página `/` confere a cada 20 s, em `GET /painel/versao` (devolve só `{"versao": "<id>-<epoch>"}` da última execução, um identificador opaco, somente leitura, sem token e sem criar o banco), se há uma execução nova e só então recarrega; abas ocultas pulam a conferência. O `<meta refresh>` de 5 minutos continua como reserva (inclusive sem JavaScript).

O servidor não tem JavaScript externo nem recursos de terceiros (bibliotecas e fontes ficam em `static/`, ver `static/VENDORED.md`).

### Validação offline (HTML exportado manualmente)

Para conferir o parser com dados reais **sem coleta automatizada**, importe um HTML da página inicial do Downdetector salvo manualmente pelo navegador:

```powershell
.venv\Scripts\python -P -m bankmon import-html C:\caminho\pagina.html [--capturado-em 2026-10-28T10:53:00-03:00]
.venv\Scripts\python -P -m bankmon status --offline
.venv\Scripts\python -P -m bankmon serve --offline
```

- Os dados vão para um banco **separado**, `data/bankmon-offline.db`. O banco demo e o operacional não são tocados.
- O horário da execução é o **horário da captura**: a data de modificação do arquivo, ou `--capturado-em` (com fuso obrigatório).
- No modo `--offline`, o relógio fica congelado nessa captura. A dashboard mostra a faixa "DADOS HISTÓRICOS DE VALIDAÇÃO OFFLINE". Não é monitoramento ao vivo.
- A mesma página (mesmo SHA-256) não é importada duas vezes.
- Todos os serviços da página ficam em `run_services`, com a geometria SVG validada e sem o `stroke` cru. As instituições configuradas são localizadas pelo `dd_label`.
- O arquivo HTML real **não** deve entrar no git (`.gitignore` cobre `downdetector*.html` e HTML na raiz).

### Coleta pela aba do navegador (extensão, com intervenção humana)

Extensão → servidor Flask → parser → SQLite → dashboard, **sem robô de rede**: a extensão só recarrega e lê a aba que **você** abriu e envia o HTML ao servidor local. Os dados vão para `data/bankmon-navegador.db` (os demais bancos não são tocados).

```powershell
.venv\Scripts\python -P -m bankmon extensao --alvo http://127.0.0.1:8777/dd --intervalo 5
.venv\Scripts\python -P -m bankmon serve --navegador
```

1. Rode `extensao`: ela cria o token em `data/coleta-token.txt` (reaproveitado nas próximas vezes) e escreve a extensão em `data/extensao-coletor/` (`manifest.json`, `background.js` e `config.json` com servidor, token, alvo e intervalo). `data/` é ignorado pelo git; o modelo versionado em `extension/coletor/` não tem segredos.
2. No Opera GX, Edge ou Chrome, abra a página de extensões, ative o modo desenvolvedor e use "Carregar sem pacote" (Opera: "Carregar descompactada") apontando para `data/extensao-coletor/`.
3. Abra a aba do alvo e rode `serve --navegador`. A cada intervalo (mínimo 0,5 min) a extensão consulta o estado, **reserva a vaga no servidor** (que aplica o intervalo mínimo) e só então recarrega a aba **sem trazê-la para a frente**, espera a página estabilizar (até 45 s) e envia o HTML. Funciona sem o Claude Code rodando. Para testar sem o Downdetector, use `python tests/e2e/sim_server.py` (páginas de simulação em `http://127.0.0.1:8777/dd`; modos em `/_sim/modo?valor=normal_a|normal_b|bloqueio`; a página `bloqueio` tem o botão "Sou humano (simulação)", cujo clique a devolve ao conteúdo normal; `/_sim/contagem` informa quantas vezes a página foi pedida).

**Origens autorizadas (lista explícita).** Não há origem implícita: só valem as origens **exatas** (`esquema://host:porta`, sem curinga) listadas em `[coleta_navegador] origens_autorizadas`. Sem a lista, `serve --navegador` não sobe (erro de configuração), `extensao` recusa o alvo e a API responde 403. Origem loopback dispensa `autorizacao_ref`; origem externa exige um identificador registrado (veja abaixo). `config/institutions.toml` não traz a seção: crie-a no TOML que você usa (por exemplo, via `BANKMON_CONFIG`).

```toml
[coleta_navegador]
origens_autorizadas = ["http://127.0.0.1:8777"]   # páginas de teste locais
intervalo_minimo_local_s = 30                     # intervalo mínimo entre recargas em loopback (padrão 30; 0 nos testes)
# origem externa (somente com autorização registrada):
# origens_autorizadas = ["https://exemplo.test"]
# autorizacao_ref = "identificador da autorização"
```

**Reserva antes da recarga (SEC-014).** `GET /api/coleta/estado?alvo=URL` (o `alvo` é obrigatório: 400 se faltar, 403 se não estiver na lista) só consulta: devolve `estado`, `pode_reservar` e `proxima_leitura_em`. Antes de recarregar, a extensão chama `POST /api/coleta/reserva` (`{alvo}`): o servidor, numa transação `BEGIN IMMEDIATE` sob o lock da coleta, responde **429** com `Retry-After` se o intervalo desde a última reserva não passou, **409** `reserva_ativa` se já há uma reserva aberta, **409** `pausado` se a coleta está pausada; senão devolve `{reserva_id, expira_em}` (validade de 2 min). Em 429 ou 409 a extensão **não recarrega**. `POST /api/coleta` exige o `reserva_id` (mesma origem, não consumido, não expirado) e o consome na mesma transação que grava a leitura; sem reserva válida, 429. O limite vem da tabela `collector_reservations` (schema v4), então sobrevive a reinícios. A cada nova reserva, as reservas consumidas ou expiradas da mesma origem são apagadas; fica só a mais recente, de onde o intervalo conta. Intervalo: origem externa, 60 min fixos; loopback, `intervalo_minimo_local_s`.

**Pausa e retomada (sem recarga).** Se a página vier como desafio de bloqueio, a execução é gravada como `inaccessible` e a coleta fica `pausada`: a dashboard avisa, mantém os cartões em "Sem dados" (com a "Última leitura válida (desatualizada)" só como texto) e a API recusa reservas e leituras (409). O estado é **por origem** (schema v6, tabela `collector_origin_state`): a pausa de uma origem não afeta as outras. Resolva o bloqueio **manualmente** na aba e clique em "Retomar verificação" da origem (a dashboard mostra um aviso por origem fora de `ativo` e um botão por origem retomável): isso só passa o estado dessa origem a `verificando`, **sem recarregar** a página. Nesse estado a extensão lê o DOM **já aberto** na aba, sem `tabs.reload` e sem reservar, e o envia com `modo: "verificacao"` (o servidor só aceita esse modo no estado `verificando` e só em origem loopback). DOM ok volta a `ativo`; ainda bloqueado volta a `pausada`. A recarga automática seguinte segue o fluxo normal e respeita o intervalo mínimo desde a última recarga. As leituras seguidas sem sucesso são contadas por origem num contador (`falhas_seguidas`), gravado na mesma transação da execução e da reserva consumida; ele zera num `ok` e na retomada e não depende do horário, então é exato mesmo quando bloqueio, retomada e leituras caem no mesmo segundo e sobrevive a reinícios. `serve --navegador` aplica essa migração aditiva na partida, se o banco já existir; um banco ilegível ou desatualizado nunca é lido como "ativo" (a API responde erro e a extensão não recarrega).

**Origem externa (travas de política).** Além de `autorizacao_ref`, que precisa ser um identificador listado em "Autorizações registradas" de `docs/etapa2-viabilidade-coletor.md` (hoje não há nenhum, então origem externa é sempre recusada), valem: a origem externa precisa ser a **única** da lista (sem loopback e sem outra externa); intervalo mínimo fixo de 60 min no `extensao` e no servidor (reserva recusada com 429 se a mesma origem foi reservada há menos de 1 h) e, se a fonte exibir um desafio, a coleta fica **suspensa** (`suspenso_por_desafio`): a dashboard não oferece retomada e `/coleta/retomar` responde 409. Essa suspensão **não volta pela dashboard nem trocando a origem externa** na configuração: ela vale para qualquer origem externa do mesmo banco (SEC-024). Reativar exige ação manual fora da aplicação (usar um banco novo) depois da revisão da autorização; não há comando de reset. Três leituras seguidas sem sucesso também pausam a coleta: em origem local (`falhas_consecutivas`) há retomada manual; em origem externa (`falhas_consecutivas_externa`) a coleta fica suspensa como no desafio (sem retomada, 409); nem trocar a origem externa a reativa, só um banco novo, depois da revisão da autorização. Com origem externa configurada, a retomada é sempre recusada, inclusive para uma pausa que sobrou de uma configuração loopback anterior no mesmo banco. A extensão informa o alvo ao servidor e, com 401/403, cancela o próprio alarme.

**API** (somente com `serve --navegador`; nos demais modos as rotas não existem): `POST /api/coleta/reserva`, `POST /api/coleta` e `GET /api/coleta/estado` exigem o cabeçalho `X-BankMonitor-Token`; `POST /coleta/retomar` usa token CSRF e recebe o campo `origem` (origem exata da lista, senão 403; vazio só vale com uma única origem configurada; origem externa recebe sempre 409). O token nunca é logado nem devolvido.

**O que a extensão nunca faz:** ler ou enviar cookies, interagir com a página, resolver CAPTCHA ou desafio, abrir/ativar abas ou janelas. Veja os comentários de `extension/coletor/background.js`.

### Leitura pontual com um clique (Windows)

A leitura pontual é uma **decisão interna (D1), não uma autorização do serviço**: uma leitura do DOM já carregado na aba aberta por você, acionada manualmente, sem recarga, sem agendamento e sem requisição da extensão ao site. Toda coleta periódica ou automática continua exigindo autorização registrada. Detalhes em [`docs/etapa2-viabilidade-coletor.md`](docs/etapa2-viabilidade-coletor.md), seção "Leitura pontual — decisão interna".

**Preparação (uma vez).** Crie `data\leitura-pontual\leitura.toml` (copie o seu TOML de instituições e acrescente a seção abaixo), gere a extensão com as mesmas `BANKMON_CONFIG`/`BANKMON_DATA_DIR` que o iniciador usa e carregue-a no navegador (página de extensões, modo desenvolvedor, "Carregar sem pacote", pasta `data\leitura-pontual\data\extensao-coletor\`):

```toml
[coleta_navegador]
origens_autorizadas = ["https://downdetector.com.br"]
leitura = "pontual"
```

```
set BANKMON_CONFIG=%CD%\data\leitura-pontual\leitura.toml
set BANKMON_DATA_DIR=%CD%\data\leitura-pontual\data
.venv\Scripts\python -P -m bankmon extensao --pontual --alvo https://downdetector.com.br/
```

**Atalho.** Clique com o botão direito em `scripts\windows\iniciar-leitura-pontual.cmd` e escolha Enviar para, Área de trabalho (criar atalho).

**Uso.** Dê duplo clique no iniciador (ele define as duas variáveis e roda `serve --navegador --abrir`; não gera a extensão, não lê o token e não abre o Downdetector). Abra você mesmo a aba `https://downdetector.com.br/` e espere carregar; clique no ícone da extensão "BankMonitor — Leitura Pontual" (ou rode `coletarAgora()` no console do service worker da extensão). A dashboard recarrega sozinha quando a leitura chega. **Feche a janela do iniciador ao terminar**: o servidor só deve ficar no ar durante a leitura (SEC-002). O Agendador de Tarefas não deve subir este ambiente.

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
- O dashboard envia Content-Security-Policy restritiva (`default-src 'self'`, sem scripts ou estilos inline, sem frames e `form-action 'self'`: o único formulário é o POST de retomada, protegido por token CSRF), autoescape do Jinja e SQL sempre parametrizado.
- Com `serve --navegador`, as rotas de POST da coleta exigem o token `X-BankMonitor-Token` (em `data/coleta-token.txt`, fora do git; nunca logado nem devolvido) e a retomada exige CSRF.
- Quatro bancos separados em `data/`: `bankmon-demo.db` (demonstração), `bankmon-offline.db` (importação de HTML), `bankmon-navegador.db` (coleta pelo navegador) e `bankmon.db` (operacional, reservado ao coletor autorizado). Nenhum modo toca nos demais.
- A Etapa 1 não usa nenhum segredo. `.env` fica fora do git (`.gitignore`); `.env.example` só documenta nomes de variáveis. Se a Etapa 2 exigir credenciais de um canal autorizado, elas entram por variável de ambiente, nunca no código.
- Textos vindos de coletores são sanitizados antes de gravar ou exibir.

## Estrutura do projeto

```
config/institutions.toml   configuração (categorias, instituições, agenda)
src/bankmon/               cli, config, db, runner (lock), status (regras), collectors/, web/, extension, origins, browser_service
extension/coletor/         modelo da extensão do navegador (sem segredos)
.claude/skills/            skills do projeto (webapp-testing, tdd, diagnosing-bugs; versões fixadas em skills-lock.json)
CLAUDE.md                  regras do projeto (fonte de dados, skills, operação)
scripts/windows/           register-tasks.ps1, unregister-tasks.ps1, start-dashboard.cmd
scripts/linux/             unidades systemd --user (collect service/timer, serve service)
docs/                      operação e viabilidade da Etapa 2
tests/                     testes (pytest)
tests/e2e/                 E2E com Edge + extensão e simulador local (pytest -m e2e)
tests/js/                  harness Node do background.js da extensão
data/                      banco, logs e backups (ignorado pelo git)
.claude/agents/            agentes do projeto
```

Agentes do projeto (`.claude/agents/`): `scraping-specialist` (Etapa 2, só por canal autorizado), `incident-analyzer` (regras de estado, transições e resumos para comunicados) e `dashboard-specialist` (interface).

## Testes

```powershell
.venv\Scripts\python -m pytest
```

Os testes usam diretórios temporários e nunca tocam em `data/`. O pytest já vem de `requirements.lock`. Os testes E2E com navegador real (`pip install -e .[e2e]`) ficam fora da suíte padrão: `python -m pytest -m e2e`. Os testes do `background.js` usam Node.js (são pulados se ele não existir).

## Limitações e próximos passos

- O coletor real depende de autorização do Downdetector (Etapa 2). Hoje há dados de demonstração, importação offline de HTML salvo manualmente e coleta pela aba do navegador apenas para origens locais configuradas.
- Uma única máquina, um usuário, sem autenticação: não é um serviço para rede.
- No Windows, com o PC desligado ou suspenso não há coleta; as horas perdidas ficam como lacuna.
- Próximos passos: coletor autorizado, confirmação dos `dd_label` reais e, se necessário, alertas e controle de acesso.
- **Limitação temporária (INF-01).** O estado da coleta pelo navegador agora é **por origem** (schema v6): uma pausa não afeta as outras origens, e duas configurações no mesmo diretório de dados têm estados independentes **quando as origens são diferentes** (configurações com a mesma origem dividem o estado dela), com duas exceções descritas abaixo: a suspensão de origem externa e a linha legada anterior à v6. Continuam valendo, até haver autorização registrada:
  - a origem externa precisa ser a **única** da lista (`[coleta_navegador] origens_autorizadas`);
  - origem externa não tem retomada nem verificação (regra do `CLAUDE.md`); `/coleta/retomar` responde 409 para ela, mesmo `ativa`;
  - não há autorização registrada em `docs/etapa2-viabilidade-coletor.md`, então origem externa é sempre recusada;
  - a linha global antiga de `collector_state` está **congelada**: nada a grava depois da v6 e ela vale só como fallback de leitura para a origem sem linha própria, então nenhuma pausa anterior se perde. Num banco migrado, uma pausa de loopback herdada vale como suspensão numa origem externa, e uma suspensão externa herdada também trava (sem retomada) a origem loopback sem linha própria. A suspensão de origem externa (por desafio ou falhas) vale para qualquer outra origem externa do mesmo banco, e só se reverte com um banco novo, depois da revisão da autorização.

  Retomar uma origem externa depois de um desafio depende de mudar a regra do `CLAUDE.md`, que hoje a proíbe, e de uma autorização registrada que cubra esse uso.
