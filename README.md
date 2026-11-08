<div align="center">

# Portal Wi-Fi Pago

**Portal cativo e painel de administração de acesso Wi-Fi para Android com Termux.**

Conceda um acesso inicial de 5 minutos, acompanhe os dispositivos conectados e libere mais tempo manualmente. O Mercado Pago é opcional.

![Plataforma](https://img.shields.io/badge/plataforma-Android%20%7C%20Termux-3DDC84?logo=android&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)
![Licença](https://img.shields.io/badge/licen%C3%A7a-MIT-blue.svg)

</div>

---

## Visão geral

O Portal Wi-Fi Pago administra o acesso de dispositivos conectados a um hotspot Android. Cada endereço MAC pode iniciar **uma única sessão inicial de 5 minutos**. Depois, o cliente solicita tempo adicional ao administrador pelo WhatsApp; o administrador confirma e libera minutos pelo painel web ou terminal.

Se o administrador optar por habilitar o Mercado Pago, os planos e métodos de pagamento configurados também ficam disponíveis aos clientes. Com o Mercado Pago desligado, o checkout público fica suspenso e a liberação adicional é feita pelo administrador.

O portal e o painel estão concentrados em um script Python, sem dependências Python externas. O controle de rede usa `iptables`.

## Recursos

- Acesso inicial gratuito de **5 minutos, uma vez por endereço MAC**.
- Painel web para clientes conectados, sessões, bloqueios, dispositivos fixos e liberação manual de tempo.
- Botão de contato com o administrador via WhatsApp, usando o número configurado no painel.
- Mercado Pago opcional: o administrador pode ligar/desligar pelo painel ou terminal.
- Confirmação de pagamentos manuais no painel quando esse método está habilitado.
- Vouchers opcionais, se habilitados pelo administrador.
- Administração por linha de comando e atualização com backup/reversão.

> **Limitação:** a identificação do acesso inicial usa o endereço MAC observado na rede. Dispositivos que alteram ou aleatorizam o MAC podem ser reconhecidos como novos dispositivos e, portanto, voltar a receber a sessão inicial. Não trate isso como mecanismo antifraude.

## Requisitos

- Android com acesso root.
- [Termux](https://termux.dev/) instalado.
- Python 3, `iptables` e `tsu` instalados no Termux.

## Instalação

No Termux:

```sh
pkg update -y
pkg install -y root-repo
pkg install -y python iptables tsu
```

### Instalação prática pelo GitHub

No Termux, instale `curl` se ainda não estiver disponível e execute o instalador publicado no repositório:

```sh
pkg install -y curl
curl -fsSL https://raw.githubusercontent.com/khallendevs/portal-wifi-pago/main/install.sh | bash
```

O instalador instala as dependências, valida o script baixado, preserva uma cópia do script anterior e cria o comando `wifi-portal`. O banco em `~/wifi_portal.db` não é substituído. Para conferir o conteúdo antes de executá-lo, abra [install.sh no GitHub](https://github.com/khallendevs/portal-wifi-pago/blob/main/install.sh).

Inicie o serviço assim:

```sh
WIFI_IFACE=wlan0 wifi-portal servidor
```

Substitua `wlan0` pela interface do hotspot, se necessário (confira com `ip a`). O painel administrativo estará em `http://IP_DO_HOTSPOT:8080/admin` (ou `http://127.0.0.1:8080/admin` no aparelho).

Também é possível instalar manualmente baixando `portal.py` do repositório e executando `python portal.py servidor`.

## Configuração do WhatsApp

No painel, entre em **Sistema → Geral** e informe o número do administrador em formato internacional, por exemplo `+55 11 99999-9999`. O cliente verá um botão para abrir o WhatsApp com uma mensagem pré-preenchida.

Também é possível configurar pelo terminal:

```sh
python portal.py whatsapp +5511999999999
python portal.py whatsapp   # consulta o número salvo
```

Se o número não estiver configurado, o portal orienta o cliente a procurar o administrador, mas não exibe o botão do WhatsApp.

## Liberação e gestão de tempo

Após a sessão inicial, use **Admin → Clientes** para conferir as conexões. Para liberar mais tempo, informe o MAC do dispositivo e os minutos desejados; também é possível encerrar ou bloquear a sessão. O comando de terminal correspondente é:

```sh
python portal.py clientes
python portal.py tempo aa:bb:cc:dd:ee:ff 60
```

O terminal pode ser aberto com `python portal.py` para subir o serviço e apresentar o menu administrativo. `python portal.py ajuda` lista os comandos disponíveis.

O menu interativo tem identidade visual própria, cores no Termux, resumo de status e atalhos por categoria. Para abrir o portal com o console administrativo, use `wifi-portal`; para iniciar somente o servidor, use `wifi-portal servidor`. Também é possível executar diretamente comandos, por exemplo `wifi-portal clientes` ou `wifi-portal verificar`.

## Mercado Pago opcional

O Mercado Pago começa **desligado** em instalações novas e também é desligado na migração desta versão, preservando o token e o histórico. Para habilitá-lo no painel, abra **Pagamentos**, informe o token de acesso e marque **Ativar**. O token não é exibido novamente; deixe o campo vazio para manter o valor salvo.

Pelo terminal, salve o token e ative/desative separadamente:

```sh
python portal.py mp-token          # solicita token sem mostrá-lo na tela
python portal.py metodo mp on      # ativa o Mercado Pago
python portal.py metodo mp off     # desativa novas compras online
python portal.py metodos           # confere o estado atual
```

Desativar o método impede novas compras, mas não apaga registros de pagamentos já existentes. Para que o status de um pagamento pendente continue sendo consultado, mantenha o token válido até resolver os pagamentos em aberto.

## Atualizações automáticas

Por padrão, o portal compara o conteúdo de `main/portal.py` no GitHub a cada **15 minutos**. Quando detectar código diferente, valida a sintaxe e a identidade do programa, guarda uma cópia de segurança, instala o arquivo e reinicia o serviço; as sessões e o banco ficam preservados. A verificação inclui mudanças no código mesmo quando o número da versão não foi alterado. Se o download ou a validação falhar, o portal registra o erro no painel e tenta novamente depois.

Desative ou reative a instalação automática em **Admin → Sistema**. Pelo terminal, use `python portal.py config upd_auto 0` para desligar ou `python portal.py config upd_auto 1` para ligar. Mantenha a URL de atualização apontando para uma origem confiável: a instalação automática executa o script recebido após a validação básica.

O endereço padrão e o endereço antigo conhecido do Dropbox são migrados para `https://raw.githubusercontent.com/khallendevs/portal-wifi-pago/main/portal.py`; confira em **Admin → Sistema** que esse é o link exibido.

## Variáveis de ambiente

| Variável | Padrão | Descrição |
|---|---:|---|
| `WIFI_IFACE` | `wlan0` | Interface de rede do hotspot. |
| `PORTAL_PORT` | `8080` | Porta do portal e do painel. |
| `NOME_REDE` | `Wi-Fi Livre` | Nome exibido na página do cliente. |
| `PRECO` | `2.00` | Preço do plano criado na primeira execução. |
| `MINUTOS` | `5` | Duração do plano padrão criado na primeira execução. |
| `MP_TOKEN` | *(vazio)* | Token Mercado Pago; sua presença não ativa o recurso automaticamente. |

## Segurança

- Troque a senha administrativa padrão (`admin123`) antes de usar o portal.
- Não exponha `/admin` diretamente à internet; prefira restringir o acesso ao aparelho administrador.
- Guarde o token do Mercado Pago e os arquivos locais de banco/backup com permissões restritas.
- Configure o WhatsApp correto e teste o link antes de divulgar o hotspot.
- Use atualizações somente de uma origem confiável.
- Não publique tokens, senhas, bancos de dados, logs com dados pessoais ou informações de clientes.

Consulte [`SECURITY.md`](SECURITY.md) para mais orientações.

## Responsabilidade e licença

Software fornecido **“como está”**, sem garantia. O funcionamento depende do Android, do Termux, de acesso root e da configuração de rede. Teste em ambiente controlado. Verifique as leis locais aplicáveis à revenda de acesso à internet, pagamentos e privacidade.

Distribuído sob licença MIT; consulte [`LICENSE`](LICENSE). O pacote original atribui a ideia e o desenvolvimento a **khallendev**.
