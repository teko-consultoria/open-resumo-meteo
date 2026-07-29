# Prompt editorial do resumo meteorológico

O pacote contém somente o bundle vigente, identificado por
`weather-summary-editorial-v27` em `openai_client.py`:

- `system_window_v15.txt` e `execution_window_v9.txt`: geração por janela;
- `system_repair_window_v1.txt` e `execution_repair_window_v1.txt`: reparo por janela.

A aplicação faz uma chamada independente por janela de previsão e executa o
reparo apenas quando necessário. A relação exata entre o ID do bundle e seus
quatro arquivos está centralizada em `PROMPT_BUNDLE`. Não existe caminho de
geração ou reparo integral: as três seções aprovadas são concatenadas e passam
por uma validação final determinística antes da publicação.
