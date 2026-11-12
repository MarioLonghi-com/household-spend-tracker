---
id: payee-rules
title: Nombrar y categorizar beneficiarios automáticamente
screens: [rules, payee-categorisation, payees]
also:
  - reglas de beneficiarios
  - cambiar el nombre que pone el banco a una tienda
  - la misma tienda sale con varios nombres
  - poner siempre un beneficiario en la misma categoría
  - fusionar dos beneficiarios
  - que no adivine la categoría de un beneficiario
---

Cada línea importada pasa por dos pasos. Una regla de **Reglas de nombre de beneficiarios** convierte las palabras del banco en un beneficiario, y después la configuración de ese beneficiario en **Categorización de beneficiarios** le da su categoría al movimiento. **Fusionar beneficiarios** junta dos grafías del mismo beneficiario. Las tres pantallas están en **Beneficiario**, en el menú de tu hogar.

## Crear una regla de nombre

1. Abre [Reglas de nombre de beneficiarios](screen:rules) y pulsa **Añadir una regla**.
2. En **Y después**, elige **llamarlo un solo beneficiario** para una tienda, o **quitar este trozo y ver qué queda** para un medio de pago cuyas palabras van delante de muchas tiendas distintas.
3. En **Cuando la línea del extracto**, elige **contiene**, **empieza por**, **es exactamente** o **coincide con el patrón** (una expresión regular) y escribe las palabras del banco en **Este texto**.
4. Para una tienda, escribe su nombre en **Llamar al beneficiario**. Para quitar un trozo, normalmente deja vacío **Poner esto en su lugar (déjalo vacío para quitarlo)**.
5. Pulsa **¿Con qué se emparejaría?** para ver cuántas filas que ya están en el libro reclamaría, con ejemplos.
6. Pulsa **Crear**.

Una regla compara *las propias palabras del banco*, no el beneficiario que muestra el registro. Abre un movimiento y pulsa **¿De dónde viene esto?** para ver exactamente lo que llegó. Da igual que sea en mayúsculas o minúsculas. Las reglas que quitan texto se aplican primero, cada una sobre lo que dejó la anterior; después gana la primera regla de nombre que coincida. **Orden (la menor se aplica primero)** fija el orden. Desmarca la casilla **Activa** de una regla para pausarla; **Eliminar** la borra en el acto.

## Aplicar las reglas a lo que ya está en el registro

Las reglas se aplican al leer un extracto, así que una regla nueva no cambia nada de lo que ya está en el registro. Justo después de **Crear** se abre el panel **Aplicar las reglas a lo que ya hay**: muestra cuántas filas cambiarían y a qué beneficiario antes de que confirmes. **Solo filas que nadie ha corregido** respeta el beneficiario que alguien puso a mano. Es una sola entrada en el [Historial](screen:history), así que un solo deshacer devuelve todas las filas.

Cuando el banco envía la misma tienda con una referencia distinta cada vez, aparece una tarjeta titulada **Parecen la misma tienda**. **Crear esta regla** empieza una regla con el texto ya puesto.

## Elegir la categoría de un beneficiario

1. Abre [Categorización de beneficiarios](screen:payee-categorisation). La columna **Categorizado como** muestra dónde se ha clasificado cada beneficiario hasta ahora.
2. Pulsa **Categorización** en la fila del beneficiario.
3. Elige **Categorizar automáticamente según el historial del beneficiario** (la categoría más frecuente de sus tres últimos movimientos), **Usar siempre mi elección** y una categoría, o *No categorizar automáticamente*, que deja en blanco los movimientos nuevos.
4. Pulsa **Guardar**.

Esto decide la categoría de los movimientos nuevos del beneficiario. Una categoría archivada no puede ser la elección fija.

## Fusionar dos grafías

1. Abre [Fusionar beneficiarios](screen:payees) y usa **Buscar un beneficiario**.
2. Pulsa **Fusionar…** en la grafía que quieres hacer desaparecer.
3. En **Hacia**, elige el beneficiario que se queda y pulsa **Fusionar**.

Todos los movimientos y reglas que nombraban al primer beneficiario pasan a nombrar al que se queda, y el primero desaparece. Puedes deshacerlo desde el Historial. Los beneficiarios que representan una transferencia no se pueden fusionar. Los que solo se diferencian en tildes, guiones o espacios invisibles aparecen en **Grafías de un nombre**; pulsa **Revisar…** y elige en **Conservar** la grafía que se queda.

Para organizar las categorías en sí, consulta [Organizar categorías y grupos](help:categories); para la importación, consulta [Importar un extracto](help:import-statement).
