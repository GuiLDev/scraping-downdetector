# Etapa 2: viabilidade do coletor do Downdetector

**Status: PENDENTE DE AUTORIZAÇÃO.** O coletor `downdetector` é um stub sem nenhum código de rede (`src/bankmon/collectors/downdetector.py`). Ele grava execuções `not_authorized`, e o dashboard mostra "Coletor Downdetector pendente de autorização". O stub só será substituído quando houver um canal de acesso autorizado, registrado neste documento.

## Objetivo

Ler os indicadores visuais (laranja e vermelho) da página inicial do Downdetector Brasil (`downdetector.com.br`) a cada 1 hora e registrar as instituições financeiras com possível instabilidade.

## Evidências verificadas (2026-10-28)

Requisições HTTP simples, sem simular navegador:

| URL | Resposta |
|---|---|
| `https://downdetector.com.br/` | `403`, `Server: cloudflare`, `Cf-Mitigated: challenge` |
| `https://downdetector.com.br/robots.txt` | `403`, `Cf-Mitigated: challenge` |
| `https://downdetector.com/terms-of-use.html` | `403`, `Cf-Mitigated: challenge` |

- Todo acesso de cliente que não seja um navegador cai num **desafio anti-bot do Cloudflare**, inclusive o `robots.txt` e a página de termos.
- A ferramenta de leitura web dos agentes também foi recusada em `downdetector.com`, `downdetector.com.br`, `ookla.com` e `web.archive.org`.
- **Os termos de uso não puderam ser lidos por automação e não foram verificados.**
- O Downdetector oferece uma **API Enterprise**: o cliente entra no portal e usa as credenciais da API, segundo a documentação de integração da Sumo Logic. O preço é sob cotação.
- **Proprietário atual:** a Accenture **concluiu em 17/06/2026** a aquisição da Ookla, dona do Downdetector e do Speedtest, e de outras marcas da divisão Connectivity da Ziff Davis. A operação tinha sido anunciada em 03/03/2026. Fontes oficiais em "Referências".
  - A troca de dono **não altera automaticamente os termos de uso** do Downdetector **nem concede autorização** para coleta automatizada. Valem os termos publicados hoje, que precisam ser lidos (T1). Qualquer permissão tem de vir por escrito do titular atual.

## T1: `robots.txt` (lido pelo responsável no navegador, 2026-10-28)

`https://downdetector.com.br/robots.txt`:

```
User-Agent: *
Allow: /ads.txt
Disallow: /search/?q=*
Disallow: /
```

Seguem-se exceções nominais com `Allow: /`: buscadores (Googlebot, bingbot, DuckDuckBot etc.) e crawlers de IA (GPTBot, ClaudeBot etc.).

- **Um coletor próprio cai em `User-Agent: *`, e todo o site está vetado para ele, inclusive a página inicial.**
- As exceções valem só para os serviços nomeados. Usar o user-agent de um deles seria falsificação, proibida neste projeto.
- **Resultado:** pelo critério de parada do T1, nenhum teste automatizado será feito. O caminho é a permissão por escrito. O T2 fica adiado até haver permissão.
- Os termos de uso ainda não foram lidos (pendente).

## Por que sessão e Cookie Jar não resolvem

Um Cookie Jar (`httpx.Client`, `http.cookiejar`) guarda e reenvia os cookies que o servidor entrega. É útil para estado de sessão comum (consentimento, idioma) e para canais autenticados.

Aqui ele não resolve:

1. **O bloqueio vem antes de qualquer sessão.** O `403 challenge` é a primeira resposta. O cookie de liberação (`cf_clearance`) só é emitido depois que um navegador resolve o desafio. Sem esse passo, não há o que guardar.
2. **O cookie é preso a quem resolveu o desafio.** A documentação da Cloudflare diz que ele é ligado ao "visitante e dispositivo". Quais sinais exatamente (user-agent, IP, assinatura TLS) é **inferência nossa, não confirmada**. A Cloudflare também pode desafiar de novo antes do vencimento se a sessão parecer suspeita.
3. **Validade e renovação.**
   - A validade é configurada pelo site. O padrão da Cloudflare ("Challenge Passage") é de 30 min, mas esse é **apenas o valor padrão configurável**. A validade usada pelo Downdetector **não é conhecida**.
   - Numa sessão legítima de navegador, a renovação **pode ocorrer sem interação humana**: a Cloudflare pode rodar verificações não interativas no próprio navegador e atualizar a liberação durante a sessão.
   - Isso **não garante** que um Cookie Jar funcione em outro cliente HTTP, que não executa essas verificações e não é o "visitante e dispositivo" liberado.
   - A viabilidade de uma sessão automatizada contínua (coleta horária sem supervisão) **não foi demonstrada**. Este item descreve hipóteses e limites conhecidos, não fatos testados no Downdetector.
