"""Curated UOKiK decisions on bid rigging (zmowa przetargowa) for the validation gold set.

Every entry is a decision of the President of UOKiK finding an agreement under art. 6 ust. 1
pkt 7 of the Polish competition act (uzgadnianie warunków ofert przez przedsiębiorców
przystępujących do przetargu). Sources: the press release "Decyzje Prezesa UOKiK w sprawie
zmów przetargowych" (uokik.gov.pl, Download/592..595) and the decision database
decyzje.uokik.gov.pl, section "Porozumienia ograniczające konkurencję", years 2015-2026.

`verified=True` means that on VERIFIED_ON the URL answered HTTP 200 with
`Content-Type: application/pdf`, the PDF has a text layer, and its operative part cites
art. 6 ust. 1 pkt 7 (checked by reading the first pages). The PDFs are fetched at runtime
(`tie gold-extract`) and are not stored in the repository.

`year` is the year of the decision; the tenders it describes are usually 1-5 years older.
`region` is the voivodeship of the tenders ("krajowy" for nationwide buyers).
"""

from __future__ import annotations

from dataclasses import dataclass

VERIFIED_ON = "2026-10-12"

_DB = "https://decyzje.uokik.gov.pl/bp/dec_prez.nsf/04b7bebd77e0d447c1257ec6004788f2"


def _u(unid: str, file_name: str) -> str:
    """Attachment URL in the Domino decision database (file name already URL-encoded)."""
    return f"{_DB}/{unid}/$FILE/{file_name}"


@dataclass(frozen=True)
class Decision:
    id: str
    url: str
    year: int
    sector: str
    region: str
    note: str
    verified: bool = True


