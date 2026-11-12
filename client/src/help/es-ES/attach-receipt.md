---
id: attach-receipt
title: Adjuntar un ticket a un movimiento
screens: [receipts, register]
also:
  - añadir la foto de un ticket
  - subir una factura en PDF
  - emparejar un ticket con un pago
  - dónde se guardan los tickets
  - guardar la foto original
  - tickets en la bandeja de entrada
---

Un ticket es una foto, un escaneo o un PDF que se guarda en un movimiento como justificante. Puedes adjuntarlo directamente al movimiento o dejarlo antes en la bandeja de entrada de tickets y emparejarlo cuando llegue el extracto. Las dos formas son normales.

## En un movimiento

1. Abre [Movimientos](screen:register) y haz clic en la fila para abrir su panel.
2. En la sección **Ticket**, elige **Adjuntar tickets** y selecciona uno o varios archivos. También puedes arrastrar archivos hasta el botón o pegar una captura de pantalla con el panel abierto.
3. Espera a que termine cada archivo. El servidor lo comprime, y con una foto del móvil puede tardar unos segundos.

Cuando el movimiento ya tiene un ticket, el panel ofrece **Añadir más** para otro, **Sustituir** para cambiar el que se ve y **Desvincular** para quitarlo. Ni sustituir ni desvincular borran nada: el ticket vuelve a la bandeja de entrada. En el registro, un clip marca las filas que tienen ticket.

## Desde la pantalla de tickets

[Tickets](screen:receipts) es la bandeja de entrada: todo lo que aún no está en un movimiento. Las fotos enviadas [desde el móvil](help:snap-receipt) llegan aquí.

1. Elige **Añadir tickets** para subir archivos sin movimiento, o haz clic en uno que ya esté esperando.
2. En su panel, elige **Adjuntar a un movimiento**.
3. La lista se abre en los diez días anteriores y posteriores a cuando se hizo la foto, o a cuando se subió si el archivo no lleva fecha. Busca la fila y elige **Adjuntar**.

**Bandeja de entrada**, **Emparejada** y **Todos** cambian entre lo que espera, lo que ya está adjunto y todo. **Notas del ticket** es una línea libre para decir para qué fue; en la vista **Lista** puedes escribirla directamente en la tabla.

## Dónde se guardan los tickets

Los tickets se guardan en la base de datos del propio hogar, en este servidor, y en ningún otro sitio. Qué se guarda se elige en la página del hogar (haz clic en el nombre del hogar en el menú), en **Tickets**:

- *Solo copias legibles*, la opción recomendada: una copia para la pantalla de 2000 píxeles como máximo en su lado largo y una miniatura, las dos comprimidas en AVIF. Unos 33 KB por ticket.
- **Guardar también el archivo original**: además, el archivo exacto que salió de la cámara o del escáner. Unos 2 MB por ticket, unas 70 veces el espacio.

Un PDF se guarda siempre tal como se subió, sea cual sea la opción, con una imagen de su primera página para mostrarla en pantalla. La opción vale solo para tickets nuevos: nada de lo ya guardado se convierte ni se borra. Si quien administra el servidor ha activado los originales para todos los hogares, la página lo indica y la opción no se puede cambiar.

**Descargar** te da el original cuando se guardó y, si no, la copia de pantalla. **Más información** muestra cuándo y dónde se hizo la foto, según la propia foto.

## Conviene saber

- Formatos admitidos: JPEG, PNG, HEIC, WebP, AVIF, GIF, TIFF, BMP y PDF, hasta 25 MB cada uno.
- El mismo archivo dos veces en un movimiento, o dos veces en la bandeja de entrada, se rechaza. El mismo archivo en dos movimientos distintos se permite, para una factura que cubre dos pagos, y el panel indica que también está en el otro.
- Cuando [divides un movimiento](help:split-transaction), sus tickets pasan a todas las partes.
- Eliminar un ticket en la pantalla de tickets se puede deshacer desde el [Historial](screen:history).
