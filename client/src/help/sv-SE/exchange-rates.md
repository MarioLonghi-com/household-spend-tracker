---
id: exchange-rates
title: Håll växelkurserna aktuella
screens: [exchange-rates, report-net-worth, report-income-expense]
also:
  - hämta växelkurser
  - ange en kurs för hand
  - rapporten säger att kurserna är inaktuella
  - valuta som inte räknas om
  - vilken kurs använder rapporten
  - importera kurser från en csv-fil
---

Boken räknar aldrig om: varje belopp stannar i sitt kontos valuta. Växelkurserna används bara av rapporterna, som kan visa siffror i hushållets basvaluta. Varje omräknad siffra märks med ≈, och raden *Kurser per* i rapporten visar hur gamla kurserna är. Alla medlemmar kan sköta dem på sidan [Växelkurser](screen:exchange-rates).

## Hämta kurserna

Ingenting hämtas av sig självt.

1. Öppna **Växelkurser**. Raden under **Hämta nu** visar vilka valutor som skulle hämtas, varifrån och från vilket datum.
2. Tryck på **Hämta nu**. Bara datum som inte redan är sparade hämtas, och hämtningen fortsätter även om du lämnar sidan.
3. Vill du hämta vissa datum igen trycker du på **Hämta ett intervall igen…**, väljer **Från** och **Till**, trycker på **Hämta igen** och bekräftar. Hämtade kurser för de datumen ersätter de sparade; kurser angivna för hand rörs inte.

**Hämtningshistorik** visar varje hämtning, vem som startade den och vad den lade till.

## Ange en kurs för hand

För en valuta som ingen källa täcker, eller en kurs du vill bestämma själv:

1. Skriv valutakoden i **Valuta** under **Kurser angivna för hand**. Du kan också trycka på **Ange en kurs** på valutans rad under **Per valuta**.
2. Välj **En dag** eller **En månad** i **För**. En månadskurs gäller alla dagar i månaden.
3. Välj datum eller månad och skriv kursen som enheter av valutan per 1 enhet basvaluta, så som ECB anger den.
4. Tryck på **Lägg till kursen**, eller på **Ersätt kursen** om det redan finns en sparad för det datumet.

För många kurser på en gång väljer du en CSV-fil med kolumnerna `currency,date,rate` i **Kursfil**, granskar förhandsvisningen och trycker på **Spara de här kurserna**. Allt eller inget: en rad med ett problem stoppar hela filen. En kurs angiven för hand kan raderas under **Sparade**, och [Historik](screen:history) kan ta tillbaka den.

## Inställningar

- *Kurskällor, i ordning.* Den första källan som har en kurs inom gränsen för inaktuella kurser används. Från början kommer kurser angivna för hand först, sedan ECB. Ändra ordningen med **Upp**, **Ned** och **Ta bort**, eller lägg till *dina egna överföringar*: kurserna som dina egna valutaöverföringar gav, avgifter inräknade, de dagar du växlade pengar.
- **Räkna en kurs som inaktuell efter (dagar)**: sju från början. En inaktuell kurs används ändå, och rapporten säger det.
- **Kurs för månadssiffror**: **Månadsgenomsnitt**, **Kurs vid månadsslut** eller **Dagskurs per rad**. Det är den kursen [Inkomster och utgifter](screen:report-income-expense) räknar om med när den visar alla valutor i basvalutan. [Nettoförmögenhet](screen:report-net-worth) räknar alltid om varje saldo med kursen för just det datumet.

Tryck på **Spara inställningar**. Att byta källor eller månadskurs ändrar omräknade siffror bakåt i tiden, så appen frågar först; tryck på **Ändra** för att fortsätta. Ändringen skrivs till Historik.

## Se vad som är sparat

**Per valuta** visar varje valutas källa, första och senaste kurs, täckning och luckor. **Månader** visar en remsa med de månader som har kurser, och **Bläddra bland kurser** listar de sparade kurserna per dag eller per månad, med siffran en rapport skulle använda. En valuta under **Utan täckning** har ingen källa; ange dess kurser för hand. Själva basvalutan ställs in på [hushållets sida](screen:household).
