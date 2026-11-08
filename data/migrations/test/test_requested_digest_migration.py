import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from data.migrations.tester import NoopTester
from data.migrations.versions import (
    c55c2ec82b92_add_requested_digest_to_imagestorage_ as migration,
)

SHA256 = "sha256:" + "a" * 64
SHA384 = "sha384:" + "b" * 96
SHA512 = "sha512:" + "c" * 128


def _operations(connection):
    context = MigrationContext.configure(connection, opts={"render_as_batch": True})
    return Operations(context)


def _create_tables(connection):
    metadata = sa.MetaData()
    sa.Table(
        "imagestorage",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(255), nullable=False),
        sa.Column("content_checksum", sa.String(255), nullable=True),
    )
    sa.Table(
        "blobupload",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(255), nullable=False),
        sa.Column("sha_state", sa.Text(), nullable=True),
    )
    metadata.create_all(connection)


def _columns(connection, table):
    return {c["name"] for c in sa.inspect(connection).get_columns(table)}


def _indexes(connection, table):
    return {i["name"] for i in sa.inspect(connection).get_indexes(table)}


def _insert(connection, uuid, checksum, requested):
    connection.execute(
        sa.text(
            "INSERT INTO imagestorage (uuid, content_checksum, requested_content_checksum) "
            "VALUES (:u, :c, :r)"
        ),
        {"u": uuid, "c": checksum, "r": requested},
    )


@pytest.fixture()
def connection():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _create_tables(conn)
        conn.execute(sa.text("INSERT INTO imagestorage (uuid, content_checksum) VALUES ('pre', :c)"), {"c": SHA256})
        yield conn


def test_upgrade_adds_columns_constraint_and_index(connection):
    migration.upgrade(_operations(connection), None, NoopTester())

    assert "requested_content_checksum" in _columns(connection, "imagestorage")
    assert {"requested_digest_algorithm", "requested_digest_state"} <= _columns(
        connection, "blobupload"
    )
    assert migration.INDEX_NAME in _indexes(connection, "imagestorage")

    # Existing rows are untouched (no backfill) and read as SHA-256 rows.
    row = connection.execute(
        sa.text("SELECT requested_content_checksum FROM imagestorage WHERE uuid = 'pre'")
    ).scalar()
    assert row is None


def test_upgrade_is_rerunnable(connection):
    migration.upgrade(_operations(connection), None, NoopTester())
    migration.upgrade(_operations(connection), None, NoopTester())
    assert migration.INDEX_NAME in _indexes(connection, "imagestorage")


@pytest.mark.parametrize(
    "requested, ok",
    [
        (None, True),
        (SHA384, True),
        (SHA512, True),
        (SHA256, False),
        ("sha512:" + "c" * 127, False),
        ("sha384:" + "b" * 95, False),
        ("md5:abc", False),
    ],
)
def test_check_constraint(connection, requested, ok):
    migration.upgrade(_operations(connection), None, NoopTester())
    if ok:
        _insert(connection, "x", SHA256, requested)
    else:
        with pytest.raises(sa.exc.IntegrityError):
            _insert(connection, "x", SHA256, requested)


def test_partial_unique_index(connection):
    migration.upgrade(_operations(connection), None, NoopTester())

    # SHA-256 rows keep today's non-unique behavior.
    _insert(connection, "s1", SHA256, None)
    _insert(connection, "s2", SHA256, None)

    # One SHA-384 and one SHA-512 row per content checksum.
    _insert(connection, "r1", SHA256, SHA512)
    _insert(connection, "r2", SHA256, SHA384)
    with pytest.raises(sa.exc.IntegrityError):
        _insert(connection, "r3", SHA256, SHA512)

    # Placeholders with a NULL content_checksum are not limited.
    _insert(connection, "p1", None, SHA512)
    _insert(connection, "p2", None, SHA512)


def test_downgrade_refuses_with_requested_rows(connection):
    migration.upgrade(_operations(connection), None, NoopTester())
    _insert(connection, "r1", SHA256, SHA512)

    with pytest.raises(migration.RequestedDigestRowsExist):
        migration.downgrade(_operations(connection), None, NoopTester())

    assert "requested_content_checksum" in _columns(connection, "imagestorage")


def test_downgrade_without_requested_rows(connection):
    migration.upgrade(_operations(connection), None, NoopTester())
    _insert(connection, "s1", SHA256, None)

    migration.downgrade(_operations(connection), None, NoopTester())

    assert "requested_content_checksum" not in _columns(connection, "imagestorage")
    assert migration.INDEX_NAME not in _indexes(connection, "imagestorage")
    assert not {"requested_digest_algorithm", "requested_digest_state"} & _columns(
        connection, "blobupload"
    )

    # The downgrade is rerunnable, and upgrade works again afterwards.
    migration.downgrade(_operations(connection), None, NoopTester())
    migration.upgrade(_operations(connection), None, NoopTester())
    assert migration.INDEX_NAME in _indexes(connection, "imagestorage")
