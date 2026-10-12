"""Testy parowe OECD (do implementacji)."""


def pair_key(a: int, b: int, market_id: str) -> str:
    return f"{a}-{b}|{market_id}"


def compute_pair_tests(conn, run_id: int) -> int:
    raise NotImplementedError
