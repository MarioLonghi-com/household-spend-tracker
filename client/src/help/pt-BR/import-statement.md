---
id: import-statement
title: Importar um extrato
screens: [import, register]
also:
  - enviar um extrato bancário
  - trazer as transações do banco
  - colar linhas do site do banco
  - importar um arquivo CSV ou OFX
  - terminar uma importação que deixei pela metade
  - desfazer uma importação
---

Um extrato entra numa conta de cada vez. Nada chega ao registro até você confirmar, e a importação inteira pode ser desfeita de uma só vez.

## Importar um arquivo

1. Abra [Importar](screen:import).
2. Em **Para qual conta**, escolha a conta a que o extrato pertence. Se o arquivo disser de qual conta é, o app a escolhe para você e diz por quê; se o arquivo indicar uma conta diferente da que você escolheu, ele avisa.
3. Escolha o arquivo em **Arquivo**: um CSV, um download OFX ou QFX, uma exportação .xls do banco ou um extrato em PDF. Se o seu banco só deixa copiar as linhas do site, cole-as na caixa de baixo.
4. Pressione **Ler o arquivo**. A prévia, **O que isto faria**, mostra o que foi detectado no arquivo e cada linha com o que vai acontecer com ela.
5. Confira as linhas. Clique na categoria de uma linha para mudá-la; digite qualquer parte do nome de uma categoria, ou esvazie a caixa para voltar à sugestão. Se outras linhas tiverem o mesmo favorecido, o app oferece dar a elas a mesma categoria e, com **Sim, todas e definir a regra**, continuar fazendo isso nos próximos extratos. **Não, só esta** muda apenas aquela linha.
6. Desmarque uma linha na coluna **Adicionar** para deixá-la de fora.
7. Quando uma linha aparecer como **Já existe** mas na verdade for outra transação, pressione **Não é a mesma coisa — adicionar à parte**. **Desfazer — adicionar como nova** volta atrás.
8. Pressione **Importar estas**. **Cancelar** fecha a prévia sem gravar nada.

Se o arquivo não indicar nenhuma conta, mas o nome dele trouxer um código que pode ser o marcador da conta, a tela oferece guardá-lo: pressione **Adicionar** para guardar ou **Ignorar**. Veja [identificadores de conta](help:account-identifiers).

## Importações deixadas pela metade

Um arquivo que você leu mas não importou fica em **Aguardando revisão**. Pressione **Abrir** para continuar de onde parou, ou **Descartar** para jogá-lo fora; nada dele chegou ao registro. Ler o mesmo arquivo de novo é recusado enquanto um estiver aguardando, com **Abrir essa importação** para ir até ele, **Importar mesmo assim** e **Descartar a que está preparada**.

## Desfazer uma importação

Uma importação é uma entrada no [Histórico](screen:history). Desfazê-la ali remove cada linha que ela adicionou, cada vínculo de transferência que fez e cada categoria que criou.

## Mais

- [Como a importação lê um extrato](help:import-how-it-works): formatos, verificações e o que cada resultado significa.
- [Como as transferências importadas são casadas](help:import-transfers).
- Vindo do YNAB? Traga todo o seu histórico de uma vez com a [Importação única](help:one-time-import).
