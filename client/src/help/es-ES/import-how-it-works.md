---
id: import-how-it-works
title: Cómo lee la importación un extracto
screens: [import]
also:
  - qué formatos de archivo puedo importar
  - por qué no se importó una línea
  - qué significa ya importada
  - los movimientos pendientes no aparecen al importar
  - el saldo no cuadra
  - cómo se elige la categoría de una línea importada
---

Lo que le pasa a un extracto desde que eliges el archivo hasta que sus filas están en el registro, y todos los caminos que puede seguir una línea. Para hacerlo, consulta [Importar un extracto](help:import-statement).

## Cómo entra el archivo

- Sube un archivo, o pega las filas si tu banco solo te deja copiarlas de una página web. Las dos cosas siguen el mismo camino.
- Formatos que se leen: CSV y otros textos delimitados (coma, punto y coma, tabulador o barra vertical); OFX, tanto en la forma SGML antigua (1.x) como en XML (2.x); el Excel antiguo `.xls`; y extractos en PDF cuya disposición conoce la app.
- No se leen: `.xlsx` (guárdalo como CSV o `.xls`), camt.053 ni MT940. Se conocen, pero aquí no se ha visto ningún archivo real de muestra, y un formato construido solo a partir de su especificación nunca se ha probado con un archivo real.
- Un archivo mucho mayor que un extracto se rechaza, con una frase que dice qué límite ha superado: un PDF de más de 50 páginas, o uno que al abrirse tiene mucho más texto y dibujo de lo que cabe en un extracto; un `.xls` cuya primera hoja pasa del millón de celdas; una sola celda de más de 128 KB. Solo se lee la primera hoja de un `.xls`.
- Una cuenta por importación, y la eliges tú. Si el archivo dice para qué cuenta es (el número de cuenta que indica un archivo OFX, un identificador en el nombre del archivo o un IBAN en sus primeras líneas), la cuenta se elige por ti y la pantalla dice por qué. Si el archivo nombra una cuenta distinta de la que elegiste, se te avisa. Si no nombra ninguna, y su nombre de archivo lleva un código que podría ser la etiqueta de la cuenta, la pantalla ofrece guardar ese código como etiqueta de la cuenta que elijas, solo si dices **Añadir**.
- El mismo archivo dos veces se rechaza mientras el primero esté esperando o importado, con una forma de abrir el que espera. Cuando una importación se deshace, su archivo puede volver a entrar.
- Un extracto de ahorro echado en el apartado equivocado se rechaza cuando todas las filas nombran el apartado de la otra cuenta.
- Los agentes con una clave pueden preparar filas pasando por las mismas comprobaciones. El Historial muestra la persona dueña de la clave y el agente que la usó.

## Cómo se lee el archivo

No se escribe nada a mano. Cada suposición de abajo se muestra arriba en la vista previa, así que una equivocada se ve antes de que se escriba nada.

- *Dónde empieza la tabla.* Se salta el bloque de título que haya encima (número de cuenta, titular, saldo). Se averigua la codificación, así que los nombres con acentos, el € y las comillas tipográficas de una exportación de Windows y un archivo "texto Unicode" en UTF-16 llegan intactos. Un archivo UTF-16 sin su marca de orden de bytes se rechaza, pidiendo que se guarde de nuevo como UTF-8, en lugar de adivinarlo.
- *La fecha.* Una columna con nombre de fecha en cualquiera de varios idiomas, o abreviada como hacen los bancos españoles (`F. Valor`, `F. Operación`). Si va primero el día o el mes se decide con el archivo entero, y se te avisa cuando nada en él lo aclara. Las fechas escritas con hora se quedan solo con la fecha. Cuando un archivo tiene una fecha de inicio y otra de *completado*, se usa la de completado: es cuando el banco lo contabilizó, la fecha que sigue el saldo y la que lleva el otro lado de una transferencia.
- *El importe.* O una columna con signo, o columnas separadas de entradas y salidas. Se leen los símbolos de moneda, los separadores de miles y los paréntesis para negativos, y también un menos escrito al final (`12,50-`) o como signo menos tipográfico o raya, y un cargo o abono escrito tras la cifra (`12.50 DR` es una salida, `12.50 CR` una entrada). Un importe con dos signos (`-12.50 DR`), o uno que ni siquiera es un número, se rechaza con el motivo en vez de leerse como cero. Si el decimal es la coma o el punto se averigua con los importes y el saldo. Cuando todos se pueden leer de las dos formas (`1.500` es mil quinientos en España y uno y medio en Gran Bretaña), decide el país de la cuenta, o el punto si no tiene, y se te avisa de que se ha supuesto. Si todos los importes del archivo son positivos y nada dice en qué sentido van, se te avisa: importarlo tal cual leería un mes de gastos como ingresos.
- *Nunca el importe:* un saldo, un tipo (AER, NIR) o una columna de fecha, aunque su nombre lo parezca.
- Una columna de comisión se convierte en una fila propia en *Bank fees*, junto al pago al que se cobró. El pago en sí no cambia.
- Un saldo se lee y se comprueba (consulta la sección siguiente). Nunca se toma como importe.
- Todo lo que escribió el banco se guarda con cada línea, con los nombres de columna del propio banco, para que un movimiento se pueda explicar cuando el archivo ya no esté.

