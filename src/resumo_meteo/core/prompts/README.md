# Prompt editorial do resumo meteorológico

O pacote contém somente o bundle vigente, identificado por
`weather-summary-editorial-v28` em `openai_client.py`:

- `system_window_v16.txt` e `execution_window_v9.txt`: geração por janela;
- `system_repair_window_v2.txt` e `execution_repair_window_v1.txt`: reparo por janela.

A aplicação faz uma chamada independente por janela de previsão e executa o
reparo apenas quando necessário. A relação exata entre o ID do bundle e seus
quatro arquivos está centralizada em `PROMPT_BUNDLE`; os SHA-256 vigentes ficam
em `PROMPT_BUNDLE_SHA256` e são verificados pelo comando `doctor`. Não existe
caminho de geração ou reparo integral: as três seções aprovadas são concatenadas
e passam por uma validação final determinística antes da publicação.

O brief editorial `weather-summary-editorial-brief.v8` separa explicitamente a
chuva observada nos cinco dias antecedentes do início diário da primeira
pêntada. Quando a previsão já tem chuva material no D+1, geração, reparo e
validação exigem uma redação que situe o retorno no primeiro dia, sem sugerir
uma transição seca para úmida dentro da própria janela.
