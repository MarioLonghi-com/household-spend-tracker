---
id: import-transfers
title: Como as transferências importadas são casadas
screens: [import, transfers]
also:
  - por que minha transferência não foi vinculada
  - a importação vinculou a transferência errada
  - transferência aguardando o outro extrato
  - dinheiro para a poupança contado como gasto
  - transferência sugerida na importação
---

Dinheiro que passa entre duas contas suas aparece nos extratos dos dois bancos, muitas vezes importados com dias de diferença. Vinculadas como uma transferência, as duas linhas ficam fora de Receitas e despesas. É assim que uma importação decide. Para vincular à mão, veja [Mover dinheiro entre as suas próprias contas](help:transfers).

## Um par candidato

Duas linhas em contas diferentes, na mesma moeda, com valores exatamente opostos e datas com no máximo 5 dias de diferença.

Uma linha com categoria não é uma transferência. Ela nunca é pareada, nunca é vinculada na confirmação e nunca fica aguardando, e a categorização acontece antes da correspondência, então uma linha que recebe categoria pela regra do favorecido ou pelo histórico fica de fora. Limpe a categoria e ela volta a ser considerada.

## Vinculadas na confirmação sem perguntar

Um par é vinculado quando a importação é confirmada se as duas condições valem:

- uma linha cita a outra conta (o número, o IBAN, o cartão ou um nome que o banco usa para ela; veja [identificadores de conta](help:account-identifiers)), ou essas duas contas já tiveram transferências vinculadas antes, por uma linha que citava o outro lado ou por você. Um vínculo feito só com base nesse histórico não conta;
- *e* nenhuma outra linha poderia ser o par dela, ou esta é estritamente a mais próxima na data, ou, quando a data não distingue duas delas, as duas descrições compartilham estritamente mais palavras do que as de qualquer rival, sem contar palavras como *TO*, *FROM* ou os números das suas contas.

Um vínculo que cria histórico entre duas contas torna fortes também os outros pares delas, então esses são vinculados na mesma confirmação, e pelo *Link all* da tela [Transferências](screen:transfers), que vai vinculando até não sobrar nada novo que seja forte.

## Sugeridas em vez disso

- Quando só os valores e as datas batem, ou quando uma linha poderia formar par com mais de uma, o par é sugerido. A prévia avisa, e a tela Transferências é onde você confirma.
- Nunca vinculadas sem perguntar, por melhor que seja a correspondência: dinheiro que sai de um cartão de crédito (uma compra, e o estorno ou cashback dela não é uma transferência), e uma linha cujo favorecido tem uma regra de categoria ou cujo texto cita alguém que já te pagou antes, como o reembolso de despesas de um empregador. Essas são sugeridas.

## Pares que você recusou

Um par que você disse que não é transferência, desvinculando-o ou com **Não é transferência** na tela Transferências, nunca mais é casado. Desfazer isso no [Histórico](screen:history), ou vincular o par você mesmo, o traz de volta.

## Aguardando o outro extrato

Quando uma linha cita uma das suas contas ou um membro da casa e o outro lado ainda não foi importado, ela fica aguardando. É vinculada quando esse extrato chegar.

## Duas moedas

Duas moedas nunca são casadas automaticamente, porque os valores diferem por uma cotação que ninguém imprimiu. Selecione as duas linhas no [registro](screen:register) e escolha **Vincular como transferência**; a cotação é calculada e guardada.
