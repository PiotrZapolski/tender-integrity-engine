# zmowy.pl - radar zmów przetargowych

Data: 2026-10-12. Status: projekt do akceptacji.

## 1. Cel

System, który na otwartych danych o zamówieniach publicznych z całej Polski wykrywa wzorce
typowe dla zmów przetargowych i pokazuje je w publicznej aplikacji webowej o czytelności
na poziomie produktów Apple/Tesla.

Co powiedział właściciel:

- docelowo cała Polska, wszystkie branże, wszystkie dostępne dane; kolejność etapami
  (województwo, branża, kolejne);
- budujemy porządnie od razu, bez skrótów pod demo;
- aplikacja publiczna; jednoosobowe działalności (JDG) tylko jako NIP, bez nazwy;
- sercem aplikacji jest ranking sygnałów; bardzo ważna strona "Jak to działa" z osią kroków;
- pełna lista kontrolna OECD jako podstawa testów;
- Jev (TypeSafe AI) do decyzji, DeepSeek do tekstu, koszty mają być niskie;
- walidacja na prawdziwych sprawach UOKiK; ręczny przegląd sygnałów robi Claude;
- serwer: główny box Hetzner; nic nie uruchamiamy na laptopie.

Założenia (do korekty przez właściciela):

- kod w nowym repo na GitHubie, deploy wyłącznie przez Git i GitHub Actions;
- aplikacja pokazuje sygnały statystyczne, nigdy zarzuty.

Kryteria sukcesu:

1. Dane BZP dla całej Polski od 01.2021 i listy oferentów z e-Zamówień są zebrane i
   odświeżane codziennie.
2. W teście A walidacji (sekcja 9) co najmniej 60% przetargów ze zmów z decyzji UOKiK
   trafia do najwyższych 5% ryzyka w swoim rynku.
3. Aplikacja na zmowy.pl działa publicznie, z rankingiem, stronami sygnałów, profilami
   firm i zamawiających oraz stroną "Jak to działa".
4. Koszt API AI dla całego archiwum poniżej 50 USD, potem kilka USD miesięcznie.

## 2. Unikalność (stan na 2026-10-12)

- z-dykty.pl czyta z ogłoszeń o wyniku w BZP tylko liczbę ofert, cenę min/max, tryb,
  wartości i zwycięzcę (metodologia serwisu). Nie ma list przegranych oferentów, grafu
  współoferentów ani testów na zmowę. Pozostałe serwisy sieci (leniwyposel.pl,
  openbooks.pl, psoz.pl, cpdp.media) nie zajmują się przetargami.
- Ofertarium wyciąga otwarcia ofert, ale dla wykonawców (wycena), bez wykrywania zmów.
- Integrity Watch Red Flags (Transparency International): tylko TED, dane 2020-2022,
  nieaktualizowane, flagi na poziomie ogłoszenia.
- UOKiK nie ma publicznego narzędzia analitycznego; 7-8.10.2026 prowadził z OECD i KE
  warsztaty o wykrywaniu zmów.

Przewaga zmowy.pl: imienne listy oferentów z cenami z platform zakupowych plus pełna lista
kontrolna OECD liczona na całej Polsce.

## 3. Źródła danych (zweryfikowane w tej sesji)

