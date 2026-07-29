# Open Resumo Meteo

Pipeline aberto e auditável para produzir um resumo meteorológico diário do
Brasil, com foco em chuva por bacia e contexto sinótico relevante ao setor
elétrico.

O projeto ingere exclusivamente a rodada 00 UTC de quatro previsões numéricas,
combina duas famílias independentes de modelos, agrega precipitação sobre
polígonos de bacias, compara os resultados com chuva observada e climatologias
versionadas e só então entrega um pacote estruturado para a etapa de redação.

> O software está em estágio alpha. A saída é informativa, pode conter erros e
> não substitui análise meteorológica profissional nem procedimentos
> operacionais do setor elétrico.

## Dados usados para construir a previsão

| Componente | Fonte/produto | Resolução temporal usada | Papel no resumo |
| --- | --- | --- | --- |
| Previsão determinística | NOAA GFS | acumulados nativos de 6 h, até 336 h no produto | Chuva por bacia e campos sinóticos |
| Ensemble | NOAA GEFS, 31 membros | acumulados nativos de 6 h, até 336 h | Distribuição e incerteza da família NCEP |
| Previsão determinística | ECMWF IFS Open Data (`ec`) | acumulados em passos de 24 h, até 240 h | Chuva por bacia e campos sinóticos |
| Ensemble | ECMWF ENS Open Data (`ecens`), 50 membros perturbados | acumulado nos limites das janelas, até 336 h | Distribuição e incerteza da família ECMWF |
| Chuva realizada | NASA GPM IMERG V07 Daily (`GPM_3IMERGDL`) | 1 grade diária de 0,1°, em mm/dia | Acumulados antecedentes de dias completos |
| Chuva do dia em andamento | NASA GPM IMERG V07 Early (`GPM_3IMERGHHE`) | grades de 30 min, até 48 por dia | Acumulado parcial separado, limitado pelo `cutoff` |
| Climatologia de chuva | MERGE/INPE | série diária de 2001–2020 | Referência histórica para janelas de 4 e 5 dias |
| Climatologia de temperatura | NOAA/NCEP Reanalysis 1 | médias mensais regionais de 1991–2020 | Classificação de anomalias regionais |
| Geometria | contornos públicos do ONS, agregados segundo a configuração SMAP revisada em abril de 2025 | 27 geometrias em EPSG:4326, alinhadas ao conjunto dos mapas gradeados | Máscara espacial para médias e cobertura por bacia |

Todos os modelos precisam corresponder à mesma data de inicialização e ao ciclo
00 UTC. O pipeline rejeita mistura de rodadas. GFS+GEFS formam uma família e
IFS+ENS formam a outra; os membros de um ensemble não são tratados como votos
independentes contra a outra família.

### Períodos previstos

As três janelas são contíguas, cobrem 336 horas e usam limites semiabertos em
UTC:

| Janela | Leads | Dias de previsão armazenados | Duração | Fontes de chuva |
| --- | --- | --- | --- | --- |
| `p1` | `[0 h, 120 h)` | D+1 a D+5 | 5 dias | GFS, GEFS, IFS e ENS |
| `p2` | `[120 h, 240 h)` | D+6 a D+10 | 5 dias | GFS, GEFS, IFS e ENS |
| `p3` | `[240 h, 336 h)` | D+11 a D+14 | 4 dias | GFS, GEFS e ENS |

Os rótulos D+1…D+14 identificam o dia ao qual o acumulado diário é atribuído.
Por exemplo, a grade D+1 contém a precipitação entre a inicialização 00 UTC e
24 horas. O IFS determinístico termina em 240 h e permanece ausente em `p3`;
ele nunca é extrapolado.

### Como a chuva prevista é calculada

1. O GFS é convertido de intervalos APCP de 6 h para grades diárias. O IFS,
   que entrega precipitação total acumulada, é diferenciado entre limites
   consecutivos de 24 h.
2. Cada grade é intersectada com os polígonos das bacias. A média da bacia é
   ponderada pela área real de interseção entre célula e polígono, não por uma
   média simples de pontos.
3. Os dias são somados em `p1`, `p2` e `p3`. Uma janela incompleta mantém
   avisos de cobertura; horas ou dias ausentes não são preenchidos
   silenciosamente.
4. GEFS e ENS preservam cada membro até a agregação por bacia e janela. O
   pipeline calcula média, desvio-padrão, P10, P25, P50, P75, P90, intervalo
   interquartil, amplitude P90–P10, coeficiente de variação e probabilidades de
   exceder 5, 10, 25, 50 e 100 mm.
5. A mediana do ensemble representa sua família. A rodada determinística serve
   para verificar coerência com o intervalo P25–P75; ela só vira representante
   quando o ensemble correspondente está ausente, situação marcada como
   degradada.
6. As duas estimativas familiares recebem pesos iguais por padrão. A opinião é
   a média ponderada entre famílias disponíveis, acompanhada de nível de
   concordância, faixa e indicadores de qualidade.
7. O valor editorial é arredondado para 5 mm; usa 10 mm quando supera 100 mm ou
   quando há simultaneamente pelo menos 20 mm de volume e 20 mm de dispersão.

