---
id: account-identifiers
title: Dizer ao app como o seu banco chama uma conta
screens: [accounts, import]
also:
  - o extrato escolhe a conta errada
  - adicionar um IBAN a uma conta
  - identificadores de conta
  - transferências entre minhas contas não são vinculadas
  - como os bancos escrevem nossos nomes
  - identificadores sugeridos
---

Identificadores são os números e nomes que os bancos usam para as suas contas. É por eles que um arquivo de extrato encontra a conta dele na tela Importar, e que uma transferência encontra o outro lado.

## Adicionar um

1. Abra [Contas](screen:accounts) e pressione **Configurações** na conta.
2. Em **Como os bancos a chamam**, escolha o **Tipo** e digite o **Valor**.
3. Pressione **Adicionar**.

O nome de um membro da casa vai em **Como os bancos escrevem nossos nomes**, na própria tela Contas, porque não pertence a nenhuma conta. Se o nome de uma conta contiver o IBAN ou um número longo, o app oferece adicioná-lo para você.

## Os tipos

- `iban`, `number`, `card`: comparados ignorando espaços. Números curtos (menos de seis caracteres) só correspondem como palavra inteira.
- `alias`: um nome que o banco usa para a conta, como o nome de um cofrinho de poupança. Comparado por palavras inteiras.
- `file_tag`: o código que um banco coloca no nome do arquivo baixado. Usado só para reconhecer arquivos.
- `holder`: como um banco escreve o nome de um membro da casa. Não pertence a nenhuma conta: quer dizer "é o nosso próprio dinheiro se movendo". Os bancos escrevem o nome de uma pessoa de muitos jeitos, então um nome de duas palavras ou mais corresponde em qualquer ordem, com iniciais para algumas das palavras, e com a última palavra do texto cortada (quatro letras ou mais). Um nome de três palavras ou mais também corresponde quando duas delas aparecem juntas e uma tem quatro letras ou mais. Um nome de uma palavra só corresponde como essa palavra inteira. Um nome só faz uma transferência ficar *sugerida*, nunca vinculada por conta própria; a tela Contas mostra o que cada nome encontra.

## Identificadores sugeridos

A tela Contas também lista, em **Identificadores sugeridos**, identificadores que o seu registro já usa e que ninguém adicionou: palavras do outro lado de transferências que você vinculou, IBANs e números de cartão cujos dígitos verificadores conferem, números `A/C` e de sort code, nomes de cofrinho entre aspas, o próprio nome de uma conta e o código que todo arquivo de extrato de uma conta traz. Cada um diz quantas linhas o mencionam (**Menções**) e quantos pares de transferência adicioná-lo vincularia (**Vincularia**).

Nenhum é adicionado sozinho. Um identificador deixa transferências serem vinculadas sem perguntar, então um errado gera vínculos errados: cada um espera por **Adicionar** ou **Ignorar**, e um ignorado não é oferecido de novo.

Veja [como as transferências importadas são casadas](help:import-transfers) para o que um identificador muda, e [Como a importação lê um extrato](help:import-how-it-works) para como um arquivo é associado à conta dele.
