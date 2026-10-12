from tie.identity.names import looks_like_company, normalize_name


def test_normalize():
    assert normalize_name('MULTISERVICE Sp. z o.o.') == "multiservice spzoo"
    assert normalize_name("„STEKOP” S.A.") == "stekop sa"
    assert looks_like_company("Multi Projekt Sp. z o.o.")
    assert not looks_like_company("PRO-REM Łukasz Solarz")
