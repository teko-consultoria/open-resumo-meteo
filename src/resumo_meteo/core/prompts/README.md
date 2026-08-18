# Prompt editorial do resumo meteorológico

O pacote contém somente o bundle vigente, identificado por
`weather-summary-editorial-v31` em `openai_client.py`:

- `system_window_v19.txt` e `execution_window_v9.txt`: geração por janela;
- `system_repair_window_v5.txt` e `execution_repair_window_v1.txt`: reparo por janela.

A aplicação faz uma chamada independente por janela de previsão e executa até
duas passagens de reparo local apenas quando necessário. Cada passagem recebe o
texto e o erro determinístico mais recentes, sem reescrever seções já aceitas.
A auditoria desse fluxo usa `weather-summary-generation.v3`. A relação exata
entre o ID do bundle e seus quatro arquivos está centralizada em
`PROMPT_BUNDLE`; os SHA-256 vigentes ficam em `PROMPT_BUNDLE_SHA256` e são
verificados pelo comando `doctor`. Não existe caminho de geração ou reparo
integral: as três seções aprovadas são concatenadas e passam por uma validação
final determinística antes da publicação.

O brief editorial `weather-summary-editorial-brief.v10` separa explicitamente a
chuva observada nos cinco dias antecedentes do início diário da primeira
pêntada. Quando a previsão já tem chuva material no D+1, geração, reparo e
validação exigem uma redação que situe o retorno no primeiro dia, sem sugerir
uma transição seca para úmida dentro da própria janela.

O bundle também exige referências temporais concretas: descreve primeiro a
pêntada atual, trata a pêntada anterior como previsão e reserva `observado` aos
cinco dias antecedentes. Códigos internos como `sinal diário material`,
comparações abstratas entre eventos e retomadas vagas como `Esse mecanismo` são
rejeitados localmente e enviados ao reparo por janela.

O bundle vigente expõe no brief a ordem geográfica das bacias editoriais, do
Sul para o Norte. Geração, reparo e validação determinística exigem essa
sequência em cada frase. Para evitar reordenação acidental, nomes de bacias são
reservados a uma única frase de acumulados, copiada integralmente do trecho
autorizado; a narrativa restante usa regiões.

O brief também inclui `mechanism_policy` em cada janela. Quando seu valor é
`omit`, geração e reparo devem descrever somente a distribuição e a evolução da
chuva. O validador rejeita causas sinóticas genéricas, inclusive `persistência
de umidade` e `circulação em grande escala`, que não substituem um mecanismo
nomeado pelos classificadores estruturados.
