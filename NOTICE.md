# Avisos sobre dados e produtos de terceiros

A licença MIT deste repositório cobre somente o código e a documentação
produzidos para o Open Resumo Meteo. Dados meteorológicos, climatologias,
geometrias derivadas e nomes de produtos continuam sujeitos aos termos de seus
respectivos provedores. A inclusão de um arquivo neste repositório não altera
esses termos.

## ECMWF Open Data

O projeto acessa previsões IFS e ENS publicadas pelo European Centre for
Medium-Range Weather Forecasts (ECMWF).

Attribution notice:

> This service is based on data and products of the European Centre for
> Medium-Range Weather Forecasts (ECMWF).

Os dados abertos do ECMWF são disponibilizados sob CC BY 4.0. Consulte os
[termos e licenças oficiais](https://apps.ecmwf.int/datasets/licences/general/)
antes de redistribuir dados ou produtos derivados. O ECMWF não endossa este
projeto e não oferece garantia sobre a adequação do produto resultante.

## NOAA/NCEP

O projeto acessa GFS, GEFS e uma climatologia derivada do NCEP/NCAR Reanalysis
1. Materiais do governo federal dos Estados Unidos são em geral de domínio
público, salvo indicação específica em contrário. A NOAA solicita crédito como
fonte e proíbe sugerir endosso. Consulte as
[orientações do arquivo NCEI](https://www.ncei.noaa.gov/index.php/archive).

## NASA GPM IMERG

O projeto acessa GPM IMERG V07 Daily e Early por meio do NASA Earthdata. Os
dados GPM são disponibilizados livremente, com solicitação de reconhecimento
da fonte. Consulte a [política oficial do GPM](https://gpm.nasa.gov/data/policy)
e os metadados da coleção usada.

Crédito sugerido: NASA Global Precipitation Measurement (GPM) mission, IMERG
Version 07.

## MERGE/INPE

A climatologia de precipitação incluída é o arquivo derivado
`merge_inpe_2001_2020.climatology.json`. Ele não contém as grades meteorológicas
originais: contém estatísticas pré-calculadas por bacia e data do calendário,
derivadas de séries diárias do produto MERGE/INPE para 2001–2020. Consulte o
[portal de dados MERGE](https://data.inpe.br/dados/merge/) e cite:

Rozante, J. R.; Moreira, D. S.; de Goncalves, L. G. G.; Vila, D. A. Combining
TRMM and surface observations of precipitation: technique and validation over
South America. *Weather and Forecasting*, 25(3), 885–894, 2010.
[DOI 10.1175/2010WAF2222325.1](https://doi.org/10.1175/2010WAF2222325.1).

O arquivo derivado é distribuído para tornar a comparação climatológica
reprodutível, mas não altera os termos do produto de origem. Consulte também o
[termo de uso da plataforma do INPE](https://data.inpe.br/dados/termo-de-uso-e-politica-de-privacidade/).

## Contornos de bacias

Os GeoJSON foram derivados dos
[Contornos das Bacias Hidrográficas do ONS](https://dados.ons.org.br/dataset/dados-hidrologicos-horizontes-de-bacias-hidrograficas),
publicados como dados abertos sob Creative Commons Atribuição. A coleção foi
adaptada e agregada segundo a configuração SMAP revisada em abril de 2025. A
versão distribuída transcreve sem simplificação o conjunto usado nos mapas
gradeados.

Ao reutilizar esses arquivos, atribua o Operador Nacional do Sistema Elétrico
(ONS) como fonte e informe que os contornos foram convertidos para GeoJSON e
agregados/modificados por este projeto. O manifesto versionado registra hashes,
nomes de origem e a versão geométrica.
