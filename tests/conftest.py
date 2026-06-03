"""Test support: build a minimal but faithful temp ``chat.db`` (+ optional AddressBook) so the
real extractor can run with zero Full Disk Access and zero real data. The same factory generates
the committed synthetic fixture, so fixtures never drift from the extractor's real output shape.

Only the columns the extractor actually queries are created.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def chatdb_factory():
    """Return :func:`make_chatdb` so tests can build a temp chat.db on demand."""
    return make_chatdb


def make_chatdb(db_path, conversations, contacts=None, addressbook_dir=None):
    """Create a chat.db at ``db_path`` from a compact spec.

    conversations: list of dicts, each::
        {
          "identifier": "+15550000001",      # chat_identifier
          "display_name": None | "Group X",  # set for named group chats
          "handles": ["+15550000001", ...],  # participant ids (len 1 => 1:1, >1 => group)
          "messages": [
            {"date": <apple_ns_int>, "from_me": bool, "handle": "<id>" | None,
             "text": str | None, "attributed": bytes | None, "has_att": 0|1, "amt": 0,
             "rowid": int | None},
            ...
          ],
        }

    contacts (optional, requires addressbook_dir): ``{addr: {first,last,org,job,note,birthday,
    nickname,emails,addresses}}`` — builds an AddressBook ``*.abcddb`` so named lookups resolve.
    Message ROWIDs are assigned sequentially in spec order unless an explicit ``rowid`` is given.
    """
    db_path = str(db_path)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, date INTEGER, is_from_me INTEGER, handle_id INTEGER,
            text TEXT, attributedBody BLOB, cache_has_attachments INTEGER,
            associated_message_type INTEGER
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        """
    )

    handle_rowid: dict[str, int] = {}

    def hid_for(addr: str) -> int:
        if addr not in handle_rowid:
            rid = len(handle_rowid) + 1
            handle_rowid[addr] = rid
            cur.execute("INSERT INTO handle (ROWID, id) VALUES (?,?)", (rid, addr))
        return handle_rowid[addr]

    next_rowid = 1
    for ci, conv in enumerate(conversations, start=1):
        cur.execute(
            "INSERT INTO chat (ROWID, chat_identifier, display_name) VALUES (?,?,?)",
            (ci, conv["identifier"], conv.get("display_name")),
        )
        for h in conv["handles"]:
            cur.execute(
                "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?,?)", (ci, hid_for(h))
            )
        for m in conv["messages"]:
            rid = m["rowid"] if m.get("rowid") is not None else next_rowid
            next_rowid = rid + 1
            hid = hid_for(m["handle"]) if m.get("handle") else None
            cur.execute(
                "INSERT INTO message (ROWID, date, is_from_me, handle_id, text, attributedBody, "
                "cache_has_attachments, associated_message_type) VALUES (?,?,?,?,?,?,?,?)",
                (
                    rid,
                    m["date"],
                    1 if m.get("from_me") else 0,
                    hid,
                    m.get("text"),
                    m.get("attributed"),
                    m.get("has_att", 0),
                    m.get("amt", 0),
                ),
            )
            cur.execute(
                "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?,?)", (ci, rid)
            )

    con.commit()
    con.close()

    if contacts and addressbook_dir:
        _make_addressbook(addressbook_dir, contacts)
    return db_path


def _make_addressbook(addressbook_dir, contacts):
    src = Path(addressbook_dir) / "Sources" / "ABC"
    src.mkdir(parents=True, exist_ok=True)
    db = src / "AddressBook-v22.abcddb"
    con = sqlite3.connect(str(db))
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT,
            ZORGANIZATION TEXT, ZJOBTITLE TEXT, ZNOTE TEXT, ZBIRTHDAY REAL, ZNICKNAME TEXT);
        CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZFULLNUMBER TEXT);
        CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZADDRESS TEXT);
        CREATE TABLE ZABCDPOSTALADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZSTREET TEXT,
            ZCITY TEXT, ZSTATE TEXT, ZZIPCODE TEXT, ZCOUNTRYNAME TEXT);
        """
    )
    pk = 0
    for addr, c in contacts.items():
        pk += 1
        cur.execute(
            "INSERT INTO ZABCDRECORD (Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZJOBTITLE, "
            "ZNOTE, ZBIRTHDAY, ZNICKNAME) VALUES (?,?,?,?,?,?,?,?)",
            (
                pk,
                c.get("first"),
                c.get("last"),
                c.get("org"),
                c.get("job"),
                c.get("note"),
                c.get("birthday"),
                c.get("nickname"),
            ),
        )
        if "@" in addr:
            cur.execute("INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS) VALUES (?,?)", (pk, addr))
        else:
            cur.execute(
                "INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER) VALUES (?,?)", (pk, addr)
            )
        for em in c.get("emails", []):
            cur.execute("INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS) VALUES (?,?)", (pk, em))
        for addr_line in c.get("addresses", []):
            cur.execute(
                "INSERT INTO ZABCDPOSTALADDRESS (ZOWNER, ZSTREET) VALUES (?,?)", (pk, addr_line)
            )
    con.commit()
    con.close()
