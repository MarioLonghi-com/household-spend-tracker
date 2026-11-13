---
id: import-statement
title: Importera ett kontoutdrag
screens: [import, register]
also:
  - ladda upp ett kontoutdrag
  - hämta in transaktioner från banken
  - klistra in rader från bankens webbplats
  - importera en CSV- eller OFX-fil
  - slutföra en import jag lämnade halvvägs
  - ångra en import
---

Ett kontoutdrag går in i ett konto i taget. Inget hamnar i registret förrän du säger till, och hela importen kan ångras på en gång.

## Importera en fil

1. Öppna [Importera](screen:import).
2. Under **Till vilket konto** väljer du kontot som kontoutdraget hör till. Om filen säger vilket konto den gäller väljer appen det åt dig och säger varför; om filen nämner ett annat konto än det du valde säger den till.
3. Välj filen under **Fil**: en CSV, en OFX- eller QFX-nedladdning, en .xls-export från banken eller ett kontoutdrag som PDF. Om banken bara låter dig kopiera rader från sin webbplats klistrar du in dem i rutan nedanför i stället.
4. Tryck på **Läs filen**. Förhandsvisningen, **Vad detta skulle göra**, visar vad som upptäcktes i filen och varje rad med vad som kommer att hända med den.
5. Gå igenom raderna. Klicka på en rads kategori för att ändra den; skriv någon del av ett kategorinamn, eller töm rutan för att gå tillbaka till förslaget. Om andra rader har samma mottagare erbjuds du att ge dem samma kategori, och med **Ja, alla och ställ in regeln** att fortsätta göra det för kommande kontoutdrag. **Nej, bara den här** ändrar bara den raden.
6. Avmarkera en rad i kolumnen **Lägg till** för att lämna den utanför.
7. När en rad står som **Finns redan** men egentligen är en annan transaktion trycker du på **Inte samma sak – lägg till den separat**. **Ångra – lägg till som ny i stället** tar tillbaka det.
8. Tryck på **Importera dessa**. **Avbryt** stänger förhandsvisningen utan att något skrivs.

Om filen inte nämner något konto men filnamnet innehåller en kod som kan vara kontots märkning erbjuder skärmen att spara den: tryck på **Lägg till** för att spara den, eller **Ignorera**. Se [kontoidentifierare](help:account-identifiers).

## Importer som lämnats halvvägs

En fil som du läst men aldrig importerat väntar under **Väntar på granskning**. Tryck på **Öppna** för att fortsätta där du slutade, eller **Kasta** för att slänga den; inget i den har nått registret. Att läsa samma fil igen nekas så länge en väntar, med **Öppna den importen** för att gå dit, **Importera ändå** och **Kasta den förberedda**.

## Ångra en import

En import är en post i [Historik](screen:history). Ångrar du den där tas varje rad den lade till, varje överföringskoppling den gjorde och varje kategori den skapade bort.

## Mer

- [Hur importen läser ett kontoutdrag](help:import-how-it-works): format, kontroller och vad varje utfall betyder.
- [Hur importerade överföringar matchas](help:import-transfers).
- Kommer du från YNAB? Ta in hela din historik på en gång med [Engångsimport](help:one-time-import).
