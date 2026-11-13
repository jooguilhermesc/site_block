# Política de Privacidade da extensão Siteblock

Última atualização: 13 de novembro de 2026

A extensão Siteblock não coleta, não guarda e não envia nenhum dado pessoal ou de navegação.

## O que a extensão faz

A extensão é um complemento do [Siteblock](https://github.com/jooguilhermesc/site_block), um bloqueador de sites que roda no seu próprio computador. Ela mostra no navegador quanto falta para um site da sua lista ser bloqueado e, quando ele está bloqueado, uma página explicando até quando.

## Quais dados ela acessa

- **O endereço da página aberta.** A extensão lê o endereço da aba e compara o domínio com a sua lista de sites. Essa comparação acontece dentro do navegador, e o endereço não é enviado a lugar nenhum.
- **O estado do Siteblock.** A extensão consulta o Siteblock em `http://127.0.0.1:8377`, um endereço que só existe dentro do seu computador. Ela recebe de lá a lista de sites e os horários de bloqueio. Nessa consulta, ela não envia nada sobre o que você está acessando.

## O que ela não faz

- Não lê o conteúdo das páginas: textos, imagens, formulários ou senhas.
- Não registra histórico de navegação.
- Não usa cookies, rastreadores nem serviços de análise.
- Não se comunica com nenhum servidor na internet.
- Não vende nem compartilha dados com terceiros, porque não tem dados para compartilhar.

## Onde ficam os seus dados

A lista de sites e os horários ficam no Siteblock, no seu computador, e não saem dele. A extensão só guarda essas informações na memória enquanto o navegador está aberto e não as salva em disco.

## Permissões

| Permissão | Para que serve |
|---|---|
| Acesso a todos os sites | Os sites da lista são escolhidos por você, então a extensão não sabe de antemão quais são. Ela precisa desse acesso para mostrar o timer e a página de bloqueio nesses sites. |
| `declarativeNetRequestWithHostAccess` | Trocar o erro de conexão de um site bloqueado pela página explicativa da extensão. |
| `webNavigation` | Perceber quando um site da lista começa a abrir ou não carrega, para mostrar a página de bloqueio. |
| `alarms` | Atualizar o estado do bloqueio a cada 30 segundos. |

## Mudanças nesta política

Se a extensão passar a tratar dados de outro jeito, esta página será atualizada antes da nova versão ser publicada, e a data no topo vai mudar.

## Contato

Dúvidas podem ser enviadas pelas [issues do projeto no GitHub](https://github.com/jooguilhermesc/site_block/issues).
