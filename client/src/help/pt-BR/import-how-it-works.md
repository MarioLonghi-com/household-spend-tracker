---
id: import-how-it-works
title: Como a importação lê um extrato
screens: [import]
also:
  - quais formatos de arquivo posso importar
  - por que uma linha não foi importada
  - o que significa já importada
  - transações pendentes não aparecem na importação
  - o saldo corrente não fecha
  - como a categoria de uma linha importada é escolhida
---

O que acontece com um extrato desde o momento em que você escolhe o arquivo até as linhas estarem no registro, e todos os caminhos que uma linha pode tomar. Para fazer a importação, veja [Importar um extrato](help:import-statement).

## Como o arquivo entra

- Envie um arquivo, ou cole as linhas se o seu banco só deixa copiá-las de uma página web. As duas formas seguem o mesmo caminho.
- Formatos lidos: CSV e outros textos delimitados (vírgula, ponto e vírgula, tabulação ou barra vertical); OFX, tanto no formato SGML antigo (1.x) quanto em XML (2.x); Excel antigo `.xls`; e extratos em PDF cujo layout o app conhece.
- Não lidos: `.xlsx` (salve como CSV ou `.xls`), camt.053 e MT940. Esses são conhecidos, mas nenhum arquivo real de exemplo passou por aqui, e um formato montado só a partir da especificação nunca foi testado com um arquivo real.
- Um arquivo muito maior do que um extrato é recusado, com uma frase dizendo qual limite ele ultrapassou: um PDF com mais de 50 páginas, ou que ao ser aberto tem muito mais texto e desenho do que um extrato; um `.xls` cuja primeira planilha passa de um milhão de células; uma única célula com mais de 128 KB. Só a primeira planilha de um `.xls` é lida.
- Uma conta por importação, e você a escolhe. Se o arquivo disser de qual conta é (o número da conta que um arquivo OFX informa, um identificador no nome do arquivo ou um IBAN nas primeiras linhas), a conta é escolhida para você e a tela diz por quê. Se o arquivo indicar uma conta diferente da que você escolheu, você é avisado. Se não indicar nenhuma, e o nome do arquivo trouxer um código que pode ser o marcador da conta, a tela oferece guardar esse código como marcador da conta que você escolher, só se você disser **Adicionar**.
- O mesmo arquivo duas vezes é recusado enquanto o primeiro estiver aguardando ou importado, com um jeito de abrir o que está aguardando. Depois que uma importação é desfeita, o arquivo dela pode entrar de novo.
- Um extrato de poupança colocado no cofrinho errado é recusado quando todas as linhas indicam o cofrinho da outra conta.
- Agentes com uma chave podem preparar linhas passando pelas mesmas verificações. O Histórico mostra a pessoa dona da chave e o agente que a usou.

## Como o arquivo é lido

Nada é digitado. Cada suposição abaixo aparece no topo da prévia, então uma errada fica visível antes de qualquer coisa ser gravada.

- *Onde a tabela começa.* Um bloco de título acima dela (número da conta, titular, saldo) é pulado. A codificação é descoberta, então nomes acentuados, o € e as aspas curvas de uma exportação do Windows e um arquivo "texto Unicode" em UTF-16 chegam intactos. Um arquivo UTF-16 sem a marca de ordem de bytes é recusado, com um pedido para salvá-lo de novo como UTF-8, em vez de ser adivinhado.
- *A data.* Uma coluna com nome de data em qualquer um de vários idiomas, ou abreviada como os bancos espanhóis fazem (`F. Valor`, `F. Operación`). Dia primeiro ou mês primeiro é decidido pelo arquivo inteiro, e você é avisado quando nada nele resolve a questão. Datas escritas com hora ficam só com a data. Quando um arquivo tem uma data de início e uma de *conclusão*, a de conclusão é usada: é quando o banco lançou, a data que o saldo corrente segue e a data que o outro lado de uma transferência traz.
- *O valor.* Ou uma coluna com sinal, ou colunas separadas de entrada e saída. Símbolos de moeda, separadores de milhar e parênteses para negativos são lidos, assim como um sinal de menos escrito no fim (`12,50-`) ou como menos tipográfico ou travessão, e um débito ou crédito escrito depois do número (`12.50 DR` é saída, `12.50 CR` é entrada). Um valor com dois sinais (`-12.50 DR`), ou que nem é um número, é recusado com o motivo em vez de ser lido como zero. Se a vírgula ou o ponto é o separador decimal é descoberto pelos valores e pelo saldo. Quando todos podem ser lidos dos dois jeitos (`1.500` é mil e quinhentos na Espanha e um e meio na Grã-Bretanha), o país da conta decide, ou o ponto quando ela não tem país, e você é avisado de que isso foi suposto. Se todos os valores do arquivo forem positivos e nada disser para que lado vão, você é avisado: importar assim leria um mês de gastos como receita.
- *Nunca o valor:* um saldo, uma taxa (AER, NIR) ou uma coluna de data, mesmo quando o nome parece de valor.
- Uma coluna de tarifa vira uma linha própria em *Bank fees*, ao lado do pagamento em que foi cobrada. O pagamento em si não muda.
- Um saldo corrente é lido e conferido (veja a próxima seção). Ele nunca é tomado como valor.
- Tudo o que o banco escreveu fica guardado com cada linha, com os nomes de coluna do próprio banco, para que uma transação possa ser explicada depois que o arquivo não existir mais.

