---
id: payee-rules
title: Nomear e categorizar favorecidos automaticamente
screens: [rules, payee-categorisation, payees]
also:
  - regras de favorecidos
  - mudar o nome que o banco dá a uma loja
  - a mesma loja aparece com vários nomes
  - colocar um favorecido sempre na mesma categoria
  - unir dois favorecidos
  - parar de adivinhar a categoria de um favorecido
---

Cada linha importada passa por duas etapas. Uma regra em **Regras de nome de favorecidos** transforma as palavras do banco em um favorecido, e depois a configuração desse favorecido em **Categorização de favorecidos** dá a categoria da transação. **Unir favorecidos** junta duas grafias do mesmo favorecido. As três telas ficam em **Favorecido** no menu da sua casa.

## Criar uma regra de nome

1. Abra [Regras de nome de favorecidos](screen:rules) e toque em **Adicionar uma regra**.
2. Em **E depois**, escolha **chamar de um só favorecido** para uma loja, ou **tirar este trecho e ver o que sobra** para um meio de pagamento cujas palavras aparecem antes de muitas lojas diferentes.
3. Em **Quando a linha do extrato**, escolha **contém**, **começa com**, **é exatamente** ou **corresponde ao padrão** (uma expressão regular) e digite as palavras do banco em **Este texto**.
4. Para uma loja, digite o nome dela em **Chamar o favorecido de**. Para tirar um trecho, normalmente deixe **Colocar isto no lugar (deixe vazio para removê-lo)** vazio.
5. Toque em **Com o que isto corresponderia?** para ver quantas linhas que já estão no livro-caixa ela pegaria, com exemplos.
6. Toque em **Criar**.

Uma regra compara *as palavras do próprio banco*, não o favorecido que o registro mostra. Abra uma transação e toque em **De onde veio isto?** para ver exatamente o que chegou. Maiúsculas e minúsculas não importam. As regras que tiram trechos rodam primeiro, cada uma sobre o que a anterior deixou; depois vale a primeira regra de nome que corresponder. **Ordem (a menor roda primeiro)** define a ordem. Desmarque a caixa **Ativa** de uma regra para pausá-la; **Excluir** a remove na hora.

## Aplicar as regras ao que já está no registro

As regras rodam quando um extrato é lido, então uma regra nova não muda nada que já esteja no registro. Logo depois de **Criar**, abre o painel **Aplicar as regras ao que já está aqui**: ele mostra quantas linhas mudariam e para qual favorecido antes de você confirmar. **Só linhas que ninguém corrigiu** deixa em paz um favorecido que alguém escolheu à mão. Tudo vira uma só entrada no [Histórico](screen:history), então um único desfazer devolve todas as linhas.

Quando o banco manda a mesma loja com uma referência diferente a cada vez, aparece um cartão chamado **Estes parecem ser a mesma loja**. **Criar esta regra** começa uma regra com o texto já preenchido.

## Escolher a categoria de um favorecido

1. Abra [Categorização de favorecidos](screen:payee-categorisation). A coluna **Categorizado como** mostra onde cada favorecido foi classificado até agora.
2. Toque em **Categorização** na linha do favorecido.
3. Escolha **Categorizar automaticamente pelo histórico do favorecido** (a categoria mais comum das três últimas transações dele), **Sempre usar minha escolha** e uma categoria, ou *Não categorizar automaticamente*, que deixa as novas transações em branco.
4. Toque em **Salvar**.

Isso decide a categoria das novas transações do favorecido. Uma categoria arquivada não pode ser a escolha fixa.

## Unir duas grafias

1. Abra [Unir favorecidos](screen:payees) e use **Encontrar um favorecido**.
2. Toque em **Unir…** na grafia que você quer eliminar.
3. Em **Para**, escolha o favorecido que fica e toque em **Unir**.

Todas as transações e regras que citavam o primeiro favorecido passam a citar o que ficou, e o primeiro deixa de existir. Dá para desfazer no Histórico. Favorecidos que representam uma transferência não podem ser unidos. Favorecidos que só diferem em acentos, traços ou espaços invisíveis aparecem em **Grafias de um nome**; toque em **Revisar…** e escolha em **Manter** a grafia que fica.

Para organizar as próprias categorias, veja [Organizar categorias e grupos](help:categories); para a importação em si, veja [Importar um extrato](help:import-statement).
