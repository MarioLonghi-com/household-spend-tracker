---
id: attach-receipt
title: Bifoga ett kvitto till en transaktion
screens: [receipts, register]
also:
  - lägga till ett foto av ett kvitto
  - ladda upp en faktura som PDF
  - matcha ett kvitto med en betalning
  - var sparas mina kvitton
  - spara originalfotot
  - kvitton som väntar i inkorgen
---

Ett kvitto är ett foto, en skanning eller en PDF som sparas på en transaktion som underlag. Du kan bifoga det direkt till transaktionen, eller lägga det i kvittoinkorgen först och matcha det när kontoutdraget kommer. Båda sätten är helt vanliga.

## På en transaktion

1. Öppna [Transaktioner](screen:register) och klicka på raden för att öppna dess panel.
2. Välj **Bifoga kvitton** under **Kvitto** och välj en eller flera filer. Du kan också dra filer till knappen eller klistra in en skärmbild medan panelen är öppen.
3. Vänta tills varje fil är klar. Servern komprimerar den, och för ett mobilfoto kan det ta några sekunder.

När transaktionen har ett kvitto erbjuder panelen **Lägg till fler** för ytterligare ett, **Ersätt** för att byta det som visas och **Koppla loss** för att ta bort det från transaktionen. Varken att ersätta eller att koppla loss raderar något: kvittot går tillbaka till inkorgen. I registret markerar ett gem raderna som har ett kvitto.

## Från kvittoskärmen

[Kvitton](screen:receipts) är inkorgen: allt som ännu inte sitter på en transaktion. Foton som skickas [från mobilen](help:snap-receipt) hamnar här.

1. Välj **Lägg till kvitton** för att ladda upp filer utan transaktion, eller klicka på ett som redan väntar.
2. Välj **Bifoga till en transaktion** i dess panel.
3. Listan öppnas på de tio dagarna före och efter när fotot togs, eller när det laddades upp om filen saknar datum. Hitta raden och välj **Bifoga**.

**Inkorg**, **Matchad** och **Alla** växlar mellan det som väntar, det som redan är bifogat och allt. **Kvittoanteckningar** är en fri rad för vad det gällde; i vyn **Lista** kan du skriva den direkt i tabellen.

## Var kvittona sparas

Kvittona sparas i hushållets egen databas på den här servern och ingen annanstans. Vad som sparas väljer du på hushållets sida (klicka på hushållets namn i menyn), under **Kvitton**:

- *Bara läsbara kopior*, det rekommenderade valet: en skärmkopia på högst 2000 pixlar på längsta sidan och en miniatyr, båda komprimerade till AVIF. Ungefär 33 KB per kvitto.
- **Behåll även originalfilen**: dessutom exakt den fil som kameran eller skannern gav. Ungefär 2 MB per kvitto, omkring 70 gånger utrymmet.

En PDF sparas alltid precis som den laddades upp, oavsett val, tillsammans med en bild av första sidan att visa på skärmen. Valet gäller bara nya kvitton: inget som redan är sparat konverteras eller raderas. Om den som driver servern har slagit på original för alla hushåll står det på sidan, och valet går inte att ändra.

**Ladda ner** ger dig originalet när det sparades och annars skärmkopian. **Mer info** visar när och var fotot togs, hämtat ur själva fotot.

## Bra att veta

- Godkända format: JPEG, PNG, HEIC, WebP, AVIF, GIF, TIFF, BMP och PDF, upp till 25 MB per fil.
- Samma fil två gånger på en transaktion, eller två gånger i inkorgen, nekas. Samma fil på två olika transaktioner går bra, för en räkning som gäller två betalningar, och panelen säger att den även finns på den andra.
- När du [delar upp en transaktion](help:split-transaction) hamnar dess kvitton på varje del.
- Att radera ett kvitto på kvittoskärmen kan ångras i [Historik](screen:history).
