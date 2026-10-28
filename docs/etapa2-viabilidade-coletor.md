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
- A informação de que o controle da Ookla mudou em 2026 (aquisição pela Accenture) vem de fontes secundárias e **não foi verificada**. Isso pode mudar o canal de licenciamento.

## Por que sessão e Cookie Jar não resolvem

Um Cookie Jar (`httpx.Client`, `http.cookiejar`) guarda e reenvia os cookies que o servidor entrega. É útil para estado de sessão comum (consentimento, idioma) e para canais autenticados.

Aqui ele não resolve:

1. **O bloqueio vem antes de qualquer sessão.** O `403 challenge` é a primeira resposta. O cookie de liberação (`cf_clearance`) só é emitido depois que um navegador resolve o desafio. Sem esse passo, não há o que guardar.
2. **O cookie é preso ao cliente que resolveu o desafio:** user-agent e, em geral, IP e assinatura TLS/HTTP. Reutilizado por outro cliente, ele é rejeitado ou dispara um novo desafio.
3. **O cookie expira.** O prazo é definido pelo site; no Cloudflare, o padrão do "Challenge Passage" é de 30 min. Renovar exigiria uma pessoa resolvendo o desafio de novo, o que é incompatível com coleta horária sem supervisão.
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

## Referências

- Documentação de integração Sumo Logic × Downdetector (API Enterprise): https://www.sumologic.com/help/docs/integrations/automation-integrations/downdetector/
- Biblioteca não oficial `downdetector-api`. **Não usar:** é citada só como evidência de que o Cloudflare bloqueia clientes automatizados. https://github.com/DavideViolante/downdetector-api
- Cloudflare, Challenge Passage: https://developers.cloudflare.com/waf/tools/challenge-passage/
