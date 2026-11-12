---
id: exchange-rates
title: Mantener al día los tipos de cambio
screens: [exchange-rates, report-net-worth, report-income-expense]
also:
  - obtener tipos de cambio
  - introducir un tipo a mano
  - el informe dice que los tipos están desactualizados
  - divisa sin convertir
  - qué tipo usa un informe
  - importar tipos desde un archivo csv
---

El libro nunca convierte: cada importe se queda en la divisa de su cuenta. Los tipos de cambio solo sirven para los informes, que pueden mostrar las cifras en la divisa base del hogar. Cada cifra convertida lleva ≈, y la línea *Tipos a fecha de* del informe indica la antigüedad de los tipos. Cualquier miembro puede ocuparse de ellos en la página [Tipos de cambio](screen:exchange-rates).

## Obtener los tipos

Nada se obtiene por sí solo.

1. Abre **Tipos de cambio**. La línea bajo **Obtener ahora** dice qué divisas se obtendrían, de dónde y desde qué fecha.
2. Pulsa **Obtener ahora**. Solo obtiene las fechas que aún no están guardadas, y sigue aunque salgas de la página.
3. Para volver a obtener unas fechas, pulsa **Volver a obtener un periodo…**, elige **Desde** y **A**, pulsa **Volver a obtener** y confirma. Los tipos obtenidos para esas fechas sustituyen a los guardados; los introducidos a mano no se tocan.

**Historial de obtenciones** muestra cada obtención, quién la inició y qué añadió.

## Introducir un tipo a mano

Para una divisa que ninguna fuente cubre, o para un tipo que quieras fijar tú:

1. En **Tipos introducidos a mano**, escribe el código de la **Divisa**. También puedes pulsar **Introducir un tipo** en su fila de **Por divisa**.
2. En **Para**, elige **Un día** o **Un mes**. Un tipo mensual cubre todos los días de su mes.
3. Elige la fecha o el mes y escribe el tipo como unidades de esa divisa por 1 de la divisa base, tal como lo publica el BCE.
4. Pulsa **Añadir el tipo**, o **Sustituir el tipo** si ya hay uno guardado para esa fecha.

Para muchos tipos a la vez, elige en **Archivo de tipos** un CSV con las columnas `currency,date,rate`, revisa la vista previa y pulsa **Guardar estos tipos**. Es todo o nada: una línea con un problema deja fuera el archivo entero. Un tipo introducido a mano se puede eliminar en **Guardados**, y el [Historial](screen:history) puede recuperarlo.

## Ajustes

- *Fuentes de tipos, por orden.* Se usa la primera fuente que tenga un tipo dentro del límite de antigüedad. De entrada van primero los tipos introducidos a mano y después el BCE. Usa **Subir**, **Bajar** y **Quitar** para cambiar el orden, o añade *tus propias transferencias*: los tipos que resultaron de tus transferencias entre divisas, comisiones incluidas, los días en que cambiaste dinero.
- **Considerar un tipo desactualizado tras (días)**: siete de entrada. Un tipo desactualizado se sigue usando, y el informe lo indica.
- **Tipo para las cifras mensuales**: **Media mensual**, **Tipo de fin de mes** o **Tipo diario por fila**. Es el que usa [Ingresos y gastos](screen:report-income-expense) para convertir cuando muestra todas las divisas en la divisa base. El [Patrimonio neto](screen:report-net-worth) siempre convierte cada saldo al tipo de su propia fecha.

Pulsa **Guardar ajustes**. Cambiar las fuentes o el tipo mensual cambia cifras convertidas del pasado, así que antes te lo pregunta; pulsa **Cambiarlo** para seguir. El cambio queda en el Historial.

## Ver lo que hay guardado

**Por divisa** muestra la fuente de cada divisa, su primer y último tipo, la cobertura y los huecos. **Meses** muestra una franja con los meses que tienen tipos, y **Ver tipos** lista los tipos guardados por día o por mes, con la cifra que usaría un informe. Una divisa en **Sin cobertura** no tiene fuente; introduce sus tipos a mano. La divisa base se fija en la [página del hogar](screen:household).
