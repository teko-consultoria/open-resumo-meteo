# Operação diária

## Pré-condições

- `WEATHER_SUMMARY_LLM_API_KEY` e `NASA_EARTHDATA_TOKEN` configurados;
- acesso HTTPS a NOAA, ECMWF, NASA e ao provedor editorial escolhido;
- ecCodes disponível;
- banco inicializado;
- rodada configurada como `00`.

## Fluxo

```bash
uv run resumo-meteo doctor
uv run resumo-meteo run --date AAAA-MM-DD --cycle 00
```

O comando verifica disponibilidade, baixa cada fonte em temporários, normaliza,
remove os brutos, constrói o pacote, redige, revisa quando necessário, valida e
publica atomicamente.

## Falhas e retomada

- Reexecute o mesmo comando: etapas completas são reutilizadas por idempotência.
- Use `uv run resumo-meteo status --date AAAA-MM-DD --cycle 00 --json` para
  consultar fontes ausentes e tentativas.
- Use `generate --force` para criar uma nova tentativa editorial sem sobrescrever
  a publicação anterior. `run --force` também força nova ingestão.
- Um `--cutoff` explícito é respeitado como instante fixo; sem a opção, o
  pipeline fecha o corte depois dos downloads da própria execução.
- Claims atômicos impedem downloads e chamadas editoriais duplicados. Uma lease
  sem atualização por quatro horas é marcada como falha e liberada para retry.
- Arquivos brutos de uma falha também são removidos. Diagnóstico usa logs e
  metadados; não há opção para reter GRIB/HDF.

## Execução sem observação

```bash
uv run resumo-meteo run \
  --date AAAA-MM-DD \
  --cycle 00 \
  --imerg-history-days 0
```

Essa escolha fica registrada como limitação. Diagnósticos que dependem de
persistência observada podem ser omitidos ou rebaixados.

## Saídas

Em `var/outputs/AAAA-MM-DD/00/run-<id>-attempt-<n>/`:

- `summary.txt`: produto visível validado;
- `analysis_packet.json`: fronteira estruturada;
- `source_manifest.json`: rodadas, cobertura e qualidade;
- `quality.json`: bloqueios e limitações;
- `generation.json`: modelo, prompt e metadados;
- `validation.json`: resultado e motivos de rejeição;
- `manifest.json`: checksums da publicação.

## Limpeza

```bash
uv run resumo-meteo cleanup --dry-run --json
uv run resumo-meteo cleanup --json
```

Faça primeiro a simulação. O comando respeita os diretórios e períodos de
retenção configurados e não remove publicações imutáveis fora do escopo.
