---
id: import-statement
title: Importar un extracto
screens: [import, register]
also:
  - subir un extracto del banco
  - traer los movimientos del banco
  - pegar filas de la web del banco
  - importar un archivo CSV u OFX
  - terminar una importación que dejé a medias
  - deshacer una importación
---

Un extracto entra en una sola cuenta cada vez. Nada llega al registro hasta que tú lo dices, y la importación entera se puede deshacer de una vez.

## Importar un archivo

1. Abre [Importar](screen:import).
2. En **En qué cuenta**, elige la cuenta a la que pertenece el extracto. Si el archivo dice de qué cuenta es, la app la elige por ti y te dice por qué; si el archivo nombra una cuenta distinta de la que elegiste, te avisa.
3. Elige el archivo en **Archivo**: un CSV, una descarga OFX o QFX, una exportación .xls del banco o un extracto en PDF. Si tu banco solo te deja copiar las filas desde su web, pégalas en el cuadro de abajo.
4. Pulsa **Leer el archivo**. La vista previa, **Lo que haría esto**, muestra lo que se ha detectado en el archivo y cada línea con lo que le va a pasar.
5. Revisa las líneas. Haz clic en la categoría de una línea para cambiarla; escribe cualquier parte del nombre de una categoría, o vacía el cuadro para volver a la sugerencia. Si otras líneas tienen el mismo beneficiario, se te ofrece darles la misma categoría y, con **Sí, todas y poner la regla**, seguir haciéndolo en los próximos extractos. **No, solo esta** cambia solo esa línea.
6. Desmarca una línea en la columna **Añadir** para dejarla fuera.
7. Cuando una línea sale como **Ya la tienes** pero en realidad es otro movimiento, pulsa **No es lo mismo: añadirlo aparte**. **Deshacer: añadir como nuevo** lo revierte.
8. Pulsa **Importar estos**. **Cancelar** cierra la vista previa sin escribir nada.

Si el archivo no nombra ninguna cuenta pero su nombre lleva un código que podría ser la etiqueta de la cuenta, la pantalla ofrece guardarlo: pulsa **Añadir** para guardarlo o **Ignorar**. Consulta [identificadores de cuenta](help:account-identifiers).

## Importaciones a medias

Un archivo que leíste pero no importaste espera en **Pendientes de revisar**. Pulsa **Abrir** para seguir donde lo dejaste, o **Descartar** para tirarlo; nada de él ha llegado al registro. Leer el mismo archivo otra vez se rechaza mientras haya uno esperando, con **Abrir esa importación** para ir a él, **Importarlo de todos modos** y **Descartar la preparada**.

## Deshacer una importación

Una importación es una entrada en el [Historial](screen:history). Deshacerla allí quita cada movimiento que añadió, cada vínculo de transferencia que hizo y cada categoría que creó.

## Más

- [Cómo lee la importación un extracto](help:import-how-it-works): formatos, comprobaciones y qué significa cada resultado.
- [Cómo se emparejan las transferencias importadas](help:import-transfers).
- ¿Vienes de YNAB? Trae todo tu historial de una vez con la [Importación única](help:one-time-import).
