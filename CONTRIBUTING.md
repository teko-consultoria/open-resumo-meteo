# Como contribuir

Obrigado por contribuir com o Open Resumo Meteo.

## Ambiente

Requisitos: Python 3.12, `uv` e ecCodes nativo.

```bash
git clone <url-do-fork>
cd open-resumo-meteo
make install
make lint
make test
uv run resumo-meteo doctor --skip-database
```

Os testes não devem depender de rede, credenciais ou da data atual.

## Mudanças meteorológicas

Uma alteração em fontes, horizontes, janelas, agregação espacial, ensembles,
consenso, climatologia ou diagnósticos precisa:

1. explicar a hipótese meteorológica no pull request;
2. preservar a independência entre as famílias NCEP e ECMWF;
3. incluir testes determinísticos para limites e casos incompletos;
4. atualizar README, manifestos e schemas quando o contrato mudar;
5. não introduzir uma nova fonte sem decisão explícita de arquitetura.

O pipeline aceita somente GFS, GEFS, IFS, ENS e IMERG. A rodada editorial é
sempre 00 UTC. O IFS determinístico não pode ser extrapolado além de 240 h.

## Dados e credenciais

Não envie GRIB, HDF, tokens, `.env`, bancos locais nem artefatos de execução.
Antes de adicionar dados derivados, documente origem, licença, método,
checksum, período e limitações em `NOTICE.md` e no manifesto correspondente.

## Estilo

- Formate e valide com Ruff.
- Prefira contratos tipados e serializáveis.
- Mantenha downloads brutos temporários e cleanup em `finally`.
- Use mensagens de commit objetivas.

Ao abrir um pull request, descreva testes executados e eventuais limitações.
