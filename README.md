# Siteblock

Bloqueia sites no Linux pelo `/etc/hosts`, com agenda por dia da semana e liberação temporária de 1 a 15 minutos. Tudo roda localmente: uma interface no navegador (`http://127.0.0.1:8377`) e um comando `siteblock` no terminal.

É um arquivo só, [siteblock.py](siteblock.py), sem dependências além do Python. O logo, [logo-block-site.jpg](logo-block-site.jpg), é opcional: se estiver na mesma pasta na hora da instalação, vira o ícone da aba e do atalho no menu.

## O que ele faz

- **Lista de sites**: adicionar, trocar o endereço e remover. Dá pra colar uma URL inteira; ele extrai só o domínio.
- **Agenda semanal**: para cada dia, os intervalos em que o site fica **bloqueado**. Por exemplo, bloqueado de segunda a sexta das 09:00 às 18:00 e livre no fim de semana. Na interface você arrasta na barra do dia ou digita os horários.
- **Liberar por X minutos**: libera o site por 1 a 15 minutos, mesmo dentro de um horário bloqueado. Depois o bloqueio volta sozinho.
- **Espera entre liberações**: depois de uma liberação, o site só pode ser liberado de novo após 3 vezes o tempo que ficou liberado. Liberou por 10 minutos, espera 30 minutos depois que a liberação acabar. Se você encerrar antes, vale o tempo que realmente usou: encerrou com 2 minutos, espera 6. Enquanto isso, o botão de liberar some e no lugar aparece quanto falta.
- **Bloquear agora**: encerra a liberação na hora e derruba as conexões que já estavam abertas com o site. Se a agenda já deixa o site livre naquele horário, o botão aparece como "Encerrar liberação".
- **Extensão do navegador (opcional)**: mostra uma janelinha no canto do site com quanto falta para ele ser bloqueado e, no lugar da tela "Não é possível acessar esse site", uma página dizendo que o site está bloqueado e em quanto tempo pode ser liberado de novo.

## Requisitos

- Linux com systemd
- Python 3.10 ou mais novo
- `ss` (pacote `iproute2`, já vem na maioria das distros)

## Instalação

Na pasta onde está o `siteblock.py`, a partir do seu usuário (não como root):

```bash
sudo python3 siteblock.py install
```

Depois abra `http://127.0.0.1:8377` ou procure **Siteblock** no menu de aplicativos. Os serviços iniciam sozinhos com o PC.

### Extensão do navegador

O `/etc/hosts` bloqueia antes de o navegador chegar a qualquer página, então só uma extensão consegue mostrar o timer. A instalação grava a extensão em `/usr/local/share/siteblock/extensao`. Para carregar no Chrome ou Chromium:

1. Abra `chrome://extensions`.
2. Ligue o **Modo do desenvolvedor**, no canto superior direito.
3. Clique em **Carregar sem compactação** e escolha `/usr/local/share/siteblock/extensao`.

Com ela, em um site da lista:

- **Liberado, com bloqueio marcado para daqui a até 1 hora**: aparece uma janelinha no canto inferior direito com o tempo restante. Ela fica vermelha no último minuto e um clique a deixa menor. Quando o tempo acaba, a aba vai para a página de bloqueio.
- **Bloqueado**: em vez do erro do navegador, aparece uma página com o motivo e, se houver espera, em quanto tempo o site pode ser liberado de novo. Quando o site é liberado, a página volta sozinha para o endereço que você tentou abrir.

A extensão só lê o estado em `127.0.0.1:8377`; ela não libera nem bloqueia nada. O bloqueio continua no hosts, com ou sem ela. Ao atualizar o Siteblock, clique no botão de recarregar no cartão da extensão em `chrome://extensions`.

Para gravar a extensão em outra pasta: `siteblock extension ~/siteblock-extensao`.

> **Antes de usar:** apague do `/etc/hosts` as linhas de bloqueio que você colocou à mão. Enquanto elas estiverem lá, o site continua bloqueado mesmo quando a agenda libera. A interface avisa quando encontra esse conflito.

### Atualizar

Baixe o `siteblock.py` novo e rode o mesmo comando de instalação. Sua lista e seus horários são mantidos.

### Desinstalar

```bash
sudo siteblock uninstall
```

Isso remove os serviços, o atalho, a extensão em `/usr/local/share/siteblock` e o bloco do Siteblock no `/etc/hosts`. Remova também a extensão em `chrome://extensions`. A lista continua em `/var/lib/siteblock/` e uma cópia do hosts original fica em `/etc/hosts.siteblock.bak`.

## Uso pelo terminal

Tudo o que a interface faz também está disponível na linha de comando. Nenhum destes comandos precisa de `sudo`.

