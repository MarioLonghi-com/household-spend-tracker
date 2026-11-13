---
id: import-how-it-works
title: Hur importen läser ett kontoutdrag
screens: [import]
also:
  - vilka filformat kan jag importera
  - varför importerades inte en rad
  - vad betyder redan importerad
  - väntande transaktioner saknas i importen
  - saldot går inte ihop
  - hur kategorin för en importerad rad väljs
---

Vad som händer med ett kontoutdrag från det att du väljer filen tills raderna finns i registret, och alla vägar en rad kan ta. Hur du gör själva importen står i [Importera ett kontoutdrag](help:import-statement).

## Att få in filen

- Ladda upp en fil, eller klistra in raderna om banken bara låter dig kopiera dem från en webbsida. Båda går samma väg.
- Format som läses: CSV och annan avgränsad text (komma, semikolon, tabb eller lodstreck); OFX, både den gamla SGML-formen (1.x) och XML (2.x); gamla Excel `.xls`; och kontoutdrag som PDF vars layout appen känner till.
- Läses inte: `.xlsx` (spara den som CSV eller `.xls`), camt.053 och MT940. De är kända, men ingen riktig exempelfil har setts här, och ett format byggt enbart utifrån sin specifikation har aldrig testats mot en riktig fil.
- En fil som är långt större än ett kontoutdrag nekas, med en mening om vilken gräns den gick över: en PDF på mer än 50 sidor, eller en som packas upp till långt mer text och grafik än ett kontoutdrag rymmer; en `.xls` vars första blad går över en miljon celler; en enskild cell längre än 128 KB. Bara det första bladet i en `.xls` läses.
- Ett konto per import, och du väljer det. Om filen säger vilket konto den gäller (kontonumret som en OFX-fil anger, en identifierare i filens namn eller ett IBAN på de första raderna) väljs kontot åt dig och skärmen säger varför. Om filen nämner ett annat konto än det du valde får du veta det. Om den inte nämner något, och filnamnet innehåller en kod som kan vara kontots märkning, erbjuder skärmen att spara koden som märkning för kontot du väljer, bara om du säger **Lägg till**.
- Samma fil två gånger nekas medan den första väntar eller är importerad, med ett sätt att öppna den som väntar. När en import har ångrats kan dess fil gå in igen.
- Ett sparkontoutdrag som hamnat på fel sparficka nekas när varje rad nämner det andra kontots ficka.
- Agenter med en nyckel kan förbereda rader genom samma kontroller. Historiken visar personen som nyckeln tillhörde och agenten som använde den.

## Hur filen läses

Inget skrivs in för hand. Varje gissning nedan visas överst i förhandsvisningen, så en felaktig syns innan något skrivs.

- *Var tabellen börjar.* Ett rubrikblock ovanför den (kontonummer, kontohavare, saldo) hoppas över. Teckenkodningen räknas ut, så namn med accenter, en Windows-exports € och typografiska citattecken och en UTF-16-fil av typen "Unicode-text" kommer fram oskadda. En UTF-16-fil utan byte-order-markering nekas med en uppmaning att spara den igen som UTF-8, i stället för att gissas.
- *Datumet.* En kolumn med ett datumnamn på något av flera språk, eller förkortad som spanska banker gör (`F. Valor`, `F. Operación`). Om dagen eller månaden kommer först avgörs av hela filen, och du varnas när inget i den avgör saken. Datum skrivna med klockslag behåller bara datumet. När en fil har både ett startdatum och ett *slutfört* datum används det slutförda: det är när banken bokförde den, datumet som saldot följer och datumet som den andra sidan av en överföring har.
- *Beloppet.* Antingen en kolumn med tecken, eller separata kolumner för pengar in och ut. Valutasymboler, tusentalsavgränsare och parenteser för negativa tal läses, liksom ett minus skrivet sist (`12,50-`) eller som typografiskt minus eller tankstreck, och debet eller kredit skrivet efter siffran (`12.50 DR` är pengar ut, `12.50 CR` pengar in). Ett belopp med två tecken (`-12.50 DR`), eller ett som inte alls är ett tal, nekas med orsaken i stället för att läsas som noll. Om komma eller punkt är decimaltecknet räknas ut från beloppen och saldot. När vartenda belopp kan läsas på båda sätten (`1.500` är femtonhundra i Spanien och en och en halv i Storbritannien) avgör kontots land, eller punkt när det saknar land, och du varnas om att det antogs. Om alla belopp i filen är positiva och inget säger åt vilket håll de går varnas du: att importera den som den är skulle läsa en månads utgifter som inkomst.
- *Aldrig beloppet:* ett saldo, en ränta (AER, NIR) eller en datumkolumn, även när namnet liknar ett belopp.
- En avgiftskolumn blir en egen rad under *Bank fees*, bredvid betalningen den togs ut på. Själva betalningen ändras inte.
- Ett löpande saldo läses och kontrolleras (se nästa avsnitt). Det tas aldrig som belopp.
- Allt banken skrev sparas med varje rad, under bankens egna kolumnnamn, så att en transaktion kan förklaras även när filen är borta.

