"""Katalog 51 punktów listy OECD do wykrywania zmów przetargowych.

Źródło: Wytyczne OECD dotyczące zwalczania zmów przetargowych w zamówieniach publicznych
(aktualizacja 2025), rozdział 3. Treść przepisana z sekcji 8.1 speca
(`docs/superpowers/specs/2026-10-12-zmowy-design.md`): tytuł po polsku, status
A = automatycznie, C = częściowo, N = niedostępne w danych publicznych, oraz `how`, czyli
jak punkt sprawdzamy albo dlaczego nie możemy. Aplikacja pokazuje wszystkie punkty na stronie
"Co sprawdzamy" (widok `api.oecd_catalog`), także niedostępne.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class OecdItem:
    item: str
    section: str
    title: str
    status: str  # "A" | "C" | "N"
    how: str


SECTIONS: dict[str, str] = {
    "3.1": "Schematy składania ofert",
    "3.2": "Dokumentacja przetargowa",
    "3.3": "Ceny",
    "3.4": "Podejrzane wypowiedzi",
    "3.5": "Podejrzane zachowania",
}

_SPEECH = "wypowiedzi nie są w danych publicznych"

_ROWS: list[tuple[str, str, str, str]] = [
    # 3.1 Schematy składania ofert
    ("3.1.1", "Przetargi często wygrywa ten sam dostawca", "A",
     "udział wygranych w rynku i u zamawiającego"),
    ("3.1.2", "Podział terytorialny", "A", "wyłączność geograficzna wygranych i startów"),
    ("3.1.3", "Stali dostawcy nie startują, zostają podwykonawcami lub startują gdzie indziej",
     "C", "absencja na tle historii startów; podwykonawstwo tylko flaga z BZP"),
    ("3.1.4", "Wycofywanie ofert", "N", "wycofania przed terminem nie są publikowane"),
    ("3.1.5", "Zawsze startują, nigdy nie wygrywają", "A", "z list oferentów"),
    ("3.1.6", "Wygrywanie na zmianę", "A", "wskaźnik rotacji w parach i klastrach"),
    ("3.1.7", "Wspólna oferta firm, które wcześniej startowały osobno", "A",
     "konsorcja z BZP plus historia startów"),
    ("3.1.8", "Stała grupa składa oferty niekompletne lub niezgodne", "C",
     "liczba odrzuconych z BZP, imiennie z informacji o wyborze"),
    # 3.2 Dokumentacja przetargowa
    ("3.2.1", "Identyczne błędy w ofertach", "N", "treść ofert niepubliczna"),
    ("3.2.2", "Identyczny format, metadane, adres IP", "N",
     "Pliki ofert nie są publiczne, więc nie da się porównać ich formatu, metadanych"
     " ani adresów IP."),
    ("3.2.3", "Ta sama osoba składa oferty konkurentów", "N",
     "Pliki ofert nie są publiczne, więc nie widać, kto je składa; pośrednio wskazuje"
     " na to punkt 3.2.11 (wspólny adres lub rachunek)."),
    ("3.2.4", "Oferta używa danych innego oferenta (adres, e-mail, telefon)", "C",
     "wspólne dane kontaktowe z KRS, CEIDG i otwarć"),
    ("3.2.5", "Oferty niekompletne mimo wcześniej kompletnych", "C",
     "zmiana odsetka odrzuceń firmy w czasie"),
    ("3.2.6", "Identyczne szacunki kosztów lub warunki dodatkowe", "C",
     "identyczne wartości kryteriów pozacenowych, jeśli są w otwarciu"),
    ("3.2.7", "Podobne poprawki", "N", "treść ofert niepubliczna"),
    ("3.2.8", "Oferty mniej szczegółowe, nieautentyczne", "N",
     "Treść ofert nie jest publiczna, więc nie da się ocenić ich szczegółowości"
     " ani autentyczności."),
    ("3.2.9", "Oferty składane razem lub w krótkim odstępie", "C",
     "jeśli czas złożenia jest w raporcie z otwarcia"),
    ("3.2.10", "Stała kolejność składania ofert", "C",
     "Kolejność składania ofert sprawdzamy, jeśli raport z otwarcia podaje czas złożenia"
     " każdej oferty."),
    ("3.2.11", "Ten sam adres lub siedziba, wspólne zobowiązania", "A",
     "adresy, wspólne osoby i wspólnicy w KRS; broker niedostępny"),
    # 3.3 Ceny
    ("3.3.1", "Nagłe lub identyczne podwyżki", "C",
     "indeks cen względem szacunku w rynku, skoki wspólne dla grupy"),
    ("3.3.2", "Oczekiwane obniżki znikają", "C",
     "Porównujemy indeks cen względem szacunku w rynku z trendem rynku i szukamy grup,"
     " u których spodziewany spadek cen nie następuje."),
    ("3.3.3", "Obniżki mniejsze niż typowe", "A",
     "stosunek ceny zwycięskiej do szacunku na tle rynku"),
    ("3.3.4", "Ceny niezmienne mimo zmian rynku", "C",
     "stabilność cen firmy przy zmienności rynku"),
    ("3.3.5", "Identyczne ceny, choć wcześniej różne", "A", "duplikaty cen w części i w czasie"),
    ("3.3.6", "Regularne różnice cen, wzór matematyczny", "A",
     "stałość stosunków cen par oferentów, współczynnik zmienności stosunków"),
    ("3.3.7", "Duża luka zwycięzca vs reszta, przegrani zbliżeni", "A",
     "odległość względna, rozrzut cen przegranych"),
    ("3.3.8", "Oferta firmy dużo wyższa niż w podobnym zamówieniu", "A",
     "podobieństwo zamówień: embeddingi plus Jev `noul`"),
    ("3.3.9", "Zwycięska oferta znacznie ponad szacunek", "A",
     "cena zwycięska / wartość szacunkowa"),
    ("3.3.10", "Spadek cen po wejściu nowego oferenta", "A",
     "porównanie przed/po wejściu, względem szacunku"),
    ("3.3.11", "Firma lokalna drożej u siebie niż daleko", "A",
     "odległość siedziba-zamawiający, regresja ceny"),
    ("3.3.12", "Podobne koszty transportu", "N", "brak danych o kosztach"),
    ("3.3.13", "Jeden oferent pyta hurtowników o ceny", "N", "brak danych"),
    # 3.4 Podejrzane wypowiedzi: wszystkie N
    ("3.4.1", "Porozumienie", "N", _SPEECH),
    ("3.4.2", '"Ceny branżowe"', "N", _SPEECH),
    ("3.4.3", '"Nasz obszar"', "N", _SPEECH),
    ("3.4.4", '"Teren należy do"', "N", _SPEECH),
    ("3.4.5", "Niepubliczna wiedza o ofertach", "N", _SPEECH),
    ("3.4.6", "Oferty pozorne", "N", _SPEECH),
    ("3.4.7", "Ta sama terminologia", "N", _SPEECH),
    ("3.4.8", "Obawy wobec oświadczeń o niezależności", "N", _SPEECH),
    ("3.4.9", "Dyskusje w stowarzyszeniu", "N", _SPEECH),
    # 3.5 Podejrzane zachowania
    ("3.5.1", "Spotkania przed złożeniem ofert", "N", "brak danych"),
    ("3.5.2", "Regularne kontakty towarzyskie", "N", "brak danych"),
    ("3.5.3", "Prośba o dokumenty dla siebie i konkurenta", "N", "brak danych"),
    ("3.5.4", "Składa swoją ofertę i ofertę konkurenta", "N", "brak danych"),
    ("3.5.5", "Oferta firmy niezdolnej do realizacji", "C",
     "młoda firma, brak historii, kapitał vs wartość zamówienia"),
    ("3.5.6", "Próby ustalenia, kto startuje", "N", "brak danych"),
    ("3.5.7", "Podobne zapytania do zamawiającego", "N", "pytania publikowane anonimowo"),
    ("3.5.8", "Ci sami konsultanci", "N", "brak danych"),
    ("3.5.9", "Zwycięzca zleca prace przegranym", "N", "BZP podaje tylko flagę podwykonawstwa"),
    ("3.5.10", "Zwycięzca nie podejmuje realizacji, potem podwykonawca", "C",
     "wybór kolejnej oferty po uchyleniu się zwycięzcy"),
]

CATALOG: list[OecdItem] = [
    OecdItem(item=item, section=item.rsplit(".", 1)[0], title=title, status=status, how=how)
    for item, title, status, how in _ROWS
]


def seed_catalog(conn: psycopg.Connection) -> int:
    """Zapisz katalog do `oecd_catalog` (upsert) i usuń punkty spoza katalogu.

    Idempotentne; zatwierdza transakcję. Zwraca liczbę punktów katalogu (51).
    """
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "insert into oecd_catalog(item, section, title, status, how)"
                " values (%s, %s, %s, %s, %s)"
                " on conflict (item) do update set"
                "  section = excluded.section,"
                "  title = excluded.title,"
                "  status = excluded.status,"
                "  how = excluded.how",
                [(c.item, c.section, c.title, c.status, c.how) for c in CATALOG],
            )
            cur.execute(
                "delete from oecd_catalog where not (item = any(%s))",
                ([c.item for c in CATALOG],),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(CATALOG)
