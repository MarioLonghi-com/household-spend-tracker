---
id: transfers
title: För över pengar mellan dina egna konton
screens: [transfers, register]
also:
  - lägga in en överföring
  - koppla två rader som överföring
  - överföring mellan valutor
  - pengar till sparkontot syns som utgift
  - koppla isär en överföring
  - ingen överföring
---

En överföring är pengar som flyttas mellan två av hushållets egna konton. Det är en enda händelse som består av två rader, en i varje konto, och den har ingen kategori: den är varken en utgift eller en inkomst och räknas därför inte med i [Inkomster och utgifter](screen:report-income-expense).

## Lägg in en överföring för hand

1. Öppna [Transaktioner](screen:register), tryck på **Lägg till transaktion** och välj **Överföring**.
2. Välj kontot pengarna lämnar under **Från** och kontot de kommer till under **Till**.
3. Ange **Datum**, skriv beloppet som lämnar kontot utan minustecken och fyll i en **Beskrivning** om du vill.
4. Tryck på **För över**.

Båda raderna sparas tillsammans, var och en med *Transfer :* och det andra kontots namn som mottagare. Ändrar du beloppet på den ena sidan ändras den andra också, och ångrar du händelsen i [Historik](screen:history) försvinner båda. Formuläret kräver två öppna konton.

## Mellan två valutor

När kontona har olika valutor frågar formuläret även efter beloppet som kommer in. Skriv det belopp som banken faktiskt satte in: appen hittar aldrig på en kurs. Den visar vilken kurs de två beloppen motsvarar och sparar den på paret.

## Koppla ihop två rader du redan har

När varje banks kontoutdrag har tagit in sin egen sida gör du de två raderna till en överföring:

1. Markera i [Transaktioner](screen:register) uttaget från det ena kontot och insättningen på det andra.
2. Tryck på **Koppla som överföring** i fältet som visas.

Raderna måste ligga i olika konton och gå åt motsatt håll. I samma valuta måste beloppen stämma exakt; mellan valutor får de skilja sig, och kursen mellan dem räknas ut och sparas. Varje rad behåller sitt eget datum och bankens text, men förlorar sin kategori. En rad som är del av en uppdelning eller ett jobbutlägg går inte att koppla. Vill du ta bort kopplingen markerar du ena sidan och trycker på **Koppla isär överföring**.

## Skärmen Överföringar

Importer kopplar de par de är säkra på medan de körs (se [hur importer matchar överföringar](help:import-transfers)). Skärmen [Överföringar](screen:transfers) visar resten, bara i samma valuta:

- **Säkra**: en rad nämner det andra kontot, eller så har de två kontona haft kopplade överföringar förut. *Koppla alla* kopplar alla par i avsnittet.
- **Värda en titt**: beloppen stämmer och datumen ligger nära, men inget nämner det andra kontot. Tryck på **Koppla** om det är en överföring, eller på **Ingen överföring** så erbjuds paret aldrig igen. Markera flera och tryck på **Koppla markerade** för att koppla dem på en gång.
- **Väntar på det andra kontoutdraget**: rader som nämner ett av dina konton men vars andra sida inte finns i boken än. De kopplas när det kontoutdraget importeras. Är en av dem ingen överföring ger du den en kategori med **Ingen överföring – kategorisera som…**.
- **Kopplade bara genom historiken**: kopplingar som ingen person har valt. Tryck på **Koppla isär** om en är fel, eller på **Behåll** för att bekräfta den.
