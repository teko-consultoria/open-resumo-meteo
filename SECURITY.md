# Política de segurança

## Versões suportadas

Enquanto o projeto estiver em estágio alpha, correções de segurança são
aplicadas somente à versão mais recente da branch principal.

## Como reportar

Não abra uma issue pública para vulnerabilidades que envolvam exposição de
credenciais, desvio de autenticação, escrita fora dos diretórios configurados,
injeção de prompt ou acesso indevido a dados.

Use o canal privado de segurança do repositório hospedado. Se ele ainda não
estiver configurado, contate o mantenedor por um canal privado e informe:

- versão ou commit afetado;
- impacto esperado;
- passos mínimos para reprodução;
- sugestão de correção, se disponível.

Nunca inclua tokens reais no relatório. O projeto tentará confirmar o
recebimento em até sete dias.

## Escopo sensível

São especialmente relevantes:

- vazamento do token NASA ou da chave do provedor editorial;
- envio de credenciais durante redirecionamentos HTTP;
- persistência acidental de GRIB/HDF ou conteúdo sensível;
- travessia de diretório em artefatos e publicações;
- texto editorial que contorne o pacote estruturado ou seu validador.
