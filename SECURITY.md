# Segurança

## Uso seguro

- O acesso inicial de cinco minutos é limitado por endereço MAC, que pode ser alterado ou aleatorizado pelo cliente; não o use como proteção antifraude.
- Mantenha o checkout Mercado Pago desligado até que o administrador configure e teste o token e as confirmações no painel.
- Altere a senha administrativa padrão (`admin123`) antes de habilitar o acesso de clientes.
- Não exponha o painel `/admin` à internet pública.
- Mantenha o Android, Termux e os pacotes atualizados.
- Guarde o token do Mercado Pago em variável de ambiente e nunca o publique.
- Não compartilhe bancos de dados, backups ou logs que possam conter dados de clientes.
- A atualização automática executa código obtido da URL configurada depois de validar a sintaxe e a identidade do programa; use somente uma origem confiável e controle quem pode alterar essa URL.
- O atualizador guarda backups recentes e reinicia o portal ao aplicar uma mudança. Isso não substitui a revisão do código nem a proteção do painel administrativo.

## Relatar uma vulnerabilidade

Não publique detalhes de uma vulnerabilidade explorável em uma issue pública. Entre em contato de forma privada com o mantenedor pelo canal de contato disponível no perfil do GitHub. Inclua passos para reproduzir e o impacto, sem anexar credenciais ou dados pessoais.
