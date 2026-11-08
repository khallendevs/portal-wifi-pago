<div align="center">

# Portal Wi-Fi Pago

**Portal cativo para administrar acesso Wi-Fi pré-pago em Android com Termux.**

Controle planos, pagamentos Pix, vouchers e sessões conectadas a partir de um painel web ou do terminal.

![Plataforma](https://img.shields.io/badge/plataforma-Android%20%7C%20Termux-3DDC84?logo=android&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)
![Licença](https://img.shields.io/badge/licen%C3%A7a-MIT-blue.svg)

</div>

---

## Visão geral

O Portal Wi-Fi Pago transforma um hotspot Android em um portal cativo: visitantes são direcionados a uma página de acesso, escolhem um plano e, após a confirmação do pagamento ou uso de um voucher, recebem conectividade pelo período correspondente.

O projeto foi pensado para execução no **Termux** e concentra o portal e a administração em um único script Python, sem dependências Python externas.

## Funcionalidades

- **Planos por duração e preço**, com configuração de valores padrão.
- **Pix via Mercado Pago** (automático) ou confirmação manual pelo administrador.
- **Vouchers pré-pagos**, incluindo geração em lote.
- **Modo de teste** para validar a instalação sem cobrar.
- **Painel web responsivo** com clientes, planos, pagamentos, vouchers, bloqueios e indicadores.
- **Administração por linha de comando** enquanto o portal está em execução.
- **Atualização integrada** com cópia de segurança e opção de reversão.
- **Proteções do painel**, incluindo hash de senha, limitação de tentativas e proteção CSRF.

> O controle de rede usa regras de `iptables` e requer acesso root no Android. Leia as limitações e os avisos de segurança antes de colocar o serviço em uso real.

## Requisitos

- Android com acesso root.
- [Termux](https://termux.dev/) instalado.
- Python 3 e ferramentas de rede disponíveis no Termux.

## Instalação

No Termux, instale os pacotes necessários:

```sh
pkg update -y
pkg install -y root-repo
pkg install -y python iptables tsu
```

Obtenha o arquivo `portal.py` e inicie o serviço:

```sh
WIFI_IFACE=wlan0 python portal.py
```

Substitua `wlan0` pela interface usada pelo hotspot, se necessário (verifique com `ip a`). Para iniciar apenas o servidor, use:

```sh
python portal.py servidor
```

O painel administrativo fica em `http://IP_DO_HOTSPOT:8080/admin` (ou `http://127.0.0.1:8080/admin` no próprio aparelho).

## Primeiro acesso: segurança obrigatória

**Antes de disponibilizar o hotspot, acesse o painel e troque a senha administrativa padrão (`admin123`) por uma senha forte e exclusiva.** Não exponha o painel administrativo diretamente à internet. Proteja também o aparelho, o hotspot e as credenciais de pagamento.

Para Pix automático, configure o token do Mercado Pago por variável de ambiente (`MP_TOKEN`); nunca grave tokens, senhas ou dados de clientes no código-fonte nem os envie ao Git. Este repositório não inclui configuração de produção ou credenciais.

## Configuração opcional

| Variável | Padrão | Descrição |
|---|---:|---|
| `WIFI_IFACE` | `wlan0` | Interface de rede do hotspot. |
| `PORTAL_PORT` | `8080` | Porta do portal e do painel. |
| `NOME_REDE` | `Wi-Fi Livre` | Nome exibido na página do cliente. |
| `PRECO` | `2.00` | Preço do plano padrão criado na primeira execução. |
| `MINUTOS` | `60` | Duração do plano padrão, em minutos. |
| `MP_TOKEN` | *(vazio)* | Token de acesso do Mercado Pago para Pix automático. |

## Comandos

```sh
python portal.py            # inicia o portal e o menu de administração
python portal.py servidor   # inicia somente o servidor
python portal.py ajuda      # mostra os comandos disponíveis
```

Consulte a ajuda exibida pelo próprio programa para os comandos de administração e atualização.

## Atualizações

O programa permite definir uma URL de atualização e mantém uma cópia de segurança antes de substituir a versão atual. Use apenas uma origem que você controla e confia; confirme a integridade do arquivo antes de atualizar.

## Limitações e responsabilidade

Este software é fornecido **“como está”**, sem garantia. O funcionamento depende da versão do Android/Termux, do acesso root, da interface de rede e da configuração do hotspot. Teste em ambiente controlado antes de qualquer uso comercial.

Verifique as leis e regras locais aplicáveis à revenda de acesso à internet, pagamentos, privacidade e tratamento de dados. O operador é responsável pela configuração segura e pelo cumprimento das obrigações aplicáveis.

## Contribuições

Relatos de bugs e melhorias são bem-vindos. Ao abrir uma issue, não inclua tokens, senhas, endereços IP públicos, dados de clientes nem outros dados pessoais.

## Licença e créditos

Distribuído sob a licença MIT; consulte [`LICENSE`](LICENSE). O README do pacote original atribui a ideia e o desenvolvimento a **khallendev**.
