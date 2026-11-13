---
id: import-transfers
title: Hur importerade överföringar matchas
screens: [import, transfers]
also:
  - varför kopplades inte min överföring
  - importen kopplade fel överföring
  - överföring väntar på det andra kontoutdraget
  - pengar till sparkontot räknas som utgift
  - föreslagen överföring vid import
---

Pengar som flyttas mellan två av dina konton syns i båda bankernas kontoutdrag, ofta importerade med flera dagars mellanrum. Kopplade som en överföring hålls de två raderna utanför Inkomster och utgifter. Så här avgör en import. Hur du kopplar för hand står i [För över pengar mellan dina egna konton](help:transfers).

## Ett möjligt par

Två rader på olika konton, i samma valuta, med exakt motsatta belopp och daterade högst 5 dagar ifrån varandra.

En rad med en kategori är ingen överföring. Den paras aldrig ihop, kopplas aldrig vid bekräftelsen och väntar aldrig, och kategoriseringen sker före matchningen, så en rad som får en kategori av sin mottagarregel eller sin historik lämnas i fred. Ta bort kategorin så matchas den igen.

## Kopplas vid bekräftelsen utan att fråga

Ett par kopplas när importen bekräftas om båda dessa gäller:

- en rad nämner det andra kontot (dess nummer, IBAN, kort eller ett namn banken använder för det; se [kontoidentifierare](help:account-identifiers)), eller så har de två kontona haft överföringar kopplade tidigare, av en rad som nämnde den andra sidan eller av dig. En koppling som gjorts enbart på den historiken räknas inte;
- *och* ingen annan rad kan vara dess par, eller den här är strikt närmast i datum, eller, när datumet inte kan skilja två av dem åt, delar dess två beskrivningar strikt fler ord än någon konkurrents, utan att räkna ord som *TO*, *FROM* eller dina kontonummer.

En koppling som ger två konton historik gör också deras andra par starka, så de kopplas i samma bekräftelse, och av *Link all* på skärmen [Överföringar](screen:transfers), som kopplar tills inget nytt är starkt.

## Föreslås i stället

- När bara beloppen och datumen stämmer, eller när en rad kan paras med fler än en, föreslås paret. Förhandsvisningen säger det, och skärmen Överföringar är där du bekräftar.
- Kopplas aldrig utan att fråga, hur väl det än stämmer: pengar ut från ett kreditkort (ett köp, och dess återbetalning eller cashback är ingen överföring), och en rad vars mottagare har en kategoriregel eller vars ord nämner någon som har betalat dig förut, till exempel en arbetsgivares ersättning för utlägg. De föreslås i stället.

## Par du har sagt nej till

Ett par som du har sagt inte är en överföring, genom att koppla isär det eller med **Ingen överföring** på skärmen Överföringar, matchas aldrig igen. Att ångra det i [Historik](screen:history), eller att koppla paret själv, tar tillbaka det.

## Väntar på det andra kontoutdraget

När en rad nämner ett av dina konton eller en medlem i hushållet och dess andra sida inte har importerats än, väntar den. Den kopplas när det kontoutdraget kommer.

## Två valutor

Två valutor matchas aldrig automatiskt, eftersom beloppen skiljer sig med en kurs som ingen skrev ut. Markera båda raderna i [registret](screen:register) och välj **Koppla som överföring**; kursen räknas ut och sparas.
