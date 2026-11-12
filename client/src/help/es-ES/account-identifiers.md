---
id: account-identifiers
title: Decirle a la app cómo llama tu banco a una cuenta
screens: [accounts, import]
also:
  - el extracto elige la cuenta equivocada
  - añadir un IBAN a una cuenta
  - identificadores de cuenta
  - no se vinculan las transferencias entre mis cuentas
  - cómo escriben los bancos nuestros nombres
  - identificadores sugeridos
---

Los identificadores son los números y nombres que usan los bancos para tus cuentas. Con ellos un archivo de extracto encuentra su cuenta en la pantalla Importar, y una transferencia encuentra su otro lado.

## Añadir uno

1. Abre [Cuentas](screen:accounts) y pulsa **Ajustes** en la cuenta.
2. En **Cómo la llaman los bancos**, elige la **Clase** y escribe el **Valor**.
3. Pulsa **Añadir**.

El nombre de un miembro del hogar va en **Cómo escriben los bancos nuestros nombres**, en la propia pantalla Cuentas, porque no pertenece a ninguna cuenta. Si el nombre de una cuenta contiene su IBAN o un número largo, la app ofrece añadirlo por ti.

## Las clases

- `iban`, `number`, `card`: se comparan sin tener en cuenta los espacios. Los números cortos (menos de seis caracteres) solo coinciden como palabra entera.
- `alias`: un nombre que usa el banco para la cuenta, como el nombre de un apartado de ahorro. Se compara por palabras enteras.
- `file_tag`: el código que pone un banco en el nombre del archivo que descargas. Solo sirve para reconocer archivos.
- `holder`: cómo escribe un banco el nombre de un miembro del hogar. No pertenece a ninguna cuenta: significa "es nuestro propio dinero moviéndose". Los bancos escriben el nombre de una persona de muchas formas, así que un nombre de dos palabras o más coincide en cualquier orden, con iniciales para algunas de sus palabras, y con la última palabra del texto cortada (cuatro letras o más). Un nombre de tres palabras o más también coincide cuando dos de ellas van juntas y una tiene cuatro letras o más. Un nombre de una sola palabra solo coincide como esa palabra entera. Un nombre solo hace que una transferencia quede *sugerida*, nunca vinculada por sí solo; la pantalla Cuentas muestra con qué coincide cada nombre.

## Identificadores sugeridos

La pantalla Cuentas también lista, en **Identificadores sugeridos**, identificadores que tu registro ya usa y que nadie ha añadido: palabras del otro lado de transferencias que has vinculado, IBAN y números de tarjeta cuyos dígitos de control son correctos, números `A/C` y de sort code, nombres de apartados entre comillas, el propio nombre de una cuenta y el código que lleva cada archivo de extracto de una cuenta. Cada uno dice cuántos movimientos lo mencionan (**Menciones**) y cuántas parejas de transferencia vincularía añadirlo (**Vincularía**).

Ninguno se añade solo. Un identificador permite que las transferencias se vinculen sin preguntar, así que uno equivocado hace vínculos equivocados: cada uno espera a **Añadir** o **Ignorar**, y uno ignorado no se vuelve a ofrecer.

Consulta [cómo se emparejan las transferencias importadas](help:import-transfers) para ver qué cambia un identificador, y [Cómo lee la importación un extracto](help:import-how-it-works) para ver cómo se asigna un archivo a su cuenta.
