# Arquitetura

## Fluxo de dados

```text
NOAA / ECMWF / NASA
        │
        ▼
download efêmero de GRIB/HDF
        │
        ▼
normalização de grades e agregação espacial
        │
        ├── chuva diária determinística
        ├── membros de ensemble por bacia/janela
        ├── campos sinóticos a cada 12 h
        └── chuva IMERG completa/parcial
        │
        ▼
catálogo SQL + artefatos derivados compactos
        │
        ▼
analysis_packet tipado e validado
        │
        ▼
redação via API compatível com OpenAI
        │
        ▼
validação determinística + publicação imutável
```

## Fronteiras

`sources` conhece HTTP, índices remotos, autenticação e política de retries.
Não contém regra editorial.

`ingestion` lê GRIB/HDF dentro de diretórios temporários, converte unidades,
normaliza grades e persiste somente produtos derivados. Todo caminho de saída,
inclusive exceções, remove arquivos brutos.

`core` contém matemática meteorológica, geometrias, climatologias, schemas e
validação. Essas funções devem ser determinísticas e independentes de rede.

`storage` seleciona fontes disponíveis até um `cutoff`, registra proveniência e
implementa idempotência. SQLite serve ao modo local; PostgreSQL atende
concorrência.

`pipeline` orquestra as etapas sem duplicar as regras de domínio. A publicação
é atômica: leitores veem uma versão completa ou nenhuma.

## Invariantes meteorológicas

- Somente GFS, GEFS, IFS (`ec`), ENS (`ecens`) e IMERG.
- Somente ciclo 00 UTC, uma emissão por dia.
- Nenhuma mistura de datas de inicialização.
- Famílias independentes: NCEP (GFS+GEFS) e ECMWF (IFS+ENS).
- Janelas fixas de 120 h, 120 h e 96 h, total de 336 h.
- Previsão e chuva realizada usam o mesmo catálogo geométrico versionado; um
  agregado IMERG de outra versão nunca é combinado com a previsão atual.
- IFS determinístico ausente depois de 240 h.
- Cada membro de ensemble preservado até a agregação.
- Ausência e cobertura parcial são dados, nunca valores zero.
- Diagnósticos nomeados exigem suporte estruturado e rastreável.

## Fronteira editorial

`analysis_packet.json` é a única fonte meteorológica da redação. O payload
enviado ao provedor é minimizado, e metadados com nomes sensíveis são
bloqueados. O texto retornado é rejeitado se inventar datas, volumes,
mecanismos ou implicações hidrológicas que o pacote não autoriza.

O aviso imutável de geração por IA é inserido somente após a validação.

## Reprodutibilidade

Cada execução registra:

- data de emissão, ciclo e `cutoff`;
- identidade, horário, cobertura e checksum das fontes;
- versões de geometrias, climatologias, config e schema;
- IDs de execução e tentativa;
- entrada editorial reduzida, resposta bruta e relatório de validação.

Pacotes estáticos têm checksums verificados pelo comando `doctor` e pelos
testes.
