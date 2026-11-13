---
id: one-time-import
title: Ta med din historik från en annan app (YNAB)
screens: [household, import]
also:
  - importera från YNAB
  - byta från en annan budgetapp
  - ta in hela min historik på en gång
  - personlig åtkomsttoken för YNAB
  - Register.csv från YNAB
  - ångra YNAB-importen
---

Att importera kontoutdrag är en fil per konto, månad efter månad. **Engångsimport** är för dagen då du flyttar hit från en annan budgetapp: den tar in hela den appens historik på en gång (transaktioner, konton, kategorier och överföringar) i stället för ett kontoutdrag i taget. YNAB är det enda flödet hittills.

## Så gör du

Den finns på [hushållets egen sida](screen:household), i avsnittet **Engångsimport**, och det är hushållets ägare som kör den.

1. Tryck på **Starta en engångsimport**.
2. Välj hur du ansluter. Med **Exportfil** laddar du upp YNAB:s export: zip-filen som YNAB ger dig, eller `Register.csv` som ligger i den. `Plan.csv` behövs inte och ignoreras. Med **API-nyckel** klistrar du in en personlig åtkomsttoken från YNAB och väljer planen. Båda sätten finns i alla installationer.
3. Koppla konton och kategorier, och gå sedan igenom alternativen.
4. Tryck på **Förhandsvisning**. Den kör hela importen och kastar den sedan, så att du ser vad som skulle hända.
5. Tryck på **Importera** och läs rapporten.

Token används bara för det besöket och sparas aldrig: den hamnar inte i boken, i Historiken eller i någon logg, och när importen stängs glöms den.

## Du kopplar, appen föreslår

Varje YNAB-konto går till ett konto du redan har, till ett nytt (**Skapa ny…**), eller hoppas över. Varje YNAB-kategori går till en av dina, till en ny (**Skapa ny…**), eller kommer in utan kategori. Förslagen bygger på namnen, och ett konto föreslås bara i samma valuta.

## Vad som följer med

- *Bankens egna ord, via API:et.* För en rad som YNAB importerade från en bank har dess API fortfarande det banken skrev, och det sparas bredvid YNAB:s mottagarnamn; mottagaren är namnet du använde i YNAB. Rapporten pekar sedan på **Namnregler för mottagare**, som kan föreslå regler utifrån den texten innan ditt första kontoutdrag kommer, och säger hur många grupper av banktexter som nu ser ut som en mottagare var. `Register.csv` har bara de rena namnen, så en filimport sparar ingen banktext, och dess rapport säger att regler kan föreslås efter den första kontoutdragsimporten.
- *Bokföringsstatus nollställs.* YNAB:s markeringar för avstämd och bokförd följer inte med: varje rad kommer in som inte bokförd, för att stämmas av här mot banken. Importen ber dig bekräfta det innan den skriver något.
- *Överföringar kopplas.* När båda sidor av en överföring i YNAB importeras blir de en kopplad överföring här. När en sida hoppas över, ligger utanför datumintervallet eller saknas kommer den andra in som en vanlig rad, och rapporten säger varför.

## Dubbletter

En rad som liknar en som redan finns i ett konto du kopplat till (samma belopp, daterad högst 3 dagar ifrån) visas innan något skrivs, och hoppas över om du inte säger att den ska importeras, en i taget eller alla på en gång. En rad som en tidigare engångsimport redan tog in hoppas alltid över, så att köra den igen lägger bara till det som är nytt. En rad i en fil känns igen på vad den säger (konto, datum, belopp, mottagare, kategori och anteckning), inte på sin plats, så en ny export med rader tillagda ovanför hoppar ändå över allt som importerats tidigare.

## Kontoutdrag efteråt

Ett kontoutdrag som importeras senare för samma månader matchar varje rad mot den transaktion som engångsimporten tog in (samma belopp, daterad högst 4 dagar ifrån) och markerar den som sedd av banken i stället för att lägga till den igen. För en rad som YNAB själv hade importerat från banken är matchningen exakt: det är just den bankraden, även om du flyttade dess datum i YNAB. En rad som du har stämt av här lämnas i fred. Att köra engångsimporten igen hoppar fortfarande över en rad som ett kontoutdrag har matchat. Se [Hur importen läser ett kontoutdrag](help:import-how-it-works).

## Delvis, inte allt eller inget

En rad som inte kan läsas (ett felaktigt datum, ett belopp som inte passar valutan) lämnas utanför och resten går ändå in. Rapporten efter bekräftelsen räknar varje rad: importerade, kopplade, överhoppade och varför, misslyckade och varför. Det som inte gick in laddas ner som en `.txt`-fil.

## En ångring tar tillbaka allt

Hela importen är en enda post i [Historik](screen:history), *One-time Import · YNAB (CSV)* eller *One-time Import · YNAB (API)*. Att ångra den tar tillbaka varje rad, överföringskoppling, konto, kategori och mottagare den skapade, och precis som själva importen är det ägarens sak. Varje rad den tog in säger det under **Var kom det här ifrån?** i registret, med filen eller planen och datumet.

## En annan app?

Fler flöden kan läggas till i engångsimporten. Föreslå ett på [GitHub](https://github.com/MarioLonghi-com/household-spend-tracker/issues/new).
