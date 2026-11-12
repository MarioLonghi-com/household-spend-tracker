---
id: backups
title: Säkerhetskopiera boken och återställ den
screens: [application]
also:
  - göra en säkerhetskopia
  - ladda ner en säkerhetskopia
  - spara säkerhetskopian i google drive eller dropbox
  - återställa en säkerhetskopia
  - flytta boken till en ny dator
  - vad är secret.key
---

En säkerhetskopia är en fullständig kopia av boken: alla hushåll i den här installationen, kvitton inräknade. Säkerhetskopior görs på skärmen [Hantering av appen](screen:application), som bara en ägare kan öppna.

## Gör en säkerhetskopia

1. Öppna **Hantering av appen** från menyn **Administration**.
2. Tryck på **Säkerhetskopiera nu** under **Databas**, i avsnittet **Säkerhetskopiera databasen**.

Kopian kontrolleras och skrivs till mappen för säkerhetskopior medan appen fortsätter att köra, och den syns i listan under knappen. **Senaste säkerhetskopia** visar när den nyaste gjordes. Ingenting här raderar en säkerhetskopia av sig självt; **Radera** tar bort en när du bekräftar, och det går inte att ångra.

## Få ut en kopia från den här datorn

En säkerhetskopia som ligger bredvid boken hjälper inte om datorn försvinner. På en säkerhetskopias rad:

- **Ladda ner** ger dig en zip-fil.
- **Spara till Drive/Dropbox…** visar de sätt webbläsaren har att lägga zip-filen i en molnmapp: enhetens delningsmeny, **Välj en mapp…** för en synkad mapp, eller en nedladdning som du laddar upp själv. Sidan pratar aldrig med Google eller Dropbox.

Zip-filen innehåller boken (`spendtracker.sqlite3`), en `manifest.json` som återställningen kontrollerar den mot och en `README.txt` som berättar för den som öppnar filen senare vad den är och hur den återställs.

## Den hemliga nyckeln

Koderna från autentiseringsappen är förseglade med en nyckel, `secret.key`, som en säkerhetskopia bara tar med om du ber om det. Kryssa i rutan ovanför listan för att ta med den; då frågar varje nedladdning efter ditt lösenord och en kod. Förvara en sådan zip-fil där bara du kommer åt den: med den och en medlems lösenord kan vem som helst logga in som den medlemmen.

Utan nyckeln går zip-filen att återställa i samma installation som den är. Återställd någon annanstans nekas alla autentiseringsappar om inte nyckeln lämnas separat; återställningskoderna fungerar fortfarande.

## Återställ en säkerhetskopia

På en server finns ingen återställningsknapp. Den som sköter installationen stoppar appen och kör `make restore FROM=` med zip-filen eller säkerhetskopians mapp, och lägger till `KEY=` med sökvägen till `secret.key` när zip-filen saknar den och ska till en annan installation. Boken som fanns flyttas undan, den skrivs inte över. En lösennyckel fungerar bara under det värdnamn den skapades för, så efter en återställning under ett annat namn loggar medlemmarna in med lösenord och kod och lägger till sina lösennycklar igen.

Varje uppdatering tar också en egen säkerhetskopia, som visas under **Säkerhetskopior tagna av uppdateringar**; de fem nyaste sparas så att uppdateringen kan ångras från **Uppdateringar**.

Säkerhetskopiera aldrig genom att kopiera databasfilen: de senaste ändringarna ligger i en andra fil bredvid den, och en kopia av bara den ena kan vara tom.

## I skrivbordsappen

Skrivbordsappen har inga autentiseringsappar, så det finns ingen nyckel att ta med. Dess meny **Fil** har **Säkerhetskopiera nu**, **Återställ från säkerhetskopia…**, **Välj mapp för säkerhetskopior…** och **Visa mappen med säkerhetskopior**. Den säkerhetskopierar också vid första starten varje dag och när du avslutar efter en ändring. En återställning säkerhetskopierar först den nuvarande boken och ber dig sedan logga in igen; den vägrar en säkerhetskopia som gjorts av en nyare version. Att återställa är också sättet att flytta boken till en ny dator.
