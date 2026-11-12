---
id: import-transfers
title: Cómo se emparejan las transferencias importadas
screens: [import, transfers]
also:
  - por qué no se vinculó mi transferencia
  - la importación vinculó la transferencia equivocada
  - transferencia esperando el otro extracto
  - el dinero al ahorro cuenta como gasto
  - transferencia sugerida al importar
---

El dinero que pasa entre dos de tus cuentas aparece en los extractos de los dos bancos, a menudo importados con días de diferencia. Vinculados como una transferencia, los dos movimientos quedan fuera de Ingresos y gastos. Así decide una importación. Para vincular a mano, consulta [Mover dinero entre tus propias cuentas](help:transfers).

## Una pareja candidata

Dos movimientos en cuentas distintas, en la misma moneda, con importes exactamente opuestos y fechas separadas 5 días como mucho.

Un movimiento con categoría no es una transferencia. Nunca se empareja, nunca se vincula al confirmar y nunca queda esperando, y la categorización ocurre antes del emparejamiento, así que un movimiento al que la regla de su beneficiario o el historial dan una categoría se deja en paz. Quita la categoría y se vuelve a tener en cuenta.

## Vinculadas al confirmar sin preguntar

Una pareja se vincula al confirmar la importación cuando se cumplen las dos cosas:

- un movimiento nombra la otra cuenta (su número, IBAN, tarjeta o un nombre que el banco usa para ella; consulta [identificadores de cuenta](help:account-identifiers)), o esas dos cuentas ya han tenido transferencias vinculadas antes, por un movimiento que nombraba el otro lado o por ti. Un vínculo hecho solo por ese historial no cuenta;
- *y* ningún otro movimiento podría ser su pareja, o este es estrictamente el más cercano en fecha, o, cuando la fecha no distingue entre dos, sus dos descripciones comparten estrictamente más palabras que las de cualquier rival, sin contar palabras como *TO*, *FROM* o tus números de cuenta.

Un vínculo que crea historial entre dos cuentas hace fuertes también sus otras parejas, así que esas se vinculan en la misma confirmación, y con *Link all* en la pantalla [Transferencias](screen:transfers), que vincula hasta que no queda nada nuevo fuerte.

## Sugeridas en su lugar

- Cuando solo coinciden los importes y las fechas, o cuando un movimiento podría emparejarse con más de uno, la pareja se sugiere. La vista previa lo dice, y la pantalla Transferencias es donde lo confirmas.
- Nunca se vinculan sin preguntar, por bien que coincidan: el dinero que sale de una tarjeta de crédito (una compra, y su devolución o cashback no es una transferencia), y un movimiento cuyo beneficiario tiene una regla de categoría o cuyas palabras nombran a alguien que ya te ha pagado antes, como el reembolso de gastos de un empleador. Estos se sugieren.

## Parejas que has rechazado

Una pareja de la que has dicho que no es una transferencia, al desvincularla o con **No es una transferencia** en la pantalla Transferencias, no se vuelve a emparejar nunca. Deshacerlo en el [Historial](screen:history), o vincular tú la pareja, la recupera.

## Esperando el otro extracto

Cuando un movimiento nombra una de tus cuentas o a un miembro del hogar y su otro lado aún no se ha importado, queda esperando. Se vincula cuando llega ese extracto.

## Dos monedas

Dos monedas nunca se emparejan automáticamente, porque los importes difieren en un tipo de cambio que nadie imprimió. Selecciona los dos movimientos en el [registro](screen:register) y elige **Vincular como transferencia**; el tipo se calcula y se guarda.