| Źródło | Dostęp | Co daje |
|---|---|---|
| BZP, `GET https://ezamowienia.gov.pl/mo-board/api/v1/notice` | otwarte, bez klucza; filtry `NoticeType`, `PublicationDateFrom/To`, `PageSize` (500 działa) | pełne ogłoszenia jako `htmlBody`; dla wyniku: liczba ofert, cena min/max, cena zwycięzcy, zwycięzca z NIP, wartość umowy, data, części, adres platformy (pole 1.6), `tenderId` (OCDS) |
| e-Zamówienia, `GET /mp-readmodels/api/Search/GetTender?id={ocds}` | otwarte | metadane postępowania, `isProceedingOnPlatform` |
| e-Zamówienia, `GET /mp-readmodels/api/Search/GetTenderDocuments?tenderId={ocds}` | otwarte | lista dokumentów z nazwami, np. "INFORMACJA Z OTWARCIA OFERT.pdf" |
| platformazakupowa.pl | strona publiczna, limit tempa (HTTP 429 przy pierwszym zapytaniu) | otwarcia ofert, do zbadania |
| ezamawiajacy.pl, logintrade.net | do zbadania | otwarcia ofert |
| API KRS (api-krs.ms.gov.pl) | otwarte | forma prawna, adres; dane osób MASKOWANE (np. "G*****"), więc nie nadaje się do łączenia firm przez osoby |
| MSiG (wyszukiwarka-msig.ms.gov.pl/api) | otwarte, bez klucza | ogłoszenia o wpisach do KRS z niemaskowanymi nazwiskami i PESEL zarządów (część 1b; PESEL tylko jako HMAC) |
| Biała lista VAT (wl-api.mf.gov.pl) | otwarte, 100 zapytań wyszukujących dziennie po 30 NIP | KRS, REGON, adres, rozpoznanie JDG, rachunki bankowe (wspólny rachunek lub adres oferentów = sygnał 3.2.11) |
| CEIDG | darmowy token | rozpoznanie JDG |
| decyzje UOKiK (uokik.gov.pl, decyzje.uokik.gov.pl) | publiczne PDF | zbiór wzorcowy; zawierają oferentów i ceny |
| wyroki KIO (orzeczenia.uzp.gov.pl) | publiczne | zbiór wzorcowy 2021+ (art. 108 ust. 1 pkt 5 Pzp) |

Udział platform w próbce 1429 ogłoszeń o wyniku (1, 2 i 5.10.2026): e-Zamówienia 36%,
platformazakupowa.pl 31%, ezamawiajacy.pl 17%, logintrade 5%, e-propublico 3%, reszta
rozproszona. BZP: ok. 500 ogłoszeń o wyniku dziennie.

## 4. Podział na części i kolejność

| # | Część | Wynik |
|---|---|---|
| 1 | Zbieranie: BZP 2021+ i otwarcia z e-Zamówień, KRS | baza uporządkowana, codzienny przyrost |
| 2 | Silnik: katalog OECD, testy, graf, decyzja Jev | tabele sygnałów |
| 3 | Walidacja na UOKiK i KIO | raport, kalibracja progów, bramka |
| 4 | Aplikacja webowa zmowy.pl | publiczna strona |
| 5 | Kolejne platformy: platformazakupowa.pl, ezamawiajacy.pl, logintrade | pokrycie ok. 90% rynku |

Każda część dostaje własny plan implementacji i issue na GitHubie. Część 4 może ruszyć
równolegle z częścią 3 na danych roboczych, ale publiczne uruchomienie wymaga przejścia
bramki z części 3.

## 5. Architektura

```
            BZP API     e-Zamówienia    platformy    KRS / CEIDG
               \             |              |            /
                v            v              v           v
        +--------------------------------------------------+
        | collector (Python, kontener z harmonogramem)      |
        |  kolejka zadań w Postgresie, limity tempa         |
        +--------------------------------------------------+
               |  surowe pliki (zstd)          | dane uporządkowane
               v                               v
        /data/raw (dysk)            Postgres: buyers, procedures, lots,
                                              bidders, bids, documents
                                               |
        +--------------------------------------------------+
        | engine (Python): rynki, testy OECD, graf Leiden,  |
        |  pakiety dowodów, decyzje Jev, opisy DeepSeek     |
        +--------------------------------------------------+
                                               |
                                    Postgres: markets, tests, clusters,
                                              signals, explanations
                                               |
        +--------------------------------------------------+
        | web (Next.js, SSR + cache), tylko odczyt          |
        +--------------------------------------------------+
                                               |
                                     Caddy -> zmowy.pl
```

Komponenty komunikują się wyłącznie przez Postgres (tabele i widoki). Collector nie zna
silnika, silnik nie zna aplikacji. Każdy komponent da się przeliczyć od nowa z warstwy
poniżej.

## 6. Model danych

Warstwa surowa: każda odpowiedź API i każdy plik zapisane bez zmian, skompresowane zstd,
z hashem SHA-256, URL i datą pobrania. Przeliczenie parserów nie wymaga ponownego pobierania.

Warstwa uporządkowana (najważniejsze kolumny):

- `buyers`: id, nip, regon, nazwa, typ zamawiającego, teryt, województwo.
- `procedures`: ocds_id (PK), numery BZP, buyer_id, tytuł, cpv_main, cpv_extra[], rodzaj
  (roboty/dostawy/usługi), tryb, wartość szacunkowa, platforma (kod), platform_url,
  termin ofert, czy poniżej progów UE, czy na platformie e-Zamówień.
