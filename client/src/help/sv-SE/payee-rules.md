---
id: payee-rules
title: Namnge och kategorisera mottagare automatiskt
screens: [rules, payee-categorisation, payees]
also:
  - mottagarregler
  - byta namnet banken ger en butik
  - samma butik dyker upp under många namn
  - alltid lägga en mottagare i samma kategori
  - slå ihop två mottagare
  - sluta gissa kategori för en mottagare
---

Varje importerad rad går igenom två steg. En regel under **Namnregler för mottagare** gör om bankens ord till en mottagare, och sedan ger den mottagarens inställning under **Kategorisering av mottagare** transaktionen dess kategori. **Slå ihop mottagare** slår ihop två stavningar av samma mottagare. Alla tre ligger under **Mottagare** i ditt hushålls meny.

## Skriva en namnregel

1. Öppna [Namnregler för mottagare](screen:rules) och tryck på **Lägg till en regel**.
2. Under **Och sedan** väljer du **kalla det en mottagare** för en butik, eller **ta bort den här biten och se vad som blir kvar** för en betaltjänst vars ord står före många olika butiker.
3. Under **När raden på kontoutdraget** väljer du **innehåller**, **börjar med**, **är exakt** eller **matchar mönstret** (ett reguljärt uttryck) och skriver bankens ord i **Den här texten**.
4. För en butik skriver du dess namn i **Kalla mottagaren**. För att ta bort text lämnar du oftast **Sätt in detta i stället (lämna tomt för att ta bort det)** tomt.
5. Tryck på **Vad skulle detta matcha?** för att se hur många rader som redan finns i boken den skulle ta, med exempel.
6. Tryck på **Skapa**.

En regel jämförs med *bankens egna ord*, inte med mottagaren som registret visar. Öppna en transaktion och tryck på **Var kom det här ifrån?** för att se exakt vad som kom in. Stora och små bokstäver spelar ingen roll. Regler som tar bort text körs först, var och en på det som den förra lämnade; sedan vinner den första namnregeln som matchar. **Ordning (lägre körs först)** bestämmer ordningen. Kryssa ur en regels ruta **På** för att pausa den; **Radera** tar bort den direkt.

## Tillämpa reglerna på det som redan finns

Reglerna körs när ett kontoutdrag läses in, så en ny regel ändrar inget som redan finns i registret. Direkt efter **Skapa** öppnas panelen **Tillämpa reglerna på det som redan finns här**: den visar hur många rader som skulle flyttas och till vilken mottagare innan du bekräftar. **Bara rader som ingen har rättat** lämnar en mottagare som någon har valt för hand i fred. Det blir en enda post i [Historik](screen:history), så en enda ångring lägger tillbaka alla rader.

När banken skickar samma butik med en ny referens varje gång visas ett kort med rubriken **De här ser ut som samma butik**. **Skapa den här regeln** påbörjar en regel med texten redan ifylld.

## Välja en mottagares kategori

1. Öppna [Kategorisering av mottagare](screen:payee-categorisation). Kolumnen **Kategoriserad som** visar vad varje mottagare hittills har kategoriserats som.
2. Tryck på **Kategorisering** på mottagarens rad.
3. Välj **Kategorisera automatiskt utifrån mottagarens historik** (den vanligaste kategorin bland dess tre senaste transaktioner), **Använd alltid mitt val** och en kategori, eller *Kategorisera inte automatiskt*, som lämnar nya transaktioner tomma.
4. Tryck på **Spara**.

Det här avgör kategorin för mottagarens nya transaktioner. En arkiverad kategori kan inte vara det fasta valet.

## Slå ihop två stavningar

1. Öppna [Slå ihop mottagare](screen:payees) och använd **Hitta en mottagare**.
2. Tryck på **Slå ihop…** på den stavning du vill bli av med.
3. Under **Till** väljer du mottagaren som ska finnas kvar och trycker på **Slå ihop**.

Alla transaktioner och regler som nämnde den första mottagaren nämner nu den du behöll, och den första försvinner. Du kan ångra det i Historik. Mottagare som står för en överföring går inte att slå ihop. Mottagare som bara skiljer sig i accenter, bindestreck eller osynliga mellanslag listas under **Stavningar av ett namn**; tryck på **Granska…** och välj under **Behåll** den stavning som ska finnas kvar.

Hur du ordnar själva kategorierna står i [Ordna kategorier och grupper](help:categories); om själva importen läser du i [Importera ett kontoutdrag](help:import-statement).