## Verificações no arquivo como um todo

- *Um arquivo com várias contas.* Alguns bancos colocam mais de uma conta num só arquivo e as distinguem por uma coluna Product. O extrato de conta do Revolut traz a conta corrente (`Current`) e cada cofrinho de poupança (`Deposit`). Cada conta recebe um produto: uma conta corrente recebe `Current`, a menos que você defina o **Produto do extrato** dela na tela [Contas](screen:accounts). As outras linhas aparecem como **Não é desta conta**. Se uma conta não disse qual produto recebe e não há palpite seguro, o arquivo é recusado com instruções.
- *Um arquivo em outra moeda.* Uma coluna Currency (ou Moneda, Divisa, CCY) e a moeda do próprio arquivo OFX são conferidas com a da conta. Todo valor é registrado na moeda da conta, então uma linha em outra moeda nunca é importada. Numa tabela que também tem linhas na moeda da conta, ela aparece como **Não é desta conta**; caso contrário, e sempre num arquivo OFX (que é de uma só conta), como **Não foi possível ler**, com a cotação do banco se ele informou uma. Nada é convertido. De qualquer forma o motivo cita as duas moedas. Um arquivo em que todas as linhas dizem estar em outra moeda é recusado. Uma linha de agente que informa a moeda passa pela mesma verificação, e uma em outra moeda fica como **Não foi possível ler**. Uma coluna dizendo quanto uma compra custou antes da conversão (Original, Moneda origen, Local, Transaction currency) não conta: esse valor já está no dinheiro da conta. Se duas colunas puderem ser a moeda dos valores e nenhuma tiver nome de conta, fatura ou liquidação, nenhuma é usada e a prévia avisa.
- *O saldo corrente fecha?* O saldo de cada linha deve ser o anterior mais o que a linha movimentou. Se não fechar, a prévia diz onde: falta uma linha, ou há linhas de outra conta misturadas. Se um número do arquivo for grande demais para ser registrado como dinheiro, aquela linha é rejeitada e a prévia diz que o saldo não foi conferido.
- *Continua de onde o livro-caixa parou?* O saldo antes da primeira linha do arquivo deve bater com o que o registro tem para o dia anterior. Se não bater, a prévia mostra a diferença: provavelmente falta um extrato entre a sua última importação e esta.
- Uma regra de favorecido que pega a maior parte de um arquivo, ou uma curta demais para ser segura, é apontada nas linhas que pegou.

## O que acontece com cada linha

A coluna **O que acontece** da prévia dá a cada linha um destes:

- **Nova** (`created`): vira uma nova transação quando você confirma. Se a linha for um lado de uma transferência de que o importador tem certeza, ela também é vinculada ao outro lado.
- **Já existe** (`matched_existing`): você mesmo digitou esta, ou uma Importação única a trouxe: mesmo valor, com data até 4 dias de diferença, ou, para uma linha que o YNAB tinha importado do banco, exatamente aquela linha do banco, qualquer que seja a data dela agora. A sua linha é mantida e marcada como vista pelo banco, em vez de uma segunda ser adicionada. Você pode recusar a correspondência na prévia com **Não é a mesma coisa — adicionar à parte**.
- **Já importada** (`duplicate_skipped`): esta linha já está na conta, vinda deste arquivo ou de um extrato anterior que se sobrepõe a ele. Nada é adicionado.
- **Precisa de revisão** (`needs_review`): a linha com que ela correspondia mudou entre a prévia e a confirmação, ou já estava associada a outra linha de extrato. Ela foi deixada como estava e não foi importada.
- **Não foi possível ler** (`rejected`): a linha não tem data ou valor legível, o valor, a tarifa ou o saldo é grande demais para ser registrado como dinheiro, ela diz estar numa moeda diferente da conta, ou o banco diz que ela não aconteceu ou ainda não foi liquidada (veja abaixo). Uma linha ruim custa uma linha, nunca o arquivo.
- **Não é desta conta** (`skipped`): lida sem problema e deixada de fora de propósito. Pertence a outra conta num arquivo que traz várias (outro produto ou outra moeda), ou não movimenta dinheiro nenhum.