- `lots`: procedure_id, nr części, cpv, wartość części, n_offers, n_rejected, price_min,
  price_max, price_winner, contract_value, contract_date, wynik (umowa/unieważnienie),
  winner_bidder_id, consortium (bool).
- `bidders`: id, nip (unikalny, jeśli znany), regon, krs, nazwa, forma prawna, is_jdg,
  adres, teryt siedziby, współrzędne siedziby.
- `bids`: lot_id, bidder_id, cena, is_winner, ranga, document_id, parse_confidence,
  match_confidence (pewność tożsamości oferenta).
- `documents`: id, procedure_id, źródło, nazwa, typ (otwarcie/wybór/unieważnienie/inne),
  ścieżka surowa, parser, status (nowy/odczytany/niezgodny/do_przeglądu), błędy.
- `krs_links`: bidder_id, osoba_hash, rola, źródło. Osoby trzymamy jako hash; nazwisk nie
  wyświetlamy.
- `jobs`: kolejka zadań (typ, klucz, status, próby, następna_próba, błąd).
- `ai_calls`: dostawca, model, zadanie, tokeny, koszt, czas, wynik skrócony.

Warstwa analityczna:

- `markets`: id, grupa CPV, region, okno czasowe, statystyki rozkładów.
- `test_results`: test_id (np. `3.3.6`), poziom (część/para/firma/rynek), klucz encji,
  wartość, percentyl w rynku, dowody (JSON).
- `clusters`: id, członkowie, rynek, metryki grafu.
- `signals`: id, cluster_id, siła (1-5), prawdopodobieństwo, punkty OECD z
  prawdopodobieństwami, status (opublikowany/przegląd/ukryty), wersja dowodów (hash).
- `explanations`: signal_id, wersja dowodów, tekst (szablon lub DeepSeek), źródło tekstu.
- `corrections`: rejestr korekt i odpowiedzi firm.

## 7. Zbieranie i parsowanie (część 1 i 5)

- Backfill BZP od 2021-01-01, dzień po dniu, typy: ogłoszenie o zamówieniu, o wyniku,
  o zmianie. Potem przyrost co noc. Zadanie wznawialne z kolejki.
- Dla postępowań z `isProceedingOnPlatform = true`: lista dokumentów, klasyfikacja
  dokumentów (nazwa pliku regułami, niejasne przypadki przez Jev `choice`), pobranie
  otwarcia ofert.
- Otwarty punkt 1: publiczny adres pobierania pliku dokumentu z e-Zamówień. Strona
  `mp-client/search/tenderdocument/{ocds}/{docId}` pozwala pobrać plik bez logowania;
  adres API ustalamy z ruchu sieciowego strony w pierwszym zadaniu części 1.
- Parsowanie w kolejności: (1) parser szablonu "Informacji z otwarcia" z e-Zamówień,
  (2) parser tabel z PDF/DOCX, (3) DeepSeek ze schematem odpowiedzi, wyłącznie na
  wyciętym fragmencie z tabelą ofert.
- Kontrola krzyżowa z BZP (obowiązkowa dla każdego odczytu): liczba ofert = `n_offers`,
  minimum i maksimum cen = `price_min`/`price_max` (tolerancja 0,01 zł), zwycięzca =
  zwycięzca z BZP. Odczyt niezgodny dostaje status `niezgodny` i nie wchodzi do analizy.
- Tożsamość firm: NIP, jeśli jest w dokumencie. Bez NIP: dopasowanie nazwy i adresu do
  znanych firm (BZP, KRS), kandydaci oceniani przez Jev `noul` ("ten sam wykonawca?").
  Poniżej progu pewności oferta zostaje bez `bidder_id` i nie tworzy krawędzi w grafie.
- Grzeczność: limit tempa per host, ponawianie z wydłużającą się przerwą, User-Agent
  "zmowy.pl (kontakt@zmowy.pl)". Platformy z ochroną anty-bot: wolne tempo, bez obchodzenia
  zabezpieczeń.
- Otwarty punkt 2: archiwum BZP sprzed 2021. Do zbadania w części 1; brak archiwum nie
  blokuje walidacji (sekcja 9).

## 8. Silnik (część 2)

### 8.1 Katalog OECD

Źródło: Wytyczne OECD dotyczące zwalczania zmów przetargowych w zamówieniach publicznych
(aktualizacja 2025), rozdział 3, "Lista dotycząca wykrywania zmów przetargowych".
Katalog w kodzie zawiera wszystkie 51 punktów; każdy ma status i jest pokazany w aplikacji
na stronie "Co sprawdzamy", także punkty niedostępne, z powodem.

