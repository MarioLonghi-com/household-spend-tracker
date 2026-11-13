---
id: one-time-import
title: Traer tu historial desde otra app (YNAB)
screens: [household, import]
also:
  - importar desde YNAB
  - cambiar desde otra app de presupuestos
  - traer todo mi historial de una vez
  - token de acceso personal de YNAB
  - Register.csv de YNAB
  - deshacer la importación de YNAB
---

Importar extractos es un archivo por cuenta, mes tras mes. La **Importación única** es para el día en que te pasas aquí desde otra app de presupuestos: trae de una vez todo el historial de esa app (movimientos, cuentas, categorías y transferencias) en lugar de extracto a extracto. Por ahora YNAB es el único flujo.

## Cómo hacerla

Está en la [página del hogar](screen:household), en la sección **Importación única**, y la hace el propietario del hogar.

1. Pulsa **Iniciar una importación única**.
2. Elige cómo conectar. Con **Archivo de exportación**, sube la exportación de YNAB: el zip tal como lo da YNAB, o el `Register.csv` que lleva dentro. El `Plan.csv` no hace falta y se ignora. Con **Clave de API**, pega un token de acceso personal de YNAB y elige el plan. Las dos formas están disponibles en todas las instalaciones.
3. Asigna las cuentas y las categorías, y luego repasa las opciones.
4. Pulsa **Vista previa**. Ejecuta la importación entera y la tira, para que veas lo que pasaría.
5. Pulsa **Importar** y lee el informe.

El token se usa solo para esa visita y nunca se guarda: no queda en el libro, en el Historial ni en ningún registro de actividad, y al cerrar la importación se olvida.

## Tú asignas, la app sugiere

Cada cuenta de YNAB va a una cuenta que ya tienes, a una nueva (**Crear nueva…**), o se omite. Cada categoría de YNAB va a una de las tuyas, a una nueva (**Crear nueva…**), o llega sin categoría. Las sugerencias salen de los nombres, y una cuenta solo se sugiere si es de la misma moneda.

## Qué se trae

- *Las palabras del propio banco, por la API.* Para una fila que YNAB importó de un banco, su API aún tiene lo que escribió el banco, y eso se guarda junto al nombre de beneficiario de YNAB; el beneficiario es el nombre que usabas en YNAB. El informe apunta entonces a **Reglas de nombre de beneficiarios**, que pueden sugerir reglas a partir de ese texto antes de que llegue tu primer extracto, y dice cuántos grupos de textos del banco parecen ahora un beneficiario cada uno. El `Register.csv` solo tiene los nombres limpios, así que una importación por archivo no guarda texto del banco, y su informe dice que las reglas se pueden sugerir después de la primera importación de un extracto.
- *Los estados de contabilizado se reinician.* Las marcas de conciliado y contabilizado de YNAB no se traen: cada movimiento llega sin contabilizar, para conciliarlo aquí con el banco. La importación te pide que lo confirmes antes de escribir.
- *Las transferencias se vinculan.* Cuando se importan los dos lados de una transferencia de YNAB, aquí se convierten en una transferencia vinculada. Cuando un lado se omite, queda fuera del periodo o falta, el otro llega como un movimiento normal y el informe dice por qué.

## Duplicados

Una fila que se parece a una que ya está en una cuenta que has asignado (mismo importe, con fechas separadas como mucho 3 días) se muestra antes de escribir nada, y se omite salvo que digas que la importe, una a una o todas a la vez. Una fila que ya trajo una importación única anterior se omite siempre, así que repetirla solo añade lo nuevo. Una fila de un archivo se reconoce por lo que dice (cuenta, fecha, importes, beneficiario, categoría y nota), no por su posición, así que una exportación nueva con filas añadidas por encima sigue omitiendo todo lo importado antes.

## Extractos después

Un extracto importado más tarde para los mismos meses empareja cada línea con el movimiento que trajo la Importación única (mismo importe, con fecha a 4 días como mucho) y lo marca como visto por el banco en vez de añadirlo otra vez. Para una fila que YNAB había importado del banco, la coincidencia es exacta: es esa línea del banco, aunque movieras su fecha en YNAB. Un movimiento que has conciliado aquí se deja en paz. Repetir la Importación única sigue omitiendo una fila que un extracto ya ha emparejado. Consulta [Cómo lee la importación un extracto](help:import-how-it-works).

## Parcial, no todo o nada

Una fila que no se puede leer (una fecha mala, un importe que no encaja con la moneda) se deja fuera y el resto entra igualmente. El informe después de confirmar cuenta cada fila: importadas, vinculadas, omitidas y por qué, fallidas y por qué. Lo que no entró se descarga como un archivo `.txt`.

## Un deshacer lo revierte

La importación entera es una sola entrada en el [Historial](screen:history), *One-time Import · YNAB (CSV)* o *One-time Import · YNAB (API)*. Deshacerla retira cada movimiento, vínculo de transferencia, cuenta, categoría y beneficiario que creó y, como la propia importación, le toca al propietario. Cada movimiento que trajo lo dice en **¿De dónde viene esto?** del registro, con el archivo o el plan y la fecha.

## ¿Otra app?

Se pueden añadir más flujos a la Importación única. Propón uno en [GitHub](https://github.com/MarioLonghi-com/household-spend-tracker/issues/new).