Além da média, o pacote registra a mediana espacial da bacia, P10/P90 espacial,
percentual de área acima de 5, 10, 25 e 50 mm e o máximo em ponto de grade. O
máximo não é apresentado como média de bacia.

### Mapas e campos meteorológicos

“Mapas” neste repositório significam grades numéricas GRIB normalizadas e seus
resumos espaciais, não imagens usadas como entrada de um modelo de linguagem.
Os diagnósticos usam GFS e IFS determinísticos a cada 12 h no domínio
110°W–20°W e 60°S–15°N, em EPSG:4326. O GFS contribui até 336 h e o IFS até
240 h.

| Nível | Campos |
| --- | --- |
| Superfície | pressão ao nível médio do mar; temperatura a 2 m, vento a 10 m, radiação de onda curta e CAPE quando disponíveis |
| 850 hPa | temperatura, umidade relativa e componentes zonal/meridional do vento |
| 500 hPa | altura geopotencial e, quando disponível, vorticidade absoluta |
| 250 hPa | altura geopotencial e componentes zonal/meridional do vento |

Para cada campo são extraídos estatísticas como mínimo, máximo, média, P10,
P50 e P90, localização dos extremos e resumos regionais. Regras objetivas
procuram sinais de frentes, baixas, cristas/cavados e altas pós-frontais. Um
mecanismo só pode ser citado como suportado quando as duas famílias apresentam
coerência física, espacial e temporal.

Uma possível faixa persistente de umidade exige sinal previsto em GFS e IFS
por pelo menos três dias consecutivos e umidade persistente em 850 hPa. A
classificação mais específica de ZCAS também exige ao menos três dias
observados completos de suporte no IMERG. Sem esses requisitos, o texto deve
usar linguagem conservadora.

### Chuva realizada

O histórico recente vem do GPM IMERG V07, consultado pelo CMR/NASA Earthdata:

- dias completos: `GPM_3IMERGDL`, uma grade diária de 0,1° em mm/dia;
- dia de emissão: `GPM_3IMERGHHE` Early, intervalos de 30 min acumulados apenas
  até o instante de corte;
- domínio recortado: 75,5°W–32°W e 35°S–6,5°N;
- primeira execução: tenta preencher até 30 dias anteriores, encerrando no dia
  anterior à emissão;
- métricas por bacia: chuva de 24 h, 72 h, 5, 10 e 30 dias;
- controle de qualidade: contagem de intervalos, completude temporal e
  cobertura espacial válida.

O acumulado parcial do dia corrente permanece separado dos dias completos. Ele
não é somado implicitamente à chuva de 24 h, 72 h ou demais acumulados. Anomalia
de chuva observada em 30 dias, percentil observado, vazão, armazenamento,
umidade do solo e energia afluente não são calculados por esta versão.

Use `--imerg-history-days 0` para pular explicitamente a chuva realizada. A
ausência é registrada nos manifestos e reduz a informação disponível ao
resumo.

### Climatologias

A referência de precipitação é um pacote estático construído a partir de
séries diárias MERGE/INPE de 2001 a 2020:

- 7.305 dias de referência;
- 12 bacias editoriais;
- para cada dia possível do calendário, amostras históricas de janelas de 4 e
  5 dias;
- soma dos valores diários antes do cálculo da distribuição;
- média, mediana, P10, P25, P75 e P90 calculados com quantis R7;
- 732 entradas por bacia: 366 datas iniciais × 2 durações.

Cinco bacias agregadas são recompostas por média diária ponderada pela área das
sub-bacias históricas. Previsão e chuva realizada IMERG usam exatamente o mesmo
catálogo de contornos atuais. A ressalva geométrica se aplica somente à
climatologia: suas máscaras históricas originais não estão disponíveis, então
o pacote preserva as séries compatíveis sem alegar que a máscara histórica seja
idêntica ao contorno atual.

A temperatura é comparada a médias mensais regionais do NOAA/NCEP Reanalysis 1
para 1991–2020, ponderadas pelo cosseno da latitude em oito regiões. Essa
referência serve para qualificar tendências regionais; não é uma normal diária
local.

### Bacias

O catálogo geométrico contém 27 bacias agregadas e versionadas. O resumo
editorial usa as 12 que possuem climatologia de precipitação compatível:

Jacuí, Uruguai, Iguaçu, Paraná, Paranapanema, Tietê, Grande, Paranaíba, São
Francisco, Tocantins, Madeira e Xingu.

As coordenadas são uma transcrição sem simplificação dos shapefiles usados nos
mapas gradeados de GFS, GEFS, IFS e ENS. O conjunto está fixado pela versão
`smap-aggregated-apr2025-grid-v2`, por hashes individuais e por um hash único
dos 27 shapefiles de origem. Assim, o limite desenhado e o limite usado na
integração espacial partem exatamente da mesma geometria. Grades de previsão e
grades IMERG são agregadas com esse mesmo catálogo; um derivado compacto de
outra versão é descartado e recalculado a partir da grade normalizada.

