---
id: transfers
title: Mover dinheiro entre suas próprias contas
screens: [transfers, register]
also:
  - registrar uma transferência
  - vincular duas linhas como transferência
  - transferência entre moedas
  - dinheiro para a poupança aparece como despesa
  - desvincular uma transferência
  - não é transferência
---

Uma transferência é dinheiro que passa de uma conta da casa para outra. É um único ato feito de duas linhas, uma em cada conta, e não tem categoria: não é despesa nem receita, por isso fica fora de [Receitas e despesas](screen:report-income-expense).

## Registrar uma transferência manualmente

1. Abra [Transações](screen:register), clique em **Adicionar transação** e escolha **Transferência**.
2. Em **De**, escolha a conta de onde o dinheiro sai; em **Para**, a conta onde ele chega.
3. Defina a **Data**, digite o valor que sai sem sinal de menos e, se quiser, preencha a **Descrição**.
4. Clique em **Transferir**.

As duas linhas são gravadas juntas, cada uma com *Transfer :* e o nome da outra conta como favorecido. Mudar o valor de um lado muda o outro também, e desfazer o ato no [Histórico](screen:history) remove as duas. O formulário precisa de duas contas abertas.

## Entre duas moedas

Quando as contas estão em moedas diferentes, o formulário pede também o valor que chega. Digite o valor que o banco realmente creditou: o aplicativo nunca inventa uma taxa. Ele mostra a taxa que os dois valores dão e a guarda no par.

## Vincular duas linhas que já existem

Quando o extrato de cada banco trouxe o seu lado, transforme as duas linhas em uma transferência:

1. Em [Transações](screen:register), marque a saída em uma conta e a entrada na outra.
2. Clique em **Vincular como transferência** na barra que aparece.

As duas precisam estar em contas diferentes e ir em sentidos opostos. Na mesma moeda, os valores têm de ser exatamente iguais; entre moedas, podem ser diferentes, e a taxa entre eles é calculada e guardada. Cada linha mantém sua própria data e a descrição do banco, e perde a categoria. Uma linha que faz parte de uma divisão ou que é despesa de trabalho não pode ser vinculada. Para desfazer, marque um dos lados e clique em **Desvincular transferência**.

## A tela de transferências

As importações vinculam os pares de que têm certeza durante a importação (veja [como as importações encontram transferências](help:import-transfers)). A tela [Transferências](screen:transfers) mostra o resto, apenas na mesma moeda:

- **Com certeza**: uma linha cita a outra conta, ou essas duas contas já tiveram transferências vinculadas antes. *Vincular todas* vincula todos os pares da seção.
- **Vale a pena ver**: os valores coincidem e as datas são próximas, mas nada cita a outra conta. Clique em **Vincular** se for mesmo uma transferência, ou em **Não é transferência** para que o par não seja oferecido de novo. Marque vários e clique em **Vincular selecionados** para vinculá-los de uma vez.
- **Aguardando o outro extrato**: linhas que citam uma das suas contas e cujo outro lado ainda não está no livro-caixa. Elas são vinculadas quando esse extrato for importado. Se uma delas não for transferência, dê uma categoria a ela em **Não é transferência — categorizar como…**.
- **Vinculadas só pelo histórico**: vínculos que nenhuma pessoa escolheu. Clique em **Desvincular** se um estiver errado, ou em **Manter** para confirmá-lo.