## Kontroller av filen som helhet

- *En fil med flera konton.* Vissa banker lägger mer än ett konto i en fil och skiljer dem åt med en Product-kolumn. Revoluts kontoutdrag innehåller lönekontot (`Current`) och varje sparficka (`Deposit`). Varje konto tar en produkt: ett lönekonto tar `Current` om du inte anger dess **Produkt i kontoutdraget** på skärmen [Konton](screen:accounts). De andra raderna listas som **Inte för det här kontot**. Om ett konto inte har sagt vilken produkt det tar och ingen säker gissning finns nekas filen med instruktioner.
- *En fil i en annan valuta.* En Currency-kolumn (eller Moneda, Divisa, CCY) och en OFX-fils egen valuta jämförs med kontots. Varje belopp bokförs i kontots valuta, så en rad i en annan valuta importeras aldrig. I en tabell som också har rader i kontots valuta listas den som **Inte för det här kontot**; annars, och alltid i en OFX-fil (som är ett enda konto), som **Kunde inte läsas**, med bankens kurs om den angav en. Inget räknas om. I båda fallen nämner orsaken båda valutorna. En fil där varje rad säger att den är i en annan valuta nekas. En agents rad som anger sin valuta kontrolleras på samma sätt, och en i en annan valuta blir **Kunde inte läsas**. En kolumn som säger vad ett köp kostade innan det växlades (Original, Moneda origen, Local, Transaction currency) räknas inte: det beloppet är redan i kontots pengar. Om två kolumner båda kan vara valutan beloppen är i och ingen av dem är namngiven efter kontot, fakturan eller avräkningen används ingen av dem, och förhandsvisningen säger det.
- *Går saldot ihop?* Varje rads saldo ska vara det föregående plus vad raden flyttade. Om det bryts säger förhandsvisningen var: en rad saknas, eller rader från ett annat konto är inblandade. Om en siffra i filen är för stor för att bokföras som pengar avvisas den raden och förhandsvisningen säger att saldot inte kontrollerades.
- *Fortsätter det där boken slutar?* Saldot före filens första rad ska stämma med vad registret har för dagen innan. Om det inte gör det visar förhandsvisningen skillnaden: troligen saknas ett kontoutdrag mellan din senaste import och den här.
- En mottagarregel som tar större delen av en fil, eller en som är för kort för att vara säker, pekas ut på raderna den tog.

## Vad som händer med varje rad

Förhandsvisningens kolumn **Vad som händer** ger varje rad ett av dessa:

- **Ny** (`created`): blir en ny transaktion när du bekräftar. Om raden är ena sidan av en överföring som importen är säker på kopplas den också till den andra sidan.
- **Finns redan** (`matched_existing`): den här skrev du in själv, eller så tog en engångsimport in den: samma belopp, daterad högst 4 dagar ifrån, eller, för en rad som YNAB hade importerat från banken, just den bankraden, vilket datum den än har nu. Din rad behålls och markeras som sedd av banken i stället för att en andra läggs till. Du kan avvisa matchningen i förhandsvisningen med **Inte samma sak – lägg till den separat**.
- **Redan importerad** (`duplicate_skipped`): raden finns redan i kontot, från den här filen eller från ett tidigare kontoutdrag som överlappar den. Inget läggs till.
- **Behöver granskas** (`needs_review`): raden den matchade ändrades mellan förhandsvisningen och bekräftelsen, eller var redan matchad mot en annan rad i ett kontoutdrag. Den lämnades orörd och importerades inte.
- **Kunde inte läsas** (`rejected`): raden har inget datum eller belopp som gick att läsa, dess belopp, avgift eller saldo är för stort för att bokföras som pengar, den säger att den är i en annan valuta än kontots, eller banken säger att den inte blev av eller inte är slutförd ännu (se nedan). En dålig rad kostar en rad, aldrig filen.
- **Inte för det här kontot** (`skipped`): läst utan problem och medvetet inte importerad. Den hör till ett annat konto i en fil som innehåller flera (en annan produkt eller en annan valuta), eller så flyttar den inga pengar alls.

