---
id: attach-receipt
title: Anexar um comprovante a uma transação
screens: [receipts, register]
also:
  - adicionar a foto de um comprovante
  - enviar uma nota fiscal em PDF
  - ligar um comprovante a um pagamento
  - onde ficam guardados os comprovantes
  - guardar a foto original
  - comprovantes na caixa de entrada
---

Um comprovante é uma foto, um escaneamento ou um PDF guardado na transação como prova. Você pode anexá-lo direto à transação ou deixá-lo antes na caixa de entrada de comprovantes e fazer a correspondência quando o extrato chegar. Os dois caminhos são normais.

## Numa transação

1. Abra [Transações](screen:register) e clique na linha para abrir o painel dela.
2. Na seção **Comprovante**, escolha **Anexar comprovantes** e selecione um ou mais arquivos. Também dá para arrastar arquivos até o botão ou colar uma captura de tela com o painel aberto.
3. Espere cada arquivo terminar. O servidor o comprime, o que pode levar alguns segundos no caso de uma foto de celular.

Quando a transação já tem um comprovante, o painel oferece **Adicionar mais** para outro, **Substituir** para trocar o que está na tela e **Desanexar** para tirá-lo. Nem substituir nem desanexar apaga nada: o comprovante volta para a caixa de entrada. No registro, um clipe marca as linhas que têm comprovante.

## Pela tela de comprovantes

[Comprovantes](screen:receipts) é a caixa de entrada: tudo o que ainda não está numa transação. As fotos enviadas [do celular](help:snap-receipt) chegam aqui.

1. Escolha **Adicionar comprovantes** para enviar arquivos sem transação, ou clique num que já esteja esperando.
2. No painel dele, escolha **Anexar a uma transação**.
3. A lista abre nos dez dias antes e depois de quando a foto foi tirada, ou de quando foi enviada, se o arquivo não trouxer data. Encontre a linha e escolha **Anexar**.

**Caixa de entrada**, **Correspondida** e **Todos** alternam entre o que está esperando, o que já está anexado e tudo. **Notas do comprovante** é uma linha livre para dizer para que foi; na visualização **Lista**, você pode digitá-la direto na tabela.

## Onde os comprovantes ficam guardados

Os comprovantes ficam no banco de dados da própria casa, neste servidor, e em nenhum outro lugar. O que é guardado se escolhe na página da casa (clique no nome da casa no menu), em **Comprovantes**:

- *Só cópias legíveis*, a opção recomendada: uma cópia para a tela com no máximo 2000 pixels no lado maior e uma miniatura, as duas comprimidas em AVIF. Cerca de 33 KB por comprovante.
- **Guardar também o arquivo original**: além disso, o arquivo exato que saiu da câmera ou do scanner. Cerca de 2 MB por comprovante, umas 70 vezes o espaço.

Um PDF é sempre guardado exatamente como foi enviado, qualquer que seja a opção, junto com uma imagem da primeira página para mostrar na tela. A escolha vale só para comprovantes novos: nada do que já está guardado é convertido ou apagado. Se quem administra o servidor ativou os originais para todas as casas, a página avisa e a escolha não pode ser mudada.

**Baixar** entrega o original quando ele foi guardado e, se não, a cópia da tela. **Mais informações** mostra quando e onde a foto foi tirada, lido da própria foto.

## Bom saber

- Formatos aceitos: JPEG, PNG, HEIC, WebP, AVIF, GIF, TIFF, BMP e PDF, até 25 MB cada.
- O mesmo arquivo duas vezes na mesma transação, ou duas vezes na caixa de entrada, é recusado. O mesmo arquivo em duas transações diferentes é permitido, para uma conta que cobre dois pagamentos, e o painel avisa que ele também está na outra.
- Quando você [divide uma transação](help:split-transaction), os comprovantes dela vão para todas as partes.
- Excluir um comprovante na tela de comprovantes pode ser desfeito no [Histórico](screen:history).