## Comprobaciones sobre el archivo entero

- *Un archivo con varias cuentas.* Algunos bancos ponen más de una cuenta en un archivo y las distinguen con una columna Product. El extracto de cuenta de Revolut lleva la cuenta corriente (`Current`) y cada apartado de ahorro (`Deposit`). Cada cuenta toma un producto: una cuenta corriente toma `Current` salvo que pongas su **Producto del extracto** en la pantalla [Cuentas](screen:accounts). Las demás filas salen como **No es de esta cuenta**. Si una cuenta no ha dicho qué producto toma y no hay una suposición segura, el archivo se rechaza con instrucciones.
- *Un archivo en otra moneda.* Una columna Currency (o Moneda, Divisa, CCY) y la moneda propia de un archivo OFX se comparan con la de la cuenta. Todo importe se registra en la moneda de la cuenta, así que una fila en otra nunca se importa. En una tabla que también tiene filas en la moneda de la cuenta sale como **No es de esta cuenta**; si no, y siempre en un archivo OFX (que es de una sola cuenta), como **No se pudo leer**, con el tipo de cambio del banco si lo dio. No se convierte nada. En ambos casos el motivo nombra las dos monedas. Un archivo en el que todas las filas dicen estar en otra moneda se rechaza. Una fila de un agente que indica su moneda pasa la misma comprobación, y una en otra moneda queda como **No se pudo leer**. Una columna que dice lo que costó una compra antes de convertirla (Original, Moneda origen, Local, Transaction currency) no cuenta: ese importe ya está en el dinero de la cuenta. Si dos columnas podrían ser la moneda de los importes y ninguna se llama como la cuenta, la factura o la liquidación, no se usa ninguna y la vista previa lo dice.
- *¿Cuadra el saldo?* El saldo de cada fila debería ser el anterior más lo que movió la fila. Si se rompe, la vista previa dice dónde: falta una fila, o hay filas de otra cuenta mezcladas. Si una cifra del archivo es demasiado grande para registrarse como dinero, esa línea se rechaza y la vista previa dice que el saldo no se ha comprobado.
- *¿Sigue donde lo dejó el libro?* El saldo antes de la primera fila del archivo debería coincidir con lo que tiene el registro para el día anterior. Si no coincide, la vista previa da la diferencia: probablemente falta un extracto entre tu última importación y esta.
- Una regla de beneficiario que se queda con la mayor parte de un archivo, o una demasiado corta para ser segura, se señala en las líneas que ha tomado.

## Qué le pasa a cada línea

La columna **Qué pasa** de la vista previa da a cada línea uno de estos:

- **Nueva** (`created`): se convierte en un movimiento nuevo cuando confirmas. Si la línea es un lado de una transferencia de la que el importador está seguro, también se vincula con el otro lado.
- **Ya la tienes** (`matched_existing`): esta la escribiste tú, o la trajo una Importación única: mismo importe, con fecha a 4 días como mucho, o, para una fila que YNAB había importado del banco, esa misma línea del banco, tenga la fecha que tenga ahora. Tu fila se conserva y se marca como vista por el banco en vez de añadir una segunda. Puedes rechazar la coincidencia en la vista previa con **No es lo mismo: añadirlo aparte**.
- **Ya importada** (`duplicate_skipped`): esta línea ya está en la cuenta, de este archivo o de un extracto anterior que se solapa con él. No se añade nada.
- **Hay que revisarla** (`needs_review`): la fila con la que coincidía cambió entre la vista previa y la confirmación, o ya estaba emparejada con otra línea de extracto. Se ha dejado como estaba y no se ha importado.
- **No se pudo leer** (`rejected`): la línea no tiene una fecha o un importe legibles, su importe, comisión o saldo es demasiado grande para registrarse como dinero, dice estar en una moneda distinta de la de la cuenta, o el banco dice que no ocurrió o que todavía no se ha liquidado (consulta abajo). Una línea mala cuesta una línea, nunca el archivo.
- **No es de esta cuenta** (`skipped`): se ha leído bien y no se importa a propósito. Pertenece a otra cuenta en un archivo que lleva varias (otro producto u otra moneda), o no mueve dinero.