Statusy: A = automatycznie, C = częściowo, N = niedostępne w danych publicznych.

3.1 Schematy składania ofert

| ID | Sygnał | Status | Implementacja / powód |
|---|---|---|---|
| 3.1.1 | Przetargi często wygrywa ten sam dostawca | A | udział wygranych w rynku i u zamawiającego |
| 3.1.2 | Podział terytorialny | A | wyłączność geograficzna wygranych i startów |
| 3.1.3 | Stali dostawcy nie startują, zostają podwykonawcami lub startują gdzie indziej | C | absencja na tle historii startów; podwykonawstwo tylko flaga z BZP |
| 3.1.4 | Wycofywanie ofert | N | wycofania przed terminem nie są publikowane |
| 3.1.5 | Zawsze startują, nigdy nie wygrywają | A | z list oferentów |
| 3.1.6 | Wygrywanie na zmianę | A | wskaźnik rotacji w parach i klastrach |
| 3.1.7 | Wspólna oferta firm, które wcześniej startowały osobno | A | konsorcja z BZP plus historia startów |
| 3.1.8 | Stała grupa składa oferty niekompletne lub niezgodne | C | liczba odrzuconych z BZP, imiennie z informacji o wyborze |

3.2 Dokumentacja przetargowa

| ID | Sygnał | Status | Implementacja / powód |
|---|---|---|---|
| 3.2.1 | Identyczne błędy w ofertach | N | treść ofert niepubliczna |
| 3.2.2 | Identyczny format, metadane, adres IP | N | j.w. |
| 3.2.3 | Ta sama osoba składa oferty konkurentów | N | j.w.; pośrednio 3.2.11 |
| 3.2.4 | Oferta używa danych innego oferenta (adres, e-mail, telefon) | C | wspólne dane kontaktowe z KRS, CEIDG i otwarć |
| 3.2.5 | Oferty niekompletne mimo wcześniej kompletnych | C | zmiana odsetka odrzuceń firmy w czasie |
| 3.2.6 | Identyczne szacunki kosztów lub warunki dodatkowe | C | identyczne wartości kryteriów pozacenowych, jeśli są w otwarciu |
| 3.2.7 | Podobne poprawki | N | treść ofert niepubliczna |
| 3.2.8 | Oferty mniej szczegółowe, nieautentyczne | N | j.w. |
| 3.2.9 | Oferty składane razem lub w krótkim odstępie | C | jeśli czas złożenia jest w raporcie z otwarcia |
| 3.2.10 | Stała kolejność składania ofert | C | j.w. |
| 3.2.11 | Ten sam adres lub siedziba, wspólne zobowiązania | A | adresy, wspólne osoby i wspólnicy w KRS; broker niedostępny |

3.3 Ceny

| ID | Sygnał | Status | Implementacja / powód |
|---|---|---|---|
| 3.3.1 | Nagłe lub identyczne podwyżki | C | indeks cen względem szacunku w rynku, skoki wspólne dla grupy |
| 3.3.2 | Oczekiwane obniżki znikają | C | j.w., odchylenie od trendu rynku |
| 3.3.3 | Obniżki mniejsze niż typowe | A | stosunek ceny zwycięskiej do szacunku na tle rynku |
| 3.3.4 | Ceny niezmienne mimo zmian rynku | C | stabilność cen firmy przy zmienności rynku |
| 3.3.5 | Identyczne ceny, choć wcześniej różne | A | duplikaty cen w części i w czasie |
| 3.3.6 | Regularne różnice cen, wzór matematyczny | A | stałość stosunków cen par oferentów, współczynnik zmienności stosunków |
| 3.3.7 | Duża luka zwycięzca vs reszta, przegrani zbliżeni | A | odległość względna, rozrzut cen przegranych |
| 3.3.8 | Oferta firmy dużo wyższa niż w podobnym zamówieniu | A | podobieństwo zamówień: embeddingi plus Jev `noul` |
| 3.3.9 | Zwycięska oferta znacznie ponad szacunek | A | cena zwycięska / wartość szacunkowa |
| 3.3.10 | Spadek cen po wejściu nowego oferenta | A | porównanie przed/po wejściu, względem szacunku |
| 3.3.11 | Firma lokalna drożej u siebie niż daleko | A | odległość siedziba-zamawiający, regresja ceny |
| 3.3.12 | Podobne koszty transportu | N | brak danych o kosztach |
| 3.3.13 | Jeden oferent pyta hurtowników o ceny | N | brak danych |

