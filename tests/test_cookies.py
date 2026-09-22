import sqlite3
from pathlib import Path

from yuanliu.cookies import (
    CookieRecord,
    load_firefox_cookies,
    to_netscape,
)


def _make_profile(tmp_path: Path, rows) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    con = sqlite3.connect(profile / "cookies.sqlite")
    con.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT,"
        " path TEXT, expiry INTEGER, isSecure INTEGER)"
    )
    con.executemany("INSERT INTO moz_cookies (host,name,value,path,expiry,isSecure) VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return profile


def test_load_filters_by_domain(tmp_path: Path) -> None:
    profile = _make_profile(
        tmp_path,
        [
            (".douyin.com", "sessionid", "abc", "/", 4102444800, 1),
            (".example.com", "x", "y", "/", 0, 0),
        ],
    )
    cookies = load_firefox_cookies(profile, domains=("douyin.com",))
    assert [c.name for c in cookies] == ["sessionid"]


def test_load_all_when_no_domain_filter(tmp_path: Path) -> None:
    profile = _make_profile(
        tmp_path,
        [
            (".douyin.com", "a", "1", "/", 0, 0),
            (".example.com", "b", "2", "/", 0, 0),
        ],
    )
    assert len(load_firefox_cookies(profile, domains=())) == 2


def test_to_netscape_format() -> None:
    cookie = CookieRecord(host=".douyin.com", name="sessionid", value="v", path="/", expiry=123, secure=True)
    line = to_netscape([cookie]).splitlines()[-1]
    assert line == ".douyin.com\tTRUE\t/\tTRUE\t123\tsessionid\tv"


def test_missing_profile_raises(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        load_firefox_cookies(tmp_path / "nope")


def test_normalize_expiry_milliseconds(tmp_path: Path) -> None:
    from yuanliu.cookies import normalize_expiry

    assert normalize_expiry(1821538792000) == 1821538792  # 毫秒 → 秒
    assert normalize_expiry(1821538792) == 1821538792
    assert normalize_expiry(0) == 0


def test_to_netscape_writes_seconds() -> None:
    cookie = CookieRecord(host=".bilibili.com", name="a", value="b", path="/", expiry=1821538792000, secure=False)
    assert to_netscape([cookie]).splitlines()[-1].split("\t")[4] == "1821538792"