### Rader som banken säger inte blev av, eller inte har blivit av ännu

En statuskolumn som säger `REVERTED`, `DECLINED`, `FAILED`, `CANCELLED` (eller `CANCELED`), `REJECTED` eller `ANULADO` betyder att inga pengar flyttades, och raden nekas med bankens eget ord som orsak. `PENDING`, `PENDIENTE` eller `HOLD` betyder att den inte är slutförd: den nekas den här gången och importeras från nästa kontoutdrag när den har slutförts, så att dricks eller en växelkurs som läggs på vid slutförandet inte kan skapa en andra kopia.

### Rader som inte flyttar några pengar

En rad på `0.00`, till exempel en dags ränta som avrundades till ingenting eller en "Closing transaction", visas och importeras inte.

### Hur "redan importerad" avgörs

Med bankens eget transaktions-id när filen har ett (OFX `FITID`). Annars med beloppet, datumet och det löpande saldot efter raden, som är detsamma vilka datum din nedladdning än täckte. Utan saldo är det beloppet, datumet och radens plats bland identiska rader den dagen. Kontoutdrag som överlappar importerar därför bara det som är nytt. Rader som importerades av en äldre version av appen känns fortfarande igen.

En rad räknas som redan importerad bara så länge dess transaktion finns i registret. Tar du bort transaktionen blir raden ny igen: nästa kontoutdrag som innehåller den, eller samma fil skickad igen med **Importera ändå**, tar tillbaka den, igenkänd på samma identitet. Kontoutdraget är det som gäller. Att ångra i [Historik](screen:history) är den andra vägen tillbaka, och det återställer också allt du hade ändrat på transaktionen.

### Mottagare och kategori

Dina namnregler för mottagare avgör mottagaren, och mottagarens kategorisering avgör kategorin (se [mottagarregler](help:payee-rules)). Namn och regler jämförs utan hänsyn till versaler, accenter, sorts streck och mellanslag du inte kan se, så *Café Sol* och *CAFE SOL* är en mottagare; en regel med reguljärt uttryck prövas mot bankens egna ord och även mot den enklare formen. En kategori du väljer i förhandsvisningen går före båda. När ingen av dem säger något avgör bankens fasta formulering för några slags rader: ränta går till mottagaren *Interest* och *Interest income*, Revoluts investeringskonto och robo portfolio till *Investments*, och avgifter till *Bank fees*. En kategori som importen behöver och som du inte har än skapas när du bekräftar, och tas bort igen om importen ångras. Det banken skrev skrivs aldrig över.

## Förberedd, sedan bekräftad, sedan möjlig att ångra

- Inget når registret förrän du bekräftar. En läst fil förbereds och väntar i kön på skärmen Importera, under **Väntar på granskning**, där den kan öppnas igen eller kastas.
- Varje gång en förberedd import öppnas kontrolleras den igen mot registret som det ser ut nu. En dubblett vars original har ångrats sedan dess blir ny igen, och förhandsvisningen säger hur många rader som ändrades. Bekräftelsen kontrollerar en gång till, så en flik som lämnats öppen kan inte importera en rad två gånger.
- En import är en händelse i Historiken, och att ångra den tar bort varje rad den lade till, varje överföringskoppling den gjorde och varje kategori den skapade. Avstämda rader matchas aldrig som **Finns redan**.

Överföringar mellan dina egna konton har egna regler: se [hur importerade överföringar matchas](help:import-transfers). Hur en fil hittar sitt konto står i [kontoidentifierare](help:account-identifiers).