3.4 Podejrzane wypowiedzi: punkty 3.4.1-3.4.9 (porozumienie, "ceny branżowe", "nasz
obszar", "teren należy do", niepubliczna wiedza o ofertach, oferty pozorne, ta sama
terminologia, obawy wobec oświadczeń o niezależności, dyskusje w stowarzyszeniu):
wszystkie N, bo wypowiedzi nie są w danych publicznych.

3.5 Podejrzane zachowania

| ID | Sygnał | Status | Implementacja / powód |
|---|---|---|---|
| 3.5.1 | Spotkania przed złożeniem ofert | N | brak danych |
| 3.5.2 | Regularne kontakty towarzyskie | N | brak danych |
| 3.5.3 | Prośba o dokumenty dla siebie i konkurenta | N | brak danych |
| 3.5.4 | Składa swoją ofertę i ofertę konkurenta | N | brak danych |
| 3.5.5 | Oferta firmy niezdolnej do realizacji | C | młoda firma, brak historii, kapitał vs wartość zamówienia |
| 3.5.6 | Próby ustalenia, kto startuje | N | brak danych |
| 3.5.7 | Podobne zapytania do zamawiającego | N | pytania publikowane anonimowo |
| 3.5.8 | Ci sami konsultanci | N | brak danych |
| 3.5.9 | Zwycięzca zleca prace przegranym | N | BZP podaje tylko flagę podwykonawstwa |
| 3.5.10 | Zwycięzca nie podejmuje realizacji, potem podwykonawca | C | wybór kolejnej oferty po uchyleniu się zwycięzcy |

Podsumowanie: 14 A, 12 C, 25 N.

### 8.2 Rynki

Rynek = grupa CPV (5 cyfr, z łączeniem rzadkich grup do 3 cyfr) x województwo x okno
36 miesięcy. Każdy test porównujemy z rozkładem w tym samym rynku (percentyl), nigdy z
całą Polską. Rynki poniżej 30 części łączymy z rynkiem nadrzędnym.

### 8.3 Testy i graf

- Testy z katalogu liczone na poziomach: część, para firm, firma, rynek w czasie.
  Testy cenowe części obejmują klasyczne miary z literatury (rozrzut cen, odległość
  względna, skośność, kurtoza) i są mapowane na punkty 3.3.
- Graf: firmy jako węzły, krawędzie ważone liczbą wspólnych startów w rynku. Klastry
  algorytmem Leiden (igraph). Klaster to kandydat na kartel; wyniki testów agregujemy
  na klaster.
- Pakiet dowodów klastra: JSON z wynikami testów, percentylami, liczbami przetargów,
  członkami (NIP), rynkiem. Hash pakietu = wersja dowodów.

### 8.4 Decyzja

- `DecisionProvider` (interfejs): `decide(state, questions) -> answers`. Implementacje:
  Jev (domyślna), Cloudflare Clef-flash (Workers AI), OpenAI Decisions API. Wybór w
  konfiguracji.
- Pytania dla klastra: `noul` dla każdego punktu A/C ("czy dowody wskazują na 3.3.6?")
  oraz `score` "siła sygnału" (5 poziomów). Arytmetyka zostaje w kodzie; model dostaje
  gotowe liczby i percentyle (zgodnie z ograniczeniami Jev 1.13).
- Progi publikacji ustalane na zbiorze wzorcowym (sekcja 9): publikacja, przegląd,
  ukrycie. Wersja modelu przypięta (np. `jev-1.13.0`), nie `jev-latest`.
- Wariant zapasowy bez modelu: regresja logistyczna na percentylach testów, kalibrowana
  na zbiorze wzorcowym.
- Kształt API Jev (endpoint, schemat) weryfikujemy w oficjalnej dokumentacji
  docs.typesafe.ai na starcie części 2.

### 8.5 Opisy

- Wszystkie sygnały dostają opis z szablonów (deterministyczne zdania, liczby z bazy).
- Opis DeepSeek tylko dla sygnałów opublikowanych, generowany przy pierwszym wyświetleniu
  i zapisywany; ponownie tylko po zmianie wersji dowodów. Model nie wpisuje liczb:
  wstawia odwołania, które aplikacja renderuje z bazy.

