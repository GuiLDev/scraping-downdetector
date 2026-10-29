# Regras do projeto BankMonitor (Downdetector Banking Monitor)

Estas regras valem para a sessão principal, os agentes e as skills deste projeto.

## Fonte de dados (inegociável)
- **Nenhum acesso automatizado ao Downdetector real** sem autorização registrada em `docs/etapa2-viabilidade-coletor.md`, seção "Autorizações registradas". Isso vale para curl, WebFetch, scripts, Playwright, `with_server.py`, a extensão de coleta e qualquer outra forma de automação.
- **Nunca contornar Cloudflare, CAPTCHA ou desafios:**
  - nada de headless ou automação para passar desafio;
  - nada de imitar fingerprint ou user-agent, nem usar proxies;
  - nada de reaproveitar ou copiar cookies (`cf_clearance`) ou repetir tráfego capturado.
- O fluxo "humano resolve o desafio e a automação continua" é proibido para origens externas. A retomada só existe para páginas locais de teste.

## Skills do projeto (`.claude/skills/`)
| Skill | Origem (commit na instalação) | Uso permitido |
|---|---|---|
| `webapp-testing` | `anthropics/skills@b9e19e6f4477`, `skills/webapp-testing` (Apache-2.0, licença em `.claude/skills/webapp-testing/LICENSE.txt`) | Testes com Playwright **somente contra loopback** (`127.0.0.1`/`localhost`) e páginas locais. Navegador: Edge instalado (`channel="msedge"`). |
| `tdd` | `mattpocock/skills@3f599139104d`, `skills/engineering/tdd` (MIT, licença em `.claude/LICENSE-mattpocock-skills.txt`) | Ciclos vermelho→verde em implementações novas. |
| `diagnosing-bugs` | `mattpocock/skills@f3fc5632f401`, `skills/engineering/diagnosing-bugs` (MIT, licença em `.claude/LICENSE-mattpocock-skills.txt`) | Só para falhas relevantes. A recomendação de "navegador headless" ou "repetir tráfego capturado" **não** se aplica a fontes externas. **Não** executar `scripts/hitl-loop.template.sh`, que é interativo. |

- As skills foram instaladas com `npx skills@1.7.4 add ... -a claude-code` (escopo do projeto). O registro está em `skills-lock.json`.
- Antes de atualizar uma skill, revise de novo o `SKILL.md` e os scripts dela.
- O `with_server.py` usa `shell=True`: passe apenas comandos do próprio projeto, nunca entrada externa. No Windows, encerre os servidores filhos explicitamente.

## Operação
- **Bancos:** demo (`bankmon-demo.db`), offline (`bankmon-offline.db`), navegador (`bankmon-navegador.db`) e operacional (`bankmon.db`) ficam separados. Não alterar o operacional em testes.
- **Testes:** a suíte padrão exclui os E2E (`-m 'not e2e'`). Os E2E rodam com `pytest -m e2e`.
- **Memória limitada:** rode as suítes em sequência, nunca em paralelo.
- **Git:** commit e push só com autorização do responsável.
