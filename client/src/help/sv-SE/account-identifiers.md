---
id: account-identifiers
title: Berätta för appen vad banken kallar ett konto
screens: [accounts, import]
also:
  - kontoutdraget väljer fel konto
  - lägga till ett IBAN på ett konto
  - kontoidentifierare
  - överföringar mellan mina konton kopplas inte
  - hur bankerna skriver våra namn
  - föreslagna identifierare
---

Identifierare är de nummer och namn som bankerna använder för dina konton. Det är med dem en kontoutdragsfil hittar sitt konto på skärmen Importera, och en överföring hittar sin andra sida.

## Lägg till en

1. Öppna [Konton](screen:accounts) och tryck på **Inställningar** vid kontot.
2. Under **Vad bankerna kallar det** väljer du **Slag** och skriver in **Värde**.
3. Tryck på **Lägg till**.

En hushållsmedlems namn läggs in under **Hur bankerna skriver våra namn** på själva skärmen Konton, eftersom det inte hör till något konto. Om ett kontos eget namn innehåller dess IBAN eller ett långt nummer erbjuder appen att lägga till det åt dig.

## Slagen

- `iban`, `number`, `card`: matchas utan hänsyn till mellanslag. Korta nummer (under sex tecken) matchar bara som ett helt ord.
- `alias`: ett namn banken använder för kontot, till exempel en sparfickas namn. Matchas som hela ord.
- `file_tag`: koden en bank sätter i den nedladdade filens namn. Används bara för att känna igen filer.
- `holder`: hur en bank skriver en hushållsmedlems namn. Det hör inte till något konto: det betyder "det här är våra egna pengar som flyttas". Banker skriver en persons namn på många sätt, så ett namn med två ord eller fler matchar i valfri ordning, med initialer för några av orden och med textens sista ord avkortat (fyra bokstäver eller fler). Ett namn med tre ord eller fler matchar också när två av dem står tillsammans och ett är fyra bokstäver eller längre. Ett namn med ett enda ord matchar bara som just det hela ordet. Ett namn gör bara en överföring *föreslagen*, aldrig kopplad på egen hand; skärmen Konton visar vad varje namn matchar.

## Föreslagna identifierare

Skärmen Konton listar också, under **Föreslagna identifierare**, identifierare som ditt register redan använder och som ingen har lagt till: ord på andra sidan av överföringar du har kopplat, IBAN och kortnummer vars kontrollsiffror stämmer, `A/C`- och sort code-nummer, fickonamn inom citattecken, ett kontos eget namn och koden som varje kontoutdragsfil för ett konto bär. Var och en visar hur många rader som nämner den (**Omnämnanden**) och hur många överföringspar som skulle kopplas om den lades till (**Skulle koppla**).

Ingen läggs till av sig själv. En identifierare låter överföringar kopplas utan att fråga, så en felaktig ger felaktiga kopplingar: var och en väntar på **Lägg till** eller **Ignorera**, och en som ignorerats erbjuds inte igen.

Se [hur importerade överföringar matchas](help:import-transfers) för vad en identifierare ändrar, och [Hur importen läser ett kontoutdrag](help:import-how-it-works) för hur en fil matchas mot sitt konto.