### 8.6 Rytm

Codzienne przeliczenie przyrostowe (rynki dotknięte nowymi danymi), pełne co tydzień.

## 9. Walidacja (część 3)

Zbiór wzorcowy:

1. Decyzje UOKiK w sprawie zmów przetargowych (wszystkie dostępne, także archiwalne),
   zamienione jednorazowo przez DeepSeek na dane strukturalne: zamawiający, rok,
   przedmiot, oferenci z cenami, uczestnicy zmowy. Pierwsze cztery pobrane:
   RGD-10/2023, RGD-11/2023, RGD-12/2023 (remonty, Wielkopolska), RLU-7/2023 (dowóz
   dzieci, Lubelszczyzna); przetargi z lat 2019-2020.
2. Wyroki KIO z art. 108 ust. 1 pkt 5 Pzp (wykluczenie za zmowę), przetargi 2021-2026.

Testy:

- A. Testy na ofertach z decyzji, w kontekście rynku: czy przetargi i pary ze zmowy są w
  najwyższych 5% ryzyka swojego rynku.
- B. Pełny pipeline na naszych danych dla spraw 2021+: czy wskazane firmy są w
  opublikowanych klastrach. Test wyprzedzenia: tylko dane sprzed daty decyzji.
- C. Fałszywe alarmy: grupa kontrolna losowych firm z tych samych rynków oraz ręczny
  przegląd 50 najmocniejszych sygnałów wykonany przez Claude (teczka na sygnał, ocena i
  uzasadnienie zapisane w repo).

Bramka: test A poniżej 60% = poprawa silnika przed publicznym uruchomieniem.
Zbiór wzorcowy służy też do kalibracji progów decyzji. Wyniki, także porażki, idą na
stronę "Sprawdziliśmy się".

## 10. Aplikacja (część 4)

Kierunek wizualny: dużo światła i powietrza, tryb jasny i ciemny, jeden kolor akcentu
ryzyka (bursztyn do czerwieni), neutralne szarości, duża typografia (Inter lub Geist),
liczby jako bohater, krótkie zdania zamiast tabel, subtelny ruch przy przewijaniu,
projekt od telefonu w górę. Bez wzorowania się na z-dykty.pl.

Ekrany:

1. Start: zdanie z żywymi liczbami, wyszukiwarka (firma, NIP, gmina), karty sygnałów
   (tytuł prostym językiem, 3 fakty, mini-wykres, pewność w procentach, znaczniki OECD),
   filtry jako chipy (branża, region, wartość, siła).
2. Sygnał: opowieść przewijana; "Co widzimy" (każde ustalenie = punkt OECD + wizualizacja:
   oś rotacji, drabinka cen, mapa podziału, graf współoferentów), "Dowody" (przetargi z
   linkami do źródeł), "Inne możliwe wyjaśnienia", "Co możesz zrobić" (zamawiający: art.
   108 Pzp; obywatel: uokik.whiblo.pl; dziennikarz: teczka PDF/CSV), prawo do odpowiedzi.
3. Firma: historia startów, wygrane/przegrane, partnerzy, sygnały, link do KRS. JDG tylko
   NIP.
4. Zamawiający: poziom konkurencji, sygnały w jego przetargach.
5. Mapa i rynki: natężenie ryzyka w regionach i branżach.
6. Jak to działa: pionowa oś 8 kroków (zbieramy, czytamy, rozpoznajemy firmy, łączymy w
   sieć, testujemy, ważymy dowody, sprawdzamy się na kartelach, publikujemy i dajemy prawo
   do odpowiedzi), animowana przy przewijaniu, z żywymi liczbami z bazy i jednym prawdziwym
   przetargiem prowadzonym przez wszystkie kroki. Podstrony: "Co sprawdzamy" (katalog
   OECD), "Sprawdziliśmy się" (walidacja), "Źródła".
7. Dla zamawiających: "Sprawdź oferentów przed wyborem" (numer postępowania -> raport).
   Wersja 1 bez kont i płatności.

Technika: Next.js (SSR, cache stron), visx, Sigma.js, MapLibre z uproszczonymi granicami
powiatów, automatyczne grafiki do udostępniania. Stała ramka "To sygnał statystyczny, nie
zarzut". Przed kodem: klikalne makiety 3 ekranów (start, sygnał, jak to działa) do
akceptacji.

## 11. Warstwa AI i koszty

