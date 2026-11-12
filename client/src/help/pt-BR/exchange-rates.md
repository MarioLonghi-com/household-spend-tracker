---
id: exchange-rates
title: Manter as taxas de câmbio em dia
screens: [exchange-rates, report-net-worth, report-income-expense]
also:
  - buscar taxas de câmbio
  - inserir uma taxa manualmente
  - relatório diz que as taxas estão desatualizadas
  - moeda não convertida
  - qual taxa o relatório usa
  - importar taxas de um arquivo csv
---

O livro-caixa nunca converte: cada valor fica na moeda da sua conta. As taxas de câmbio servem só para os relatórios, que podem mostrar os números na moeda base da casa. Cada número convertido aparece com ≈, e a linha *Taxas até* do relatório diz a idade das taxas. Qualquer membro pode cuidar delas na página [Taxas de câmbio](screen:exchange-rates).

## Buscar as taxas

Nada é buscado sozinho.

1. Abra **Taxas de câmbio**. A linha abaixo de **Buscar agora** diz quais moedas seriam buscadas, de onde e a partir de que data.
2. Clique em **Buscar agora**. Só as datas que ainda não estão salvas são buscadas, e a busca continua mesmo se você sair da página.
3. Para buscar algumas datas de novo, clique em **Buscar um período de novo…**, escolha **De** e **Para**, clique em **Buscar de novo** e confirme. As taxas buscadas para essas datas substituem as salvas; as taxas inseridas manualmente não mudam.

**Histórico de buscas** lista cada busca, quem a iniciou e o que ela trouxe.

## Inserir uma taxa manualmente

Para uma moeda que nenhuma fonte cobre, ou para uma taxa que você mesmo quer definir:

1. Em **Taxas inseridas manualmente**, digite o código da **Moeda**. Também dá para clicar em **Inserir uma taxa** na linha dela em **Por moeda**.
2. Em **Para**, escolha **Um dia** ou **Um mês**. Uma taxa mensal vale para todos os dias do mês.
3. Escolha a data ou o mês e digite a taxa como unidades dessa moeda por 1 da moeda base, como o BCE a publica.
4. Clique em **Adicionar a taxa**, ou em **Substituir a taxa** se já houver uma salva para essa data.

Para muitas taxas de uma vez, escolha em **Arquivo de taxas** um arquivo CSV com as colunas `currency,date,rate`, confira a prévia e clique em **Salvar estas taxas**. É tudo ou nada: uma linha com problema deixa o arquivo inteiro de fora. Uma taxa inserida manualmente pode ser excluída em **Salvas**, e o [Histórico](screen:history) pode trazê-la de volta.

## Configurações

- *Fontes de taxas, em ordem.* Vale a primeira fonte que tiver uma taxa dentro do limite de atualização. Por padrão, primeiro as taxas inseridas manualmente, depois o BCE. Use **Subir**, **Descer** e **Remover** para mudar a ordem, ou acrescente *suas próprias transferências*: as taxas que suas transferências entre moedas deram, com as tarifas, nos dias em que você trocou dinheiro.
- **Considerar uma taxa desatualizada após (dias)**: sete por padrão. Uma taxa desatualizada continua sendo usada, e o relatório avisa.
- **Taxa usada nos números mensais**: **Média mensal**, **Taxa de fim de mês** ou **Taxa diária por linha**. É com ela que [Receitas e despesas](screen:report-income-expense) converte quando mostra todas as moedas na moeda base. O [Patrimônio líquido](screen:report-net-worth) sempre converte cada saldo pela taxa da própria data.

Clique em **Salvar configurações**. Mudar as fontes ou a taxa mensal altera números convertidos do passado, por isso o aplicativo pergunta antes; clique em **Mudar** para seguir. A mudança fica registrada no Histórico.

## Ver o que está salvo

**Por moeda** mostra, para cada moeda, a fonte, a primeira e a última taxa, a cobertura e as lacunas. **Meses** mostra uma faixa com os meses que têm taxas, e **Ver taxas** lista as taxas salvas por dia ou por mês, com o número que um relatório usaria. Uma moeda em **Sem cobertura** não tem fonte; insira as taxas dela manualmente. A própria moeda base é definida na [página da casa](screen:household).
