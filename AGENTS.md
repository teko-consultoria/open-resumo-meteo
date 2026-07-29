# AGENTS.md

Este é um pipeline aberto e auditável para gerar um resumo meteorológico diário
do Brasil, com chuva por bacia e contexto sinótico voltado ao setor elétrico.
O README é a referência funcional; `docs/architecture.md`,
`docs/data-sources.md` e `docs/operations.md` detalham o desenho e a operação.

## Contrato do produto

- Fontes autorizadas: GFS, GEFS, IFS determinístico, ENS e GPM IMERG.
- A execução editorial usa exclusivamente a rodada 00 UTC e não mistura datas
  de inicialização.
- GFS+GEFS e IFS+ENS são duas famílias independentes. Membros de ensemble não
  contam como votos independentes entre famílias.
- As janelas são fixas e semiabertas: `[0,120)`, `[120,240)` e `[240,336)`
  horas, equivalentes a 5, 5 e 4 dias.
- O IFS determinístico termina em 240 horas e deve permanecer ausente na
  terceira janela.
- Ausência ou cobertura parcial é informação de qualidade, nunca chuva zero.
- O catálogo espacial contém 27 GeoJSON versionados; somente as 12 bacias com
  climatologia compatível entram no texto.
- Previsão e chuva realizada IMERG devem usar exatamente o mesmo catálogo
  geométrico; agregados de outra versão precisam ser descartados e recalculados.
- O `analysis_packet` vigente é a única fronteira meteorológica da redação.
  GRIB, HDF, imagens e geometrias não são enviados ao provedor editorial.
- A geração e o reparo acontecem por janela. As seções validadas são
  concatenadas e passam por uma validação determinística final.

## Fronteiras do código

- `core/`: contratos, matemática, geometrias, climatologias, diagnósticos,
  brief editorial e validação; deve permanecer determinístico e sem rede.
- `sources/`: HTTP, autenticação, índices remotos, byte ranges e retries.
- `ingestion/`: leitura de GRIB/HDF, normalização e materialização de derivados.
- `storage/`: catálogo SQL, idempotência, seleção temporal e retenção.
- `pipeline/`: orquestração; não deve reimplementar regras de domínio.
- `cli.py`: composição operacional e apresentação de resultados.
- `scripts/`: reprodução dos ativos vendorizados, não caminhos de runtime.

Fontes únicas de verdade importantes:

- `core/config/basins.json` e `core/assets/basins/v2/manifest.json`: identidade,
  proveniência e checksums das 27 geometrias;
- `core/config/weather_summary_basins.json`: escopo editorial de 12 bacias;
- `core/data/*.manifest.json`: identidade e checksum dos dados estáticos;
- `core/schema.py` e `core/schemas/analysis_packet_v2.schema.json`: contrato
  tipado e representação JSON Schema;
- `core/openai_client.py::PROMPT_BUNDLE`: conjunto exato dos prompts vigentes.

## Regras de manutenção

- Mantenha apenas o contrato, os prompts e os caminhos de execução atuais.
  Migração histórica e compatibilidade retroativa só entram quando forem um
  requisito explícito; o histórico normal pertence ao controle de versão.
- Não crie aliases, wrappers ou reexports sem consumidor concreto.
- Não distribua matrizes espaciais calculadas para outra versão geométrica.
  Pesos devem ser reconstruídos a partir da grade e dos contornos vigentes.
- GRIB e HDF brutos são temporários e precisam ser removidos também em falhas.
- Operações de escrita e publicação devem continuar atômicas, idempotentes e
  restritas às raízes configuradas.
- Novas fontes ou ativos derivados exigem origem, licença, período, método,
  limitações e SHA-256 documentados em manifesto e, quando aplicável,
  `NOTICE.md`.
- Uma mudança em janelas, modelos, consenso, climatologia, geometria,
  arredondamento, diagnóstico ou validação exige teste determinístico.
- Testes não podem acessar rede, depender de credenciais, relógio real ou ordem
  não determinística.
- Nunca versione `.env`, chaves, bancos, arquivos meteorológicos brutos,
  `var/`, `tmp/`, caches ou artefatos de build.

Ao substituir um schema, prompt ou ativo, atualize juntos o identificador de
versão, o manifesto/checksum, os testes e a documentação. Não mantenha a versão
anterior ao lado da vigente sem um consumidor operacional demonstrável.

## Verificação

Use Python 3.12, `uv` e a biblioteca nativa ecCodes.

```bash
make check
uv run resumo-meteo doctor --skip-database
uv run resumo-meteo --help
uv build
```

Antes de encerrar uma mudança, confirme também que o pacote construído contém
somente o schema atual, os quatro prompts do `PROMPT_BUNDLE`, os 27 GeoJSON e os
dados estáticos documentados.