### Linhas que o banco diz que não aconteceram, ou ainda não aconteceram

Uma coluna de estado dizendo `REVERTED`, `DECLINED`, `FAILED`, `CANCELLED` (ou `CANCELED`), `REJECTED` ou `ANULADO` significa que nenhum dinheiro se moveu, e a linha é recusada com a própria palavra do banco como motivo. `PENDING`, `PENDIENTE` ou `HOLD` significa que ainda não foi liquidada: é recusada desta vez e entra pelo próximo extrato depois de liquidada, para que uma gorjeta ou uma cotação aplicada na liquidação não crie uma segunda cópia.

### Linhas que não movimentam dinheiro

Uma linha de `0.00`, como os juros de um dia que arredondaram para nada ou uma "Closing transaction", aparece e não é importada.

### Como se decide que já foi importada

Pelo identificador de transação do próprio banco quando o arquivo tem um (`FITID` do OFX). Senão, pelo valor, pela data e pelo saldo corrente depois da linha, que é o mesmo seja qual for o período que o seu download cobriu. Sem saldo, é o valor, a data e a posição da linha entre as idênticas daquele dia. Extratos que se sobrepõem, portanto, importam só o que é novo. Linhas importadas por uma versão mais antiga do app continuam sendo reconhecidas.

Uma linha só conta como já importada enquanto a transação dela estiver no registro. Exclua a transação e a linha volta a ser nova: o próximo extrato que a trouxer, ou o mesmo arquivo enviado de novo com **Importar mesmo assim**, a traz de volta, reconhecida pela mesma identidade. O extrato é o registro. Desfazer no [Histórico](screen:history) é o outro caminho de volta, e também restaura o que você tinha mudado na transação.

### Favorecido e categoria

As suas regras de nome de favorecidos decidem o favorecido, e a categorização do favorecido decide a categoria (veja [regras de favorecidos](help:payee-rules)). Nomes e regras são comparados ignorando maiúsculas, acentos, o tipo de traço e espaços invisíveis, então *Café Sol* e *CAFE SOL* são um favorecido só; uma regra de expressão regular é testada contra as palavras do próprio banco e também contra essa forma simplificada. Uma categoria que você escolhe na prévia vale mais que as duas. Quando nenhuma diz nada, o texto fixo do banco decide para alguns tipos de linha: juros vão para o favorecido *Interest* e *Interest income*, a conta de investimentos e o robo portfolio do Revolut para *Investments*, e tarifas para *Bank fees*. Uma categoria de que o importador precisa e que você ainda não tem é criada quando você confirma, e removida de novo se a importação for desfeita. O que o banco escreveu nunca é sobrescrito.

## Preparada, depois confirmada, depois reversível

- Nada chega ao registro até você confirmar. Um arquivo lido fica preparado e aguarda na fila da tela Importar, em **Aguardando revisão**, onde pode ser aberto de novo ou descartado.
- Cada vez que uma importação preparada é aberta, ela é conferida de novo com o registro como está agora. Uma duplicata cujo original foi desfeito nesse meio-tempo volta a ser nova, e a prévia diz quantas linhas mudaram. A confirmação confere mais uma vez, então uma aba esquecida aberta não consegue importar uma linha duas vezes.
- Uma importação é um ato no Histórico, e desfazê-la remove cada linha que ela adicionou, cada vínculo de transferência que fez e cada categoria que criou. Linhas conciliadas nunca são associadas como **Já existe**.

Transferências entre as suas próprias contas têm regras próprias: veja [como as transferências importadas são casadas](help:import-transfers). Como um arquivo encontra a conta dele está em [identificadores de conta](help:account-identifiers).