| Zadanie | Dostawca | Szacunek dla całego archiwum |
|---|---|---|
| Decyzje o klastrach, tożsamość firm, klasyfikacja dokumentów, podobieństwo | Jev (0,042 USD / 1 mln tokenów wejścia) | ok. 8 USD |
| Odczyt nietypowych PDF (ok. 10% dokumentów, tylko fragment z tabelą) | DeepSeek Flash | ok. 5-15 USD |
| Opisy opublikowanych sygnałów (1-2 tys.) | DeepSeek Flash | ok. 2-10 USD |
| Embeddingi do wstępnego podobieństwa | lokalnie na CPU (mały model wielojęzyczny, do wyboru w części 2), limit CPU | 0 USD |

`TextProvider` (interfejs): DeepSeek (domyślnie), Claude (zapas). Każde zadanie ma limit
budżetu; każde wywołanie zapisuje koszt w `ai_calls`. Ceny DeepSeek do potwierdzenia w
oficjalnym cenniku. Lokalne LLM na głównym boxie odrzucone: brak GPU (GT 710), CPU Ryzen 5
3600 współdzielony z produkcją.

## 12. Infrastruktura, deploy, testy

- Dwa repozytoria. Backend: publiczne `tender-integrity-engine` (TIE; `collector/`, `engine/`, walidacja, `infra/`, `docs/`), do przekazania instytucjom do samodzielnej analizy. Frontend zmowy.pl: osobne repo (Next.js), tylko prezentacja, czyta z widoków tylko do odczytu w bazie TIE.
- Deploy: GitHub Actions, self-hosted runner na głównym boxie, `/var/www/zmowy/`,
  `docker-compose.yml` + `docker-compose.prod.yml`, sieć `zoyeai_sail`, blok Caddy,
  checklista z `/root/SERVER.md` sekcja 7 (w tym weryfikacja backupu).
- Limity: workery `cpus: 4`, `mem_limit: 8g`; zadania nocne o 01:00 i 04:30 (backup o
  03:00).
- Dysk: surowe pliki zstd w `/var/www/zmowy/data/raw`; JSON BZP wyłączony z backupu
  (do ponownego pobrania), PDF z platform w backupie.
- Sekrety (Jev, DeepSeek, CEIDG) tylko w `.env` na serwerze, nigdy w repo.
- Testy w CI na GitHubie: parsery na prawdziwych PDF; testy OECD na syntetycznych rynkach
  z wstrzykniętą zmową; raport walidacji; testy dymne Playwright dla aplikacji. Nic nie
  uruchamiamy na laptopie właściciela.
- Domena zmowy.pl: zakup i DNS w Hostingerze po decyzji właściciela (wg indeksu wolna,
  do potwierdzenia).

## 13. Prawo i etyka

- Każda strona: "To sygnał statystyczny, nie zarzut".
- JDG: tylko NIP. Osób z KRS nie wyświetlamy z nazwiska ("wspólna osoba w zarządzie" +
  link do KRS).
- Formularz prawa do odpowiedzi, publiczny rejestr korekt, szybka ścieżka usunięcia
  błędnego sygnału.
- Pełna metodologia i katalog OECD publicznie.
- Pobieranie danych grzeczne, bez obchodzenia zabezpieczeń.
- Podstawa prawna wartości dla zamawiających: Zawiadomienie KE 2021/C 91/01 i art. 108
  ust. 1 pkt 5 Pzp (numer punktu do potwierdzenia w aktualnym tekście ustawy).

## 14. Ryzyka i otwarte punkty

1. Adres pobierania dokumentów z e-Zamówień (pierwsze zadanie części 1).
2. Anty-bot na platformazakupowa.pl i innych: wolne tempo; jeśli blokują, pokrycie
   tylko e-Zamówień do czasu rozwiązania.
3. Archiwum BZP sprzed 2021.
4. Liczba wyroków KIO z art. 108 ust. 1 pkt 5 (do sprawdzenia w bazie).
5. Jev w wersji 1.13: słabszy w arytmetyce i przy nieistotnym kontekście; liczby
   liczymy w kodzie, do modelu idzie zwięzły pakiet.
6. Fałszywe alarmy na rynkach jednodostawcowych: porównanie w obrębie rynku i progi z
   kalibracji.
7. Klucz Jev był wklejony w czacie: zapisać tylko w `.env` na serwerze, zrotować po
   hackathonie.
