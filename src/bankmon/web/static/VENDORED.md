# Assets de terceiros (vendored)

Copiados sem modificação dos tarballs oficiais do registry npm. A integridade de cada
tarball foi conferida contra o `dist.integrity` (sha512) do metadata do registry
antes de extrair. Sem CDN: o painel funciona offline.

Para atualizar um asset, baixe a nova versão, confira o `dist.integrity` e atualize
esta tabela (o teste `tests/test_web_assets.py` compara os hashes).

## Pacotes

| Pacote | Versão | Origem | Licença |
|---|---|---|---|
| `@picocss/pico` | 2.1.1 | https://registry.npmjs.org/@picocss/pico/-/pico-2.1.1.tgz | MIT |
| `chart.js` | 4.6.1 | https://registry.npmjs.org/chart.js/-/chart.js-4.6.1.tgz | MIT |

## Arquivos (SHA-256)

| Arquivo (relativo a `static/`) | Origem no pacote | SHA-256 |
|---|---|---|
| `vendor/pico/pico.min.css` | `package/css/pico.min.css` | `fbc9a63fc9fc9f72d12fd7fc9806e11fa9f77ae4f9cad146b27003a1119ba3db` |
| `vendor/pico/LICENSE.md` | `package/LICENSE.md` | `afaff063e044233f917b7807dccab022f09dca474f92b642846bee4850655bbf` |
| `vendor/chartjs/chart.umd.min.js` | `package/dist/chart.umd.min.js` | `87364cc2b2a76a6684929e7d6b80b18a897794db38400caff1fcf1791c6925d2` |
| `vendor/chartjs/LICENSE.md` | `package/LICENSE.md` | `41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce` |

Observação: `chart.umd.min.js` termina com um comentário `sourceMappingURL`; o arquivo
`.map` não foi copiado (só afeta ferramentas de desenvolvedor).