4. **Exportar o cookie do navegador para um script, ou automatizar um navegador para obtê-lo, é contornar a proteção:** um passe emitido para uma pessoa passa a ser usado por um robô. **Não será implementado.**

**Conclusão:** sessão e Cookie Jar só serão usados *dentro* de um canal já autorizado, por exemplo para manter um token de API. Eles não são um caminho de autorização.

## O que o projeto não faz (proibições)

- Contornar Cloudflare, CAPTCHA, rate limiting ou outra proteção.
- Usar navegador headless ou automatizado para passar desafios, imitar fingerprint TLS/HTTP (curl_cffi, tls-client etc.), usar proxies rotativos ou serviços de "unblocker".
- Reaproveitar `cf_clearance` ou pedir ao usuário que exporte cookies, HAR ou sessão do navegador.
- Obter os dados por fontes indiretas: web.archive.org, caches de buscadores, APIs de scraping de terceiros ou sites que republicam o Downdetector.
- Falsificar user-agent de navegador.

Essas regras também estão em `.claude/agents/scraping-specialist.md`.

## Caminhos legítimos

| Caminho | Como obter | Encaixe no projeto |
|---|---|---|
| **Licença da API Downdetector Enterprise** (recomendado) | Contato comercial da Ookla/Downdetector (Downdetector Enterprise), confirmando o canal atual | `DowndetectorApiCollector`: converte os indicadores da API para `no_alert`, `attention`, `critical` ou `not_listed` |
| **Permissão por escrito** para acesso automatizado à página inicial | Pedido formal ao Downdetector, por exemplo com IP liberado no Cloudflare e user-agent identificado | `DowndetectorHomepageCollector`: 1 requisição por hora, respeitando o `robots.txt` |

### Requisitos de qualquer coletor autorizado

- Implementa o contrato `Collector` (`src/bankmon/collectors/base.py`) e é registrado em `collectors/registry.py`. Dashboard e banco não mudam.
- User-agent próprio e identificado. Timeout. Respeito a `Retry-After`. No máximo a frequência autorizada (1 vez por hora).
- Credenciais **somente** em variáveis de ambiente (`.env`, fora do git, documentadas em `.env.example` sem valores). Sempre em header, **nunca** na URL.
- Falha de rede ou de parsing vira `source_status` `inaccessible` ou `error`, nunca "sem alerta". Instituição ausente da fonte vira `not_listed`.
- O parser é uma função pura, testada com fixtures obtidas pelo canal autorizado.
- O `dd_label` de cada instituição em `config/institutions.toml` deve ser confirmado contra a fonte autorizada.

## Próximos passos (dependem do responsável pelo projeto)

1. Ler os termos de uso do Downdetector **no navegador**, procurando "automated", "scrape", "robot", "commercial" e "reproduce".
2. Decidir entre pedir a cotação da API Enterprise ou uma permissão por escrito, e confirmar o canal atual de licenciamento.
3. Confirmar com o jurídico se dados, cores ou capturas do Downdetector podem ser citados nos comunicados aos clientes.
4. Com a autorização registrada aqui (data, escopo, referência do contrato ou e-mail), o `scraping-specialist` implementa o coletor correspondente, seguido de `qa-tester` e `security-reviewer`.

## Leitura pontual — decisão interna

**Decisão do responsável em 2026-10-30 (D1). É uma regra interna do projeto, não uma autorização do Downdetector.** Não entra em "Autorizações registradas" e não usa `autorizacao_ref`.

Leitura pontual permitida: uma leitura do DOM já carregado na aba aberta pelo responsável, acionada manualmente, sem recarga, sem agendamento e sem nenhuma requisição da extensão ao site. A extensão nunca interage com desafios. **Não é autorização do serviço.** Toda coleta periódica ou automática continua exigindo autorização registrada.