```bash
siteblock list                      # mostra cada site e o estado atual
siteblock add youtube.com reddit.com # adiciona (bloqueados o tempo todo)
siteblock edit reddit.com old.reddit.com
siteblock rm reddit.com
siteblock unblock youtube.com 10    # libera por 10 minutos (1 a 15), se não estiver na espera
siteblock block youtube.com         # encerra a liberação
siteblock extension PASTA           # grava a extensão do navegador em PASTA
```

### Agenda

`siteblock schedule DOMÍNIO DIAS=HORÁRIOS ...` define os horários **bloqueados**:

```bash
# bloqueado em horário comercial durante a semana, livre no fim de semana
siteblock schedule youtube.com seg-sex=09:00-18:00 sab,dom=livre

# dois intervalos no mesmo dia
siteblock schedule reddit.com seg-sex=08:00-12:00,13:00-18:00

# bloqueado o dia inteiro na quarta
siteblock schedule instagram.com qua=tudo
```

- Dias: `seg`, `ter`, `qua`, `qui`, `sex`, `sab`, `dom`. Aceita faixas (`seg-sex`) e listas (`sab,dom`).
- Horários: `HH:MM-HH:MM`. Use `livre` para nenhum bloqueio no dia e `tudo` para o dia inteiro.
- Dias não citados mantêm a agenda que já tinham.
- Para atravessar a meia-noite, divida em dois dias: `sex=22:00-24:00 sab=00:00-02:00`.

## Como funciona

São dois serviços systemd, separados de propósito:

| Serviço | Roda como | O que faz |
|---|---|---|
| `siteblock-enforcer` | root | A cada 2 segundos lê a agenda e reescreve o bloco do Siteblock no `/etc/hosts`. Quando um site passa a ficar bloqueado, derruba as conexões abertas com ele (`ss -K`). Não abre nenhuma porta. |
| `siteblock-ui` | seu usuário | Serve a interface em `127.0.0.1:8377`. Só edita o arquivo de configuração e nunca toca no hosts. |

O Siteblock só mexe no trecho entre estas marcas do `/etc/hosts`; o resto do arquivo fica como está:

```
# >>> siteblock (gerenciado automaticamente, não edite este bloco)
...
# <<< siteblock
```

A configuração fica em `/var/lib/siteblock/config.json`. Se ela estiver inválida, o enforcer mantém o hosts como está, em vez de liberar tudo.

## Limitações

- **Subdomínios**: bloqueia o domínio, `www.` e `m.`. Outros subdomínios, como `music.youtube.com`, precisam ser adicionados à parte, porque o hosts não aceita curinga.
- **Conteúdo de outros domínios**: só caem as conexões com o domínio listado. Um vídeo já aberto que vem de uma CDN pode continuar tocando até você bloquear esse domínio também.
- **Sites no mesmo servidor**: ao derrubar conexões, outro site que use o mesmo IP perde a conexão por um instante e reconecta sozinho. Uma chamada de vídeo nesse servidor pode cair brevemente.
- **DNS seguro (DoH)**: se o navegador usar DNS seguro, ele pode ignorar o `/etc/hosts`. O Firefox costuma exigir atenção; o bloqueio imediato foi testado só no Chromium.
- **Autocontrole, não cadeado**: a lista é editável pelo seu usuário sem `sudo`. Mudar a agenda ou remover e adicionar o site de novo contorna a espera entre liberações. Serve para segurar o impulso, não para impedir quem quer burlar.
- **Extensão**: só funciona no Chrome e no Chromium, e só no navegador onde estiver carregada. Em outros navegadores o bloqueio funciona igual, mas aparece a tela de erro comum.

## Problemas

**O site continua abrindo depois de bloqueado.**
Veja o log do enforcer, que registra quando não consegue derrubar uma conexão:

```bash
journalctl -u siteblock-enforcer -n 20
```

Confira também se não sobrou uma linha manual no `/etc/hosts` ou se o navegador está usando DNS seguro.

**A interface não abre.**

```bash
systemctl status siteblock-ui
```

## Variáveis de ambiente

Úteis para testes, sem precisar mexer no sistema real:

| Variável | Padrão |
|---|---|
| `SITEBLOCK_DIR` | `/var/lib/siteblock` |
| `SITEBLOCK_HOSTS` | `/etc/hosts` |
| `SITEBLOCK_PORT` | `8377` |

## Alternativas

Se preferir algo pronto (informações das páginas dos projetos, não testadas aqui):

- **LeechBlock NG**: extensão de navegador com agenda e liberação temporária. Vale só no navegador onde estiver instalada.
- **WebTimeout**: extensão do Firefox com agenda por dia e horário.
- **Censor**: bloqueia no sistema todo por dia e horário, sem liberação temporária.
- **Open Turkey**: feito para ser difícil de burlar. Linha de comando com systemd.