### Filas que el banco dice que no ocurrieron, o que todavía no

Una columna de estado que dice `REVERTED`, `DECLINED`, `FAILED`, `CANCELLED` (o `CANCELED`), `REJECTED` o `ANULADO` significa que no se movió dinero, y la línea se rechaza con la propia palabra del banco como motivo. `PENDING`, `PENDIENTE` o `HOLD` significa que no se ha liquidado: se rechaza esta vez y entra con el siguiente extracto cuando se liquide, para que una propina o un tipo de cambio añadidos al liquidar no creen una segunda copia.

### Líneas que no mueven dinero

Una línea de `0.00`, como los intereses de un día que se redondearon a nada o una "Closing transaction", se muestra y no se importa.

### Cómo se decide que ya está importada

Por el identificador de transacción del propio banco cuando el archivo lo tiene (`FITID` de OFX). Si no, por el importe, la fecha y el saldo después de la línea, que es el mismo sea cual sea el periodo que cubra tu descarga. Sin saldo, es el importe, la fecha y la posición de la línea entre las idénticas de ese día. Por eso, los extractos que se solapan importan solo lo nuevo. Las líneas importadas por una versión anterior de la app se siguen reconociendo.

Una línea cuenta como ya importada solo mientras su movimiento esté en el registro. Borra el movimiento y la línea vuelve a ser nueva: el siguiente extracto que la lleve, o el mismo archivo enviado otra vez con **Importarlo de todos modos**, la trae de vuelta, reconocida por la misma identidad. El extracto es lo que cuenta. Deshacer en el [Historial](screen:history) es el otro camino de vuelta, y además recupera lo que hubieras cambiado en el movimiento.

### Beneficiario y categoría

Tus reglas de nombre de beneficiarios deciden el beneficiario, y la categorización del beneficiario decide la categoría (consulta [reglas de beneficiarios](help:payee-rules)). Los nombres y las reglas se comparan sin tener en cuenta mayúsculas, acentos, el tipo de guion ni los espacios que no se ven, así que *Café Sol* y *CAFE SOL* son un solo beneficiario; una regla de expresión regular se prueba contra las palabras del propio banco y también contra esa forma más sencilla. Una categoría que elijas en la vista previa manda sobre las dos. Cuando ninguna dice nada, el texto fijo del banco decide para algunos tipos de línea: los intereses van al beneficiario *Interest* y a *Interest income*, la cuenta de inversión y el robo portfolio de Revolut a *Investments*, y las comisiones a *Bank fees*. Una categoría que el importador necesita y que aún no tienes se crea al confirmar, y se vuelve a quitar si se deshace la importación. Lo que escribió el banco nunca se sobrescribe.

## Preparada, luego confirmada, luego reversible

- Nada llega al registro hasta que confirmas. Un archivo leído queda preparado y espera en la cola de la pantalla Importar, en **Pendientes de revisar**, donde se puede volver a abrir o tirar.
- Cada vez que se abre una importación preparada se vuelve a comprobar con el registro tal como está ahora. Un duplicado cuyo original se ha deshecho entretanto vuelve a ser nuevo, y la vista previa dice cuántas líneas han cambiado. Al confirmar se comprueba una vez más, así que una pestaña olvidada abierta no puede importar una línea dos veces.
- Una importación es un acto en el Historial, y deshacerla quita cada movimiento que añadió, cada vínculo de transferencia que hizo y cada categoría que creó. Los movimientos conciliados nunca se emparejan como **Ya la tienes**.

Las transferencias entre tus propias cuentas tienen sus propias reglas: consulta [cómo se emparejan las transferencias importadas](help:import-transfers). Cómo encuentra un archivo su cuenta está en [identificadores de cuenta](help:account-identifiers).
