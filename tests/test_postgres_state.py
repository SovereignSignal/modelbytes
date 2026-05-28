"""Guard that save_seen_models UPSERTs into models and does not DELETE+INSERT."""
from unittest.mock import MagicMock, patch

import monitor


def _setup_pg_mocks():
    """Returns (mock_cursor, context-manager mocks) wired so connect/cursor work."""
    mock_cur = MagicMock()
    mock_cur_ctx = MagicMock()
    mock_cur_ctx.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur_ctx.__exit__ = MagicMock(return_value=None)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur_ctx
    return mock_cur, mock_conn


def _all_sql_issued(mock_cur):
    """All SQL strings issued through execute() or executemany()."""
    sql_strings = []
    for call in mock_cur.execute.call_args_list:
        if call.args:
            sql_strings.append(call.args[0])
    for call in mock_cur.executemany.call_args_list:
        if call.args:
            sql_strings.append(call.args[0])
    return sql_strings


def test_save_seen_models_uses_upsert_not_delete():
    """save_seen_models must not DELETE FROM models; must use ON CONFLICT."""
    mock_cur, mock_conn = _setup_pg_mocks()
    with patch.object(monitor, "DATABASE_URL", "postgres://fake"), \
         patch.object(monitor.psycopg2, "connect", return_value=mock_conn):
        monitor.save_seen_models({"foo/bar", "baz/qux"})

    sql_strings = _all_sql_issued(mock_cur)
    assert sql_strings, "save_seen_models issued no SQL"

    for sql in sql_strings:
        assert "DELETE" not in sql.upper(), (
            f"save_seen_models still issues DELETE — found: {sql!r}"
        )

    upsert_found = any("ON CONFLICT" in sql.upper() for sql in sql_strings)
    assert upsert_found, (
        f"save_seen_models did not issue any ON CONFLICT statement. SQL issued: {sql_strings}"
    )


def test_save_seen_models_noop_without_database_url():
    """With no DATABASE_URL, save_seen_models must not attempt to connect."""
    with patch.object(monitor, "DATABASE_URL", ""), \
         patch.object(monitor.psycopg2, "connect") as mock_connect:
        monitor.save_seen_models({"foo/bar"})
    assert not mock_connect.called, (
        "save_seen_models tried to connect to Postgres despite DATABASE_URL being unset"
    )


def test_load_seen_models_returns_empty_set_without_database_url():
    """With no DATABASE_URL, load_seen_models returns an empty set without connecting."""
    with patch.object(monitor, "DATABASE_URL", ""), \
         patch.object(monitor.psycopg2, "connect") as mock_connect:
        result = monitor.load_seen_models()
    assert result == set(), f"expected empty set, got {result!r}"
    assert not mock_connect.called, "load_seen_models tried to connect despite no DATABASE_URL"


def test_init_database_creates_posted_digests_table():
    """init_database must create the post idempotency ledger."""
    mock_cur, mock_conn = _setup_pg_mocks()
    with patch.object(monitor, "DATABASE_URL", "postgres://fake"), \
         patch.object(monitor.psycopg2, "connect", return_value=mock_conn):
        monitor.init_database()

    sql_strings = _all_sql_issued(mock_cur)
    assert any("POSTED_DIGESTS" in sql.upper() for sql in sql_strings), (
        f"init_database did not create posted_digests. SQL issued: {sql_strings}"
    )


def test_has_posted_digest_queries_ledger():
    """has_posted_digest checks the posted_digests table by UTC date."""
    mock_cur, mock_conn = _setup_pg_mocks()
    mock_cur.fetchone.return_value = (1,)
    with patch.object(monitor, "DATABASE_URL", "postgres://fake"), \
         patch.object(monitor.psycopg2, "connect", return_value=mock_conn):
        result = monitor.has_posted_digest("2026-05-21")

    assert result is True
    sql_strings = _all_sql_issued(mock_cur)
    assert any("FROM POSTED_DIGESTS" in sql.upper() for sql in sql_strings)


def test_mark_posted_digest_upserts_ledger():
    """mark_posted_digest records date/source/path/hash without overwriting."""
    mock_cur, mock_conn = _setup_pg_mocks()
    with patch.object(monitor, "DATABASE_URL", "postgres://fake"), \
         patch.object(monitor.psycopg2, "connect", return_value=mock_conn):
        result = monitor.mark_posted_digest(
            "2026-05-21",
            "curated",
            "pending/2026-05-21.txt",
            "hello",
        )

    assert result is True
    sql_strings = _all_sql_issued(mock_cur)
    assert any("INSERT INTO POSTED_DIGESTS" in sql.upper() for sql in sql_strings)
    assert any("ON CONFLICT" in sql.upper() for sql in sql_strings)