UOKIK_DECISIONS: tuple[Decision, ...] = (
    Decision(
        "RLU-3/2026",
        _u("5bd6f80e48f3f919c1258e0b003d5670",
           "RLU.2.410.2.2025%20Hortus%20Gitpol%20zmowa%20przetargowa%20wersja%20jawna.pdf"),
        2026, "utrzymanie zieleni", "lubelskie",
        "Hortus (Lublin) i Gitpol (Skierniewice): konserwacja zieleni komunalnej w Lublinie."),
    Decision(
        "RBG-7/2025",
        _u("aa5ba024d3e2f42ac1258e30001bccba",
           "decyzja%20nr%20RBG%20-%207_2025%20wersja%20jawna.pdf"),
        2025, "dowóz uczniów", "warmińsko-mazurskie",
        "Grupa Faster (Olsztyn, Górowo Iławeckie) i Radex: dowóz uczniów, przetargi gmin od"
        " 2019 r.; część nazw zaczerniona w wersji jawnej."),
    Decision(
        "RKT-4/2025",
        _u("f05bcd076473fb0cc1258d400040bfd5",
           "Decyzja%20nr%20RKT-4-2025%20w%20spr.%20%20Anny%20Gre%C5%84%20i%20Krzysztofa%20"
           "Gre%C5%84%20Firma%20Gre%C5%84%20s.c.%20oraz%20Grzegorza%20Holeksy%20-%20praktyki"
           "%20ograniczaj%C4%85ce%20konkurencj%C4%99%20wersja%20jawna%20(002).pdf"),
        2025, "usługi leśne", "śląskie",
        "Firma Greń s.c. i Grzegorz Holeksa: Nadleśnictwo Ustroń 2023, część 13 (leśnictwo"
        " Czantoria)."),
    Decision(
        "RKR-5/2024",
        _u("880c6b638a51dc93c1258c2f0030a096", "58004326.pdf"),
        2024, "drogi", "pomorskie",
        "IMKOP i Jacek Machuta (Ciecholewy): budowa, naprawa i konserwacja dróg."),
    Decision(
        "RPZ-16/2024",
        _u("4cab95bca89e97f3c1258bfa00748d19",
           "Decyzja%20nr%20RPZ%2016-2024%20z%2013.12.2024%20-%20Nowicki%20Walczykowski%20-%20"
           "antymonopol.pdf"),
        2024, "tereny zieleni", "wielkopolskie",
        "Zielona Przestrzeń i Aleks (Turek): zagospodarowanie terenów zieleni dla samorządów"
        " środkowo-zachodniej Polski."),
    Decision(
        "RGD-6/2024",
        _u("efc067ab0d664459c1258c0d006048c7",
           "RGD-410-1-2022%20zmowa%20przetrgowa%20%20S-PROBUD%20-%20S-PROBUD%20Logistic%20-%20"
           "decyzja%20RGD-6-2024%20wersja%20bip.pdf"),
        2024, "drogi", "zachodniopomorskie",
        "S-Probud i S-Probud Logistic (Stargard): remonty dróg, spółki powiązane składające"
        " oferty."),
    Decision(
        "RWR-5/2024",
        _u("68acc53ae14660aec1258bc6002edfeb",
           "Decyzja%20RWR-5_2024_(wersja%20jawna).pdf"),
        2024, "serwis drukarek", "dolnośląskie",
        "Mydir.pl (Kalisz) i Print-Sol (Wrocław): serwis pogwarancyjny sprzętu drukującego"
        " dla ZUS Oddział Wrocław, 2021."),
    Decision(
        "RGD-12/2023",
        _u("1e56a3bcd2228cfdc1258add004d65d3", "34549743.pdf"),
        2023, "remonty i budowy", "wielkopolskie",
        "Antczak SRH, Tech-Fen i Kamastra (Kalisz): roboty remontowo-budowlane, rezygnacja"
        " zwycięzcy na rzecz droższej oferty; uokik.gov.pl/Download/593."),
    Decision(
        "RGD-11/2023",
        _u("9178e9f351bf4882c1258add004e5518", "79715670.pdf"),
        2023, "remonty i budowy", "wielkopolskie",
        "Antczak SRH i Kamastra (Kalisz): roboty remontowo-budowlane dla szkół i innych"
        " podmiotów publicznych; uokik.gov.pl/Download/594."),
    Decision(
        "RGD-10/2023",
        _u("50f8f8a8dfd3b4aec1258add004e9642", "86484299.pdf"),
        2023, "remonty i budowy", "wielkopolskie",
        "Antczak SRH i Tech-Fen: roboty remontowo-budowlane, nieuzupełnianie dokumentów przez"
        " zwycięzcę; uokik.gov.pl/Download/592."),
    Decision(
        "RLU-7/2023",
        _u("0c45d020c639c267c1258afa003140e1", "88719021.pdf"),
        2023, "dowóz uczniów", "lubelskie",
        "Skiba, Tacho-System Marek Pilip i SZT Siedliska: dowóz dzieci do szkół 2019-2020,"
        " wycofanie tańszej oferty; uokik.gov.pl/Download/595."),
    Decision(
        "RLU-5/2023",
        _u("c1ef138ce9394389c1258aac003b7a67", "66908262.pdf"),
        2023, "remonty i budowy", "lubelskie",
        "Media-Bud i PG Piotr Gutek (Lublin): przebudowa budynku Szpitala"
        " Neuropsychiatrycznego w Lublinie."),
    Decision(
        "RGD-3/2023",
        _u("e95c1f99e61b1a2cc12589fe00335c9f",
           "RGD-410.2.2020%20decyzja%20z%2024%20maja%202023%20zmowa%20przetrgowa%20"
           "Czekiery%C5%82o%20-W%C5%82odarczyk%20-%20Maksym%20-%20Maksym%20-%20wersja%20bip"
           "%20(002).pdf"),
        2023, "usługi leśne", "pomorskie",
        "Leśnik-T, Włodarczyk i Maksym: Nadleśnictwo Niedźwiady, pakiet VIII (leśnictwa"
        " Rudka i Głuszec)."),
    Decision(
        "RKR-3/2022",
        _u("8d19f773852b2e06c125894f0041f5b8",
           "DECYZJA%20-%20ADAM%20TOURS%20i%20ROAL%20-%20wersja%20jawna.pdf"),
        2022, "dowóz uczniów", "małopolskie",
        "Adam-Tours i Roal (Kraków): dowóz uczniów, dzieci niepełnosprawnych i komunikacja"
        " zastępcza w małopolskim i podkarpackim."),
    Decision(
        "RGD-12/2022",
        _u("7fde8b2818c278dcc12589650046105b",
           "RGD-410.1.2020%20decyzja%20zmowa%20przetrgowa%20Czekiery%C5%82o%20-Czekiery"
           "%C5%82o%20-%20Stasiak%20-%20wersja%20BIP%20(2).pdf"),
        2022, "usługi leśne", "pomorskie",
        "Czekieryło, Leśnik-T i Trans-Mar: Nadleśnictwo Niedźwiady, pakiet II (leśnictwa"
        " Pustowo i Kamionka)."),
    Decision(
        "RPZ-12/2022",
        _u("343c0e63571bcd89c125894e0034aee0",
           "decyzja%20nr%20RPZ%2012-2022%20z%2028.12.2022%20r.%20%20WERSJA%20JAWNA.pdf"),
        2022, "odpady komunalne", "wielkopolskie",
        "Remondis Sanitech, PreZero, PUK Zys, Eko-Tom i inni: odbiór odpadów dla związku"
        " GOAP (Poznań), przetargi 2017."),
    Decision(
        "RWR-1/2022",
        _u("2e70b2e78f796274c12588a10037695a", "Decyzja%20RWR-1_2022%20(BIP).pdf"),
        2022, "wycinka drzew", "lubuskie",
        "Zwyż-Dźwig s.c. (Żagań) i Maciej Berger: wycinka i pielęgnacja drzew dla ZDW Zielona"
        " Góra i 43 WOG Świętoszów, 2016-2019."),
    Decision(
        "RWR-5/2021",
        _u("243be48199dd2f2dc12587d700490802", "RWR-5_2021.pdf"),
        2021, "wycinka drzew", "lubuskie",
        "Bożysława Woś Zwyż-Dźwig i inni: wycinka, nasadzenia i pielęgnacja drzew dla"
        " Zarządu Dróg Wojewódzkich, 2018-2020."),
    Decision(
        "RKT-7/2021",
        _u("8df14943b01ce25ec12587d700378e0a",
           "Decyzja%20nr%20RKT-7-2021%20z%2023.12.2021%20r.%20Dariusz%20Jojko%20i%20Miros"
           "%C5%82aw%20Mikosz%20antymonopol%20wersja%20jawna.pdf"),
        2021, "przewóz osób niepełnosprawnych", "śląskie",
        "Dariusz Jojko i Mirosław Mikosz (Chorzów): przewóz dzieci niepełnosprawnych dla Gminy"
        " Siemianowice Śląskie."),
    Decision(
        "RKT-5/2021",
        _u("0930c43e0bd545bec12587b200394689",
           "Decyzja%20nr%20RKT-5_2021%20z%2030.11.2021%20MASTER%20i%20inni%20-%20antymonop"
           "%20-%20wersja%20jawna.pdf"),
        2021, "utrzymanie dróg", "śląskie",
        "Master Odpady i Energia, AN-EKO i inni: zimowe utrzymanie dróg w Tychach, przetarg"
        " 1/DDT/27/15 (2015)."),
    Decision(
        "RGD-6/2021",
        _u("a604768881b0d5d8c1258806003623e7", "decyzja%20RGD%206.2021%20-publ2.pdf"),
        2021, "odpady", "pomorskie",
        "Aqua-Expres i Mateusz Milczarek (Czarne): odbiór odpadów kuchennych dla 16 Wojskowego"
        " Oddziału Gospodarczego."),
    Decision(
        "RPZ-4/2021",
        _u("fa0714d9e0d24fa2c12587450025f9bf",
           "Decyzja%20nr%20RPZ%204-2021%20z%2030.07.2021.pdf"),
        2021, "drogi", "lubuskie",
        "Uniwro i Józef Łucki (Nowa Sól): dostawy i usługi przy budowie i utrzymaniu dróg."),
    Decision(
        "RKR-11/2020",
        _u("2209f46255216835c125866f0044530c",
           "Decyzja%20RKR-11_2020%20BRIMAT%20%26%20JS%20Janusz%20Kurek%20-%20wersja%20jawna"
           ".pdf"),
        2020, "roboty budowlane", "podkarpackie",
        "Brimat i J&S Janusz Kurek: ścianki aluminiowe, przetarg Gminy Tarnobrzeg 2014,"
        " zadanie 1."),
    Decision(
        "RLU-5/2020",
        _u("66fe53a553a3f0b1c1258665002db14d", "Decyzja%20RLU%205.2020%20wersja%20jawna.pdf"),
        2020, "remonty i budowy", "lubelskie",
        "Budpol/Bioforma Piotr Ciupa i PHU Magnum (Siedlce): roboty remontowo-budowlane"
        " w lubelskim i mazowieckim."),
    Decision(
        "DOK-2/2020",
        _u("eb61fb338e142895c12585ea001e730e",
           "DOK1.410.1.2015_04.09.2020_Decyzja_wersja%20jawna.pdf"),
        2020, "podkłady kolejowe", "krajowy",
        "Nasycalnia Podkładów, ThyssenKrupp GFT, Track Tec i inni: dostawa podkładów dla"
        " PKP PLK 2014-2015."),
    Decision(
        "RKR-3/2020",
        _u("a2725a7ede01e141c12585d900324ae2", "decyzja%20RKR-3_2020%20wersja%20jawna.pdf"),
        2020, "maszyny rolnicze", "krajowy",
        "Agromotors (Wojciech i Paweł Płoski, Piotrkowo): dostawy maszyn rolniczych dla"
        " podmiotów instytucjonalnych 2017-2018."),
    Decision(
        "RKR-1/2020",
        _u("a3cf88c7c8637e2fc12585f8003903ca", "RKR-1_2020%20wersja%20BIP%20(003).pdf"),
        2020, "dowóz uczniów", "małopolskie",
        "Firmy rodziny Jakubas (Euro-Bus i inne): dowóz uczniów do szkół w gminach"
        " małopolskich."),
    Decision(
        "RGD-12/2019",
        _u("bab4ef6d52c88fb3c125851300341a2c",
           "RGD-410-2-18-IW%20-%20zmowa%20przetargowa%20-%20wersja%20bip%20(2).pdf"),
        2019, "dostawy warzyw", "pomorskie",
        "Hurtowe dostawy warzyw dla podmiotów publicznych; nazwy stron zanonimizowane w bazie"
        " decyzji."),
    Decision(
        "RŁO-7/2019",
        _u("4679c274c4cc8785c12584f5003ec38d",
           "Decyzja%20nr%20R%C5%81O%207%202019%20z%209.12.2019%20-%20Aglet%20i%20Cie%C5%9Bla"
           "%20antymonopl%20decyzja%20(wersja%20jawna).pdf"),
        2019, "drogi", "łódzkie",
        "Star Budowa Karol Cieśla i Aglet: budowa i remonty dróg dla samorządów w łódzkim,"
        " świętokrzyskim i mazowieckim."),
    Decision(
        "RPZ-8/2019",
        _u("12aa893f56134545c1258482002ab804",
           "Decyzja%20nr%20RPZ%208-2019%20z%2030.08.2019%20-%20Kubiaczyk%20-%20Kubiaczyk%20-%20"
           "porozumienie%20ograniczaj%C4%85ce%20konkurencj%C4%99.pdf"),
        2019, "usługi kurierskie", "wielkopolskie",
        "AK Trans Aneta Kubiaczyk i Jacek Kubiaczyk (Konin): doręczanie paczek dla Poczty"
        " Polskiej, postępowanie PI.TPZPoz.2600.129.2017."),
    Decision(
        "RWR-1/2019",
        _u("e8477375edef3dcbc1258463003704f5", "RWR%2001-2019%20-%20BIP%20-%20ALIC%20i%20TYLDA.pdf"),
        2019, "informatyka", "dolnośląskie",
        "Alic (Zielona Góra) i Tylda (Przeworsk): oprogramowanie i usługi IT dla Województwa"
        " Dolnośląskiego, 2016."),
    Decision(
        "RKT-1/2019",
        _u("d0902837df1690bfc12584470028da40",
           "Decyzja%20nr%20RKT-01_2019%20z%202.06.2019%20Soltur%20Sp.%20z%20o.o.%20i%20Trans-Jan"
           "%20Sp.%20%20o.o.%20-%20porozumienie.pdf"),
        2019, "transport towarów", "śląskie",
        "Soltur i Trans-Jan: przewóz towarów dla KHW KWK Murcki-Staszic, przetarg"
        " ZP/005/2016."),
    Decision(
        "DOK-1/2019",
        _u("5f8260cc8efbc1d3c12583bb003ec082",
           "2019_03_12_DOK1-410-1-17_decyzja%20wersja%20jawna.pdf"),
        2019, "przewóz osób niepełnosprawnych", "mazowieckie",
        "Tom Transport, Gama, Jacek Grzelak, Sabak i inni: przewóz dzieci niepełnosprawnych"
        " w Warszawie."),
    Decision(
        "RKR-1/2019",
        _u("d54fd1e7d9c63e01c12583c1003fa315", "S%C5%82ota-S%C5%82ota%20wersja%20jawna.pdf"),
        2019, "wodociągi i kanalizacja", "podkarpackie",
        "Arster i Wodo-Gaz (Słota, Przeworsk): kanalizacja sanitarna dla Miasta i Gminy"
        " Kańczuga, 2017."),
    Decision(
        "RKR-3/2018",
        _u("24c4fa92db91263ac1258330003851ef", "DECYZJA_Nr_RKR-3_2018_wersja%20jawna.pdf"),
        2018, "hosting serwerów", "krajowy",
        "Infomex, Nabino i ncNETcom: hosting i kolokacja serwerów w zamówieniach publicznych."),
    Decision(
        "RGD-5/2017",
        _u("d40cf11c97295f6ac12582b300494391",
           "RGD_Decyzja%205-2017_z%20dnia%2017%20-%2010-17%20porozumienie%20(KUS_WNUK).%20"
           "antymonopolowa.pdf"),
        2017, "remonty i budowy", "zachodniopomorskie",
        "Dar-Bud Dariusz Kuś i Bud-Max Marzena Wnuk: remonty dla 21 Bazy Lotnictwa"
        " Taktycznego (Świdwin), 2016."),
    Decision(
        "RKT-4/2017",
        _u("2417c7af9961b45ec125813d0047ca0a",
           "Decyzja%20nr%20RKT-04_2017%20z%2019.04.2017%20r.%20Bernard%20Czok%20i%20Andrzej"
           "%20Mamczy%C5%84ski%20-%20porozumienie.pdf"),
        2017, "holowanie pojazdów", "opolskie",
        "Auto Czok i P.W. Warm: holowanie i parking pojazdów dla Starostwa Opolskiego i KWP"
        " w Opolu."),
    Decision(
        "RKR-10/2016",
        _u("147bd237f4834fdfc12580b3004144e4", "RKR-10_2016.pdf"),
        2016, "dostawy pieczywa", "mazowieckie",
        "Piekarnie rodziny Dziewit (Tomex i inne): dostawy pieczywa dla aresztów śledczych"
        " w Warszawie."),
    Decision(
        "RKT-19/2015",
        _u("2228941629d52349c1257f5d005a0f45",
           "Decyzja%20RKT-19-2015%20z%2028.12.2015%20Kuchtyn,%20Marynowski%20porozumienie.pdf"),
        2015, "utrzymanie dróg", "opolskie",
        "PHU Andrew Andrzej Kuchtyn i Marynowski: koszenie traw przy drogach krajowych dla"
        " GDDKiA Oddział Opole."),
    Decision(
        "RKR-4/2024",
        _u("d5aed4b862fd23f5c1258be3005fc3c4", "Decyzja%20RKR_4_2024%20wersja%20jawna.pdf"),
        2024, "opracowania kartograficzne", "krajowy",
        "Geokart-International i Okręgowe Przedsiębiorstwa Geodezyjno-Kartograficzne:"
        " opracowania kartograficzne dla Służby Geodezyjnej i Kartograficznej."),
)

_BY_ID = {d.id: d for d in UOKIK_DECISIONS}


def get_decision(decision_id: str) -> Decision:
    """Decision by its number, for example "RLU-7/2023". Raises KeyError when unknown."""
    return _BY_ID[decision_id.strip()]
