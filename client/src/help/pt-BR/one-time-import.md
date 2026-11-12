---
id: one-time-import
title: Trazer o seu histórico de outro app (YNAB)
screens: [household, import]
also:
  - importar do YNAB
  - mudar de outro app de orçamento
  - trazer todo o meu histórico de uma vez
  - token de acesso pessoal do YNAB
  - Register.csv do YNAB
  - desfazer a importação do YNAB
---

Importar extratos é um arquivo por conta, mês após mês. A **Importação única** é para o dia em que você se muda para cá vindo de outro app de orçamento: ela traz todo o histórico desse app de uma vez (transações, contas, categorias e transferências) em vez de extrato por extrato. O YNAB é o único fluxo por enquanto.

## Como fazer

Ela fica na [página da casa](screen:household), na seção **Importação única**, e quem a executa é o proprietário da casa.

1. Pressione **Iniciar uma importação única**.
2. Escolha como se conectar. Com **Arquivo de exportação**, envie a exportação do YNAB: o zip como o YNAB entrega, ou o `Register.csv` que está dentro dele. O `Plan.csv` não é necessário e é ignorado. Com **Chave de API**, cole um token de acesso pessoal do YNAB e escolha o plano. As duas formas estão disponíveis em todas as instalações.
3. Associe as contas e as categorias e depois passe pelas opções.
4. Pressione **Prévia**. Ela executa a importação inteira e depois a descarta, para você ver o que aconteceria.
5. Pressione **Importar** e leia o relatório.

O token é usado só naquela visita e nunca é armazenado: não fica no livro-caixa, no Histórico nem em nenhum log, e fechar a importação o esquece.

## Você associa, o app sugere

Cada conta do YNAB vai para uma conta que você já tem, para uma nova (**Criar nova…**), ou é pulada. Cada categoria do YNAB vai para uma das suas, para uma nova (**Criar nova…**), ou chega sem categoria. As sugestões vêm dos nomes, e uma conta só é sugerida se for da mesma moeda.

## O que vem junto

- *As palavras do próprio banco, pela API.* Para uma linha que o YNAB importou de um banco, a API dele ainda tem o que o banco escreveu, e isso fica guardado ao lado do nome de favorecido do YNAB; o favorecido é o nome que você usava no YNAB. O relatório então aponta para **Regras de nome de favorecidos**, que podem sugerir regras a partir desse texto antes do seu primeiro extrato chegar, e diz quantos grupos de textos do banco agora parecem um favorecido cada. O `Register.csv` tem só os nomes limpos, então uma importação por arquivo não guarda texto do banco, e o relatório dela diz que regras podem ser sugeridas depois da primeira importação de extrato.
- *Os estados de compensação são zerados.* As marcas de conciliada e compensada do YNAB não vêm junto: toda linha chega não compensada, para ser conciliada aqui com o banco. A importação pede que você confirme isso antes de gravar.
- *As transferências são vinculadas.* Quando os dois lados de uma transferência do YNAB são importados, viram uma transferência vinculada aqui. Quando um lado é pulado, está fora do período ou falta, o outro chega como uma linha comum e o relatório diz por quê.

## Duplicatas

Uma linha que parece uma que já está numa conta que você associou (mesmo valor, com datas até 3 dias de diferença) aparece antes de qualquer coisa ser gravada, e é pulada a menos que você diga para importá-la, uma a uma ou todas de uma vez. Uma linha que uma importação única anterior já trouxe é sempre pulada, então executar de novo só adiciona o que é novo. Uma linha de arquivo é reconhecida pelo que diz (conta, data, valores, favorecido, categoria e memorando), não pela posição, então uma exportação nova com linhas adicionadas acima ainda pula tudo o que já foi importado.

## Extratos depois

Um extrato importado depois para os mesmos meses associa cada linha à transação que a Importação única trouxe (mesmo valor, com data até 4 dias de diferença) e a marca como vista pelo banco em vez de adicioná-la de novo. Para uma linha que o próprio YNAB tinha importado do banco, a correspondência é exata: é aquela linha do banco, mesmo que você tenha mudado a data dela no YNAB. Uma linha que você conciliou aqui fica como está. Executar a Importação única de novo continua pulando uma linha que um extrato já associou. Veja [Como a importação lê um extrato](help:import-how-it-works).

## Parcial, não tudo ou nada

Uma linha que não pode ser lida (uma data ruim, um valor que não cabe na moeda) fica de fora e o resto entra mesmo assim. O relatório depois da gravação conta cada linha: importadas, vinculadas, puladas e por quê, com falha e por quê. O que não entrou é baixado como um arquivo `.txt`.

## Um desfazer reverte tudo

A importação inteira é uma única entrada no [Histórico](screen:history), *One-time Import · YNAB (CSV)* ou *One-time Import · YNAB (API)*. Desfazê-la remove cada transação, vínculo de transferência, conta, categoria e favorecido que ela criou e, como a própria importação, cabe ao proprietário fazer. Cada linha que ela trouxe diz isso em **De onde veio isto?** no registro, com o arquivo ou plano e a data.

## Outro app?

Mais fluxos podem ser adicionados à Importação única. Sugira um no [GitHub](https://github.com/MarioLonghi-com/household-spend-tracker/issues/new).
