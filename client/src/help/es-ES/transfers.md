---
id: transfers
title: Mover dinero entre tus propias cuentas
screens: [transfers, register]
also:
  - registrar una transferencia
  - vincular dos movimientos como transferencia
  - transferencia entre divisas
  - el dinero que paso al ahorro sale como gasto
  - desvincular una transferencia
  - no es una transferencia
---

Una transferencia es dinero que pasa de una cuenta del hogar a otra. Es un solo acto formado por dos movimientos, uno en cada cuenta, y no tiene categoría: no es un gasto ni un ingreso, así que queda fuera de [Ingresos y gastos](screen:report-income-expense).

## Registrar una transferencia a mano

1. Abre [Movimientos](screen:register), pulsa **Añadir movimiento** y elige **Transferencia**.
2. En **Desde**, elige la cuenta de la que sale el dinero; en **A**, la cuenta a la que llega.
3. Indica la **Fecha**, escribe el importe que sale sin signo menos y, si quieres, añade un **Concepto**.
4. Pulsa **Transferir**.

Los dos movimientos se guardan juntos, cada uno con *Transfer :* y el nombre de la otra cuenta como beneficiario. Si cambias el importe de un lado, cambia también el otro, y deshacer el acto en el [Historial](screen:history) quita los dos. El formulario necesita dos cuentas abiertas.

## Entre dos divisas

Cuando las cuentas están en divisas distintas, el formulario pide también el importe que llega. Escribe la cifra que tu banco abonó de verdad: la aplicación nunca se inventa un tipo de cambio. Te muestra el tipo que resulta de los dos importes y lo guarda con la pareja.

## Vincular dos movimientos que ya tienes

Cuando el extracto de cada banco ha traído su propio lado, convierte los dos movimientos en una sola transferencia:

1. En [Movimientos](screen:register), marca la salida de una cuenta y la entrada en la otra.
2. Pulsa **Vincular como transferencia** en la barra que aparece.

Tienen que estar en cuentas distintas e ir en sentidos opuestos. En la misma divisa, los importes deben coincidir exactamente; entre divisas pueden ser distintos, y el tipo entre ellos se calcula y se guarda. Cada movimiento conserva su fecha y el texto del banco, y pierde la categoría. No se puede vincular un movimiento que forme parte de una división ni uno marcado como gasto de trabajo. Para deshacerlo, marca un lado y pulsa **Desvincular transferencia**.

## La pantalla de transferencias

Las importaciones vinculan sobre la marcha las parejas de las que están seguras (consulta [cómo emparejan transferencias las importaciones](help:import-transfers)). La pantalla [Transferencias](screen:transfers) reúne el resto, solo en la misma divisa:

- **Seguras**: un movimiento nombra la otra cuenta, o esas dos cuentas ya han tenido transferencias vinculadas. *Vincular todas* vincula todas las parejas de la sección.
- **Merece un vistazo**: los importes coinciden y las fechas están cerca, pero nada nombra la otra cuenta. Pulsa **Vincular** si es una transferencia, o **No es una transferencia** para que la pareja no vuelva a aparecer. Marca varias y pulsa **Vincular seleccionados** para vincularlas de una vez.
- **Esperando el otro extracto**: movimientos que nombran una de tus cuentas y cuyo otro lado aún no está en el libro. Se vinculan cuando se importa ese extracto. Si alguno no es una transferencia, dale una categoría con **No es una transferencia: categorizar como…**.
- **Vinculadas solo por el historial**: vínculos que no eligió ninguna persona. Pulsa **Desvincular** si uno está mal, o **Conservar** para confirmarlo.