- **Viabilidade técnica:** ler o DOM renderizado de uma aba aberta não faz requisição ao site. O recurso fica restrito a `[coleta_navegador] leitura = "pontual"`, com uma única origem, extensão gerada com `extensao --pontual` e banco separado.
- **O que o servidor garante:**
  - token obrigatório;
  - origem da lista;
  - com config pontual, a reserva e a leitura exigem `modo: "pontual"`; qualquer outro modo é recusado sem gravar nada (409; na leitura, modo desconhecido: 400), e sem reserva a extensão não recarrega;
  - reserva válida consumida junto com a leitura;
  - no máximo 1 reserva a cada 60 min para origem externa (em loopback vale `intervalo_minimo_local_s`);
  - suspensão se o DOM for um desafio (em origem externa, sem retomada).
- **O que só a extensão garante:** o acionamento manual (`coletarAgora()`), sem alarme e sem `tabs.reload`. O servidor não distingue um clique humano de um processo local que tenha o token (SEC-002).
- **Mitigação operacional do SEC-002 (decidida em 2026-10-30):** o servidor pontual sobe só durante a leitura, com token próprio na pasta de dados separada, e é encerrado logo depois. Com o limite de 1 reserva a cada 60 min, isso dá uma leitura por sessão. Esse é o risco residual aceito.
- **Procedimento obrigatório (SEC-008):** `extensao --pontual` e `serve --navegador` rodam com o **mesmo** `BANKMON_DATA_DIR` próprio (`data/leitura-pontual/data`, fora do git), e a mesma pasta é usada em todas as sessões. Sem essa variável, o modo pontual usaria o token e o banco padrão de `data/`. Trocar de pasta zera o limite de 60 min e a suspensão, que valem por banco.
- **Condições externas, em aberto (D2):** os termos de uso do Downdetector sobre extração e reprodução ainda não foram lidos (a mesma questão da exportação manual de 28/10). O `robots.txt` veta coletores; a leitura pontual não faz requisição. O Cloudflare não é tocado: o responsável acessa como visitante e a extensão não interage com desafios. O uso dos dados fica restrito à validação interna.
- Se o DOM lido for um desafio, a leitura é gravada como `inaccessible` e a origem é suspensa, sem retomada.

## Autorizações registradas

Uma origem externa na coleta pelo navegador (`[coleta_navegador]` no TOML) só é aceita se o `autorizacao_ref` for igual a um identificador listado aqui, no formato `- `ID`: data, escopo, referência do contrato ou e-mail`. A frequência máxima para origem externa é 1 leitura por hora, e um desafio de verificação suspende a coleta até revisão desta autorização.

Nenhuma autorização registrada até agora (origem externa recusada, exceto na leitura pontual, que não é autorização; ver "Leitura pontual — decisão interna").

## Referências

- Documentação de integração Sumo Logic × Downdetector (API Enterprise): https://www.sumologic.com/help/docs/integrations/automation-integrations/downdetector/
- Biblioteca não oficial `downdetector-api`. **Não usar:** é citada só como evidência de que o Cloudflare bloqueia clientes automatizados. https://github.com/DavideViolante/downdetector-api
- Cloudflare, Challenge Passage: https://developers.cloudflare.com/waf/tools/challenge-passage/

### Aquisição da Ookla pela Accenture (fontes oficiais)

- Accenture Newsroom, comunicado de 03/03/2026 com a nota "This acquisition closed on June 17, 2026": https://newsroom.accenture.com/news/2026/accenture-to-acquire-ookla-to-strengthen-network-intelligence-and-experience-with-data-and-ai-for-enterprises
- Ziff Davis, comunicado da venda da divisão Connectivity (Business Wire). A nota de conclusão neste link não foi conferida, porque a página bloqueou a leitura automatizada. A conclusão está confirmada pelas outras duas fontes: https://www.businesswire.com/news/home/20260302093984/en/Ziff-Davis-Announces-Definitive-Agreement-to-Sell-Connectivity-Division-to-Accenture
- Ziff Davis, Form 10-Q do trimestre encerrado em 30/06/2026 (SEC): https://www.sec.gov/Archives/edgar/data/0001084048/000108404826000046/zd-20260630.htm