Os demais polígonos permanecem disponíveis no catálogo para produtos espaciais,
mas não recebem opinião editorial. Checksums, caixas envolventes e proveniência dos
contornos ficam no manifesto em
[`core/assets/basins/v2/manifest.json`](src/resumo_meteo/core/assets/basins/v2/manifest.json).

### O que chega à etapa de redação

O modelo de linguagem não recebe arquivos GRIB/HDF nem imagens meteorológicas.
Ele recebe somente o `analysis_packet.json`, já validado e serializável, com:

- valores por bacia, janela, modelo e família;
- estatísticas dos ensembles e opinião calculada deterministicamente;
- chuva realizada completa e parcial;
- comparações climatológicas;
- diagnósticos sinóticos estruturados;
- fontes, horários, cobertura, limitações e alertas de qualidade.

A resposta passa por um segundo validador, que confere títulos, datas, valores
de chuva, unidades, mecanismos suportados e linguagem de incerteza. O aviso de
uso de IA é anexado depois da validação e não pode ser alterado pelo provedor.

Mais detalhes e links para os provedores estão em
[docs/data-sources.md](docs/data-sources.md).

## Arquitetura

```text
src/resumo_meteo/
├── core/       # matemática, contratos, bacias, climatologia e validação
├── sources/    # clientes HTTP para NOAA, ECMWF e NASA
├── ingestion/  # download efêmero e normalização
├── storage/    # catálogo SQL e repositório de artefatos
├── pipeline/   # ingestão, pacote, geração, publicação e cleanup
├── cli.py      # interface operacional
└── config.py   # configuração por ambiente
```

Veja [docs/architecture.md](docs/architecture.md) para as fronteiras e
invariantes do fluxo.

## Requisitos

- Python 3.12;
- [uv](https://docs.astral.sh/uv/);
- biblioteca nativa ecCodes;
- token NASA Earthdata para IMERG;
- chave de uma API compatível com OpenAI para a etapa editorial.

No macOS, instale ecCodes com `brew install eccodes`. Em Debian/Ubuntu, use os
pacotes `libeccodes0` e `libeccodes-data`. A imagem Docker já os inclui.

## Início rápido

```bash
cp .env.example .env
uv sync --python 3.12 --extra dev --extra postgres
uv run resumo-meteo init-db
uv run resumo-meteo doctor
```

Preencha no `.env` o token NASA e a configuração do provedor editorial. O
adaptador aceita endpoints compatíveis com a API OpenAI.

Para validar dados estáticos, ecCodes e configuração sem criar o banco:

```bash
uv run resumo-meteo doctor --skip-database
```

## Execução

```bash
# Pipeline completo para a data local de hoje
uv run resumo-meteo run --cycle 00 --show

# Data histórica e instante de corte reproduzíveis
uv run resumo-meteo run \
  --date 2026-07-13 \
  --cycle 00 \
  --cutoff 2026-07-13T15:00:00Z \
  --show

# Somente ingestão e normalização
uv run resumo-meteo ingest --date 2026-07-13 --cycle 00

# Construir o pacote sem chamar o provedor editorial
uv run resumo-meteo build-packet --date 2026-07-13 --cycle 00 --json

# Regerar o texto a partir de artefatos persistidos
uv run resumo-meteo generate --date 2026-07-13 --cycle 00 --show

# Estado e limpeza segura
uv run resumo-meteo status --date 2026-07-13 --cycle 00 --json
uv run resumo-meteo cleanup --dry-run --json
```

Um `--cutoff` explícito congela a visão temporal dos dados e torna a seleção de
fontes reproduzível.

## Saídas e persistência

Por padrão, metadados ficam em `var/resumo_meteo.sqlite3`, artefatos derivados
em `var/artifacts/` e publicações em:

```text
var/outputs/AAAA-MM-DD/00/run-<id>-attempt-<n>/
├── summary.txt
├── analysis_packet.json
├── source_manifest.json
├── quality.json
├── generation.json
├── validation.json
└── manifest.json
```

GRIB e HDF são temporários e removidos em blocos `finally`, inclusive em caso
de falha. O SQLite é apropriado para um único processo local. Para contêineres,
agendamento ou concorrência, use PostgreSQL; `docker-compose.yml` fornece uma
configuração de desenvolvimento.

## Docker

```bash
docker build -t open-resumo-meteo:local .
docker run --rm --env-file .env -v "$PWD/var:/app/var" \
  open-resumo-meteo:local doctor --json
```

## Desenvolvimento

```bash
make install
make lint
make test
```

Os testes são determinísticos e não fazem chamadas de rede. Alterações em
regras meteorológicas, contratos, retenção ou validação editorial devem incluir
testes.

## Segurança, licenças e contribuição

Nunca versione `.env`, tokens, chaves, bancos ou produtos de runtime. Consulte
[SECURITY.md](SECURITY.md) para reportar vulnerabilidades.

O código-fonte é distribuído sob a licença MIT. Dados e produtos de terceiros
mantêm os termos de seus provedores e não são relicenciados pela MIT; consulte
[NOTICE.md](NOTICE.md) antes de redistribuir os ativos. Para contribuir, leia
[CONTRIBUTING.md](CONTRIBUTING.md).
