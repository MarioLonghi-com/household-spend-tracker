---
id: backups
title: Fazer backup do livro-caixa e restaurá-lo
screens: [application]
also:
  - fazer um backup
  - baixar um backup
  - salvar o backup no google drive ou no dropbox
  - restaurar um backup
  - levar o livro-caixa para outro computador
  - o que é o secret.key
---

Um backup é uma cópia completa do livro-caixa: todas as casas desta instalação, com os comprovantes. Os backups são feitos na tela [Gerenciamento do aplicativo](screen:application), que só um proprietário pode abrir.

## Fazer um backup

1. Abra **Gerenciamento do aplicativo** no menu **Administração**.
2. Em **Banco de dados**, na parte **Fazer backup do banco de dados**, clique em **Fazer backup agora**.

A cópia é verificada e gravada na pasta de backups enquanto o aplicativo continua funcionando, e aparece na lista abaixo do botão. **Último backup** mostra quando o mais recente foi feito. Nada aqui exclui um backup sozinho; **Excluir** remove um quando você confirma, e isso não pode ser desfeito.

## Levar uma cópia para fora desta máquina

Um backup que fica ao lado do livro-caixa não ajuda se a máquina se perder. Na linha de um backup:

- **Baixar** entrega um arquivo zip.
- **Salvar no Drive/Dropbox…** mostra as formas que este navegador tem de colocar o zip numa pasta na nuvem: a tela de compartilhamento do aparelho, **Escolher uma pasta…** para uma pasta sincronizada, ou um download para você enviar depois. A página nunca fala com o Google nem com o Dropbox.

O zip contém o livro-caixa (`spendtracker.sqlite3`), um `manifest.json` que permite à restauração conferi-lo e um `README.txt` que explica a quem abrir o arquivo no futuro o que ele é e como restaurá-lo.

## A chave secreta

Os códigos do aplicativo autenticador são protegidos por uma chave, `secret.key`, que o backup só leva se você pedir. Marque a caixa acima da lista para incluí-la; a partir daí, cada download pede sua senha e um código. Guarde esse zip num lugar que só você consiga abrir: com ele e a senha de um membro, qualquer pessoa entra como esse membro.

Sem a chave, o zip é restaurado nesta mesma instalação sem problemas. Em qualquer outro lugar, todos os autenticadores são recusados, a menos que a chave seja fornecida à parte; os códigos de recuperação continuam funcionando.

## Restaurar um backup

Num servidor não há botão de restaurar. Quem cuida da instalação para o aplicativo e executa `make restore FROM=` com o zip ou a pasta do backup, acrescentando `KEY=` com o caminho do `secret.key` quando o zip não o tem e vai para outra instalação. O livro-caixa atual é posto de lado, não sobrescrito. Uma chave de acesso só funciona no nome de host em que foi criada; por isso, depois de restaurar com outro nome, os membros entram com senha e código e registram as chaves de acesso de novo.

Cada atualização também faz seu próprio backup, listado em **Backups feitos por atualizações**; os cinco mais recentes são mantidos para que a atualização possa ser desfeita em **Atualizações**.

Nunca faça backup copiando o arquivo do banco de dados: as mudanças recentes ficam num segundo arquivo ao lado dele, e a cópia de um arquivo só pode sair vazia.

## No aplicativo para desktop

O aplicativo para desktop não tem autenticadores, então não há chave para incluir. O menu **Arquivo** tem **Fazer backup agora**, **Restaurar de um backup…**, **Escolher a pasta de backups…** e **Mostrar a pasta de backups**. Ele também faz backup na primeira abertura de cada dia e ao sair depois de alguma mudança. A restauração faz antes um backup do livro-caixa atual e depois pede que você entre de novo; ela recusa um backup feito por uma versão mais nova. Restaurar também é a forma de levar o livro-caixa para outro computador.
