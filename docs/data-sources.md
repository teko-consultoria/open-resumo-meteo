# Fontes de dados

Este documento complementa o README com proveniência e tratamento técnico.
Termos de redistribuição ficam em [NOTICE.md](../NOTICE.md).

## NOAA GFS

- Provedor: NOAA/NCEP.
- Uso: precipitação determinística e campos sinóticos.
- Ciclo: 00 UTC.
- Precipitação: APCP por intervalo nativo de 6 h, somada para dias e janelas.
- Horizonte usado: 336 h, embora o catálogo genérico do modelo alcance além.
- Campos sinóticos: passos de 12 h no domínio 110°W–20°W, 60°S–15°N.
- Cliente: acesso por índice e byte ranges, evitando baixar arquivos globais
  inteiros.

Referências: [NOMADS](https://nomads.ncep.noaa.gov/) e
[arquivo NOAA/NCEI](https://www.ncei.noaa.gov/index.php/archive).

## NOAA GEFS

- Provedor: NOAA/NCEP.
- Uso: distribuição de precipitação da família NCEP.
- Membros: controle mais 30 perturbados, total 31.
- Ciclo: 00 UTC; horizonte usado: 336 h.
- Grade: 0,25° até 240 h e 0,5° depois desse limite.
- Tratamento: acumulados nativos de 6 h; cada membro permanece identificado.

## ECMWF IFS Open Data

- Identificador interno: `ec`.
- Uso: precipitação determinística e campos sinóticos.
- Ciclo: 00 UTC.
- Precipitação: total acumulado convertido para incrementos diários por
  diferença entre limites consecutivos.
- Horizonte: 240 h; não existe preenchimento em `p3`.
- Campos sinóticos: passos de 12 h no mesmo domínio usado pelo GFS.

Referência: [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data).

## ECMWF ENS Open Data

- Identificador interno: `ecens`.
- Uso: distribuição de precipitação da família ECMWF.
- Membros: 50 membros perturbados.
- Ciclo: 00 UTC; horizonte usado: 336 h.
- Tratamento: totais acumulados nos limites das janelas, convertidos por
  diferença; ruído pequeno de empacotamento é tolerado sem permitir chuva
  física negativa.

## NASA GPM IMERG V07

- Descoberta: NASA CMR.
- Autenticação: bearer do Earthdata, encaminhado apenas a hosts permitidos.
- Dias completos: `GPM_3IMERGDL`, resolução 0,1°, unidade mm/dia.
- Dia em andamento: `GPM_3IMERGHHE` Early, resolução temporal de 30 min.
- Domínio: 75,5°W–32°W e 35°S–6,5°N.
- Histórico operacional: até 30 dias completos anteriores à emissão.
- Tratamento: acumulado por célula, interseção ponderada com bacias e métricas
  de cobertura/completude.

O produto parcial nunca substitui nem completa artificialmente um dia
histórico. Referências: [GPM Data Directory](https://gpm.nasa.gov/data/directory)
e [política de dados GPM](https://gpm.nasa.gov/data/policy).

## Climatologia MERGE/INPE

- Período: 2001–2020, 7.305 registros diários de referência.
- Escopo: 12 bacias.
- Janelas históricas: 4 e 5 dias para cada data inicial do calendário.
- Distribuição: soma diária primeiro; quantis R7 depois.
- Estatísticas: média, mediana, P10, P25, P75 e P90.
- Qualidade: 732 janelas por bacia, checksums e método no manifesto.

As cinco bacias compostas usam média diária ponderada pela área das séries
históricas componentes. As máscaras antigas não foram disponibilizadas; por
isso a limitação geométrica fica explícita e nenhuma equivalência perfeita com
os contornos atuais é alegada.

Referência: [dados MERGE/INPE](https://data.inpe.br/dados/merge/).

## Climatologia de temperatura

- Fonte: NOAA/NCEP Reanalysis 1.
- Variável: temperatura do ar a 2 m.
- Período: 1991–2020.
- Resolução de uso: média mensal em oito regiões.
- Agregação: média regional ponderada pelo cosseno da latitude.

O arquivo é estático e não é baixado durante a execução. Ele classifica
anomalias regionais, não fornece normal diária para uma localidade.

## Geometrias de bacias

O catálogo contém 27 GeoJSON derivados dos
[Contornos das Bacias Hidrográficas do ONS](https://dados.ons.org.br/dataset/dados-hidrologicos-horizontes-de-bacias-hidrograficas),
disponibilizados como dados abertos sob Creative Commons Atribuição. Os
contornos foram convertidos e agregados segundo a configuração SMAP revisada em
abril de 2025. A versão `smap-aggregated-apr2025-grid-v2` transcreve, sem
simplificação, cada parte dos shapefiles usados operacionalmente nos mapas
gradeados de GFS, GEFS, IFS e ENS. O resultado usa EPSG:4326.

O manifesto fixa o conjunto por hash SHA-256 global, além do nome, hash,
quantidade de registros, partes e pontos de cada shapefile. A reprodução é
feita por `scripts/vendor_smap_basins.py`; os shapefiles de origem não são
dependência de execução.

O escopo editorial de 12 bacias é separado do catálogo para impedir que uma
bacia sem climatologia compatível apareça no texto.

As médias de grade usam a fração de área intersectada por célula. Pesos podem
ser recalculados deterministicamente a partir do layout da grade e dos
polígonos; matrizes antigas de outra versão geométrica não são reutilizadas. O
mesmo catálogo é obrigatório para previsão e chuva realizada IMERG. Agregados
compactos são reutilizados somente quando schema, versão geométrica, escopo e
inventário de bacias coincidem; caso contrário, a grade normalizada é
reagregada. O alinhamento garante os mesmos contornos, mas não promete
identidade numérica com outro produto que use apenas o centro dos pontos ou uma
ponderação espacial diferente.

## Retenção

GRIB e HDF existem apenas durante a normalização e são removidos mesmo quando
uma etapa falha. Persistem apenas CSV/JSON compactos, checksums, manifestos e
metadados necessários à auditoria.
