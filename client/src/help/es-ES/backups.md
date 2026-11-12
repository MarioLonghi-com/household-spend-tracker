---
id: backups
title: Hacer una copia de seguridad del libro y restaurarla
screens: [application]
also:
  - hacer una copia de seguridad
  - descargar una copia de seguridad
  - guardar la copia en google drive o dropbox
  - restaurar una copia de seguridad
  - pasar el libro a otro ordenador
  - qué es secret.key
---

Una copia de seguridad es una copia completa del libro: todos los hogares de esta instalación, con los tickets incluidos. Las copias se hacen en la pantalla [Gestión de la aplicación](screen:application), que solo puede abrir un propietario.

## Hacer una copia

1. Abre **Gestión de la aplicación** desde el menú **Administración**.
2. En **Base de datos**, en el apartado **Hacer una copia de seguridad de la base de datos**, pulsa **Hacer copia de seguridad ahora**.

La copia se comprueba y se escribe en la carpeta de copias mientras la aplicación sigue funcionando, y aparece en la lista bajo el botón. **Última copia de seguridad** indica cuándo se hizo la más reciente. Aquí nada elimina una copia por su cuenta; **Eliminar** quita una cuando lo confirmas, y no se puede deshacer.

## Sacar una copia de esta máquina

Una copia que se queda junto al libro no sirve de nada si se pierde la máquina. En la fila de una copia:

- **Descargar** te da un zip.
- **Guardar en Drive/Dropbox…** te ofrece las formas que tiene este navegador de dejar el zip en una carpeta en la nube: el menú de compartir del dispositivo, **Elegir una carpeta…** para una carpeta sincronizada, o una descarga para que la subas tú. La página nunca se comunica con Google ni con Dropbox.

El zip contiene el libro (`spendtracker.sqlite3`), un `manifest.json` con el que la restauración lo comprueba y un `README.txt` que explica a quien lo abra más adelante qué es y cómo restaurarlo.

## La clave secreta

Los códigos de la aplicación de autenticación están sellados con una clave, `secret.key`, que la copia solo lleva si lo pides. Marca la casilla sobre la lista para incluirla; desde ese momento, cada descarga te pide la contraseña y un código. Guarda ese zip donde solo tú puedas abrirlo: con él y la contraseña de un miembro, cualquiera puede iniciar sesión como ese miembro.

Sin la clave, el zip se restaura sin más en esta misma instalación. En cualquier otro sitio se rechazan todos los autenticadores, salvo que la clave se aporte por separado; los códigos de recuperación siguen funcionando.

## Restaurar una copia

En un servidor no hay botón de restaurar. Quien administra la instalación detiene la aplicación y ejecuta `make restore FROM=` con el zip o la carpeta de la copia, y añade `KEY=` con la ruta de `secret.key` cuando el zip no la lleva y va a otra instalación. El libro que había se aparta, no se sobrescribe. Una llave de acceso solo funciona con el nombre de host con el que se creó, así que, tras restaurar con otro nombre, los miembros inician sesión con contraseña y código y vuelven a añadir sus llaves de acceso.

Cada actualización hace también su propia copia, que aparece en **Copias de seguridad hechas por actualizaciones**; se conservan las cinco más recientes para poder deshacer la actualización desde **Actualizaciones**.

Nunca hagas una copia copiando el archivo de la base de datos: los cambios recientes están en un segundo archivo a su lado, y la copia de uno solo puede quedar vacía.

## En la aplicación de escritorio

La aplicación de escritorio no tiene autenticadores, así que no hay clave que incluir. Su menú **Archivo** tiene **Hacer copia de seguridad ahora**, **Restaurar desde una copia de seguridad…**, **Elegir la carpeta de copias de seguridad…** y **Mostrar la carpeta de copias de seguridad**. También hace una copia al abrirla por primera vez cada día y al salir si algo ha cambiado. Al restaurar, primero hace una copia del libro actual y después te pide que vuelvas a iniciar sesión; rechaza una copia hecha por una versión más nueva. Restaurar es también la forma de pasar el libro a otro ordenador.
