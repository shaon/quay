"""add requested digest to imagestorage and blobupload

Revision ID: c55c2ec82b92
Revises: 78f840600e4c
Create Date: 2026-11-08 18:55:05.457555

PROJQUAY-10117, Proposal 1. Adds:

- ImageStorage.requested_content_checksum: the client-visible sha384/sha512 digest of a storage
  row. NULL means the row is a SHA-256 row whose identity is its content_checksum. No backfill.
- A check constraint limiting non-NULL values to normalized sha384:/sha512: digests.
- A partial unique index on (requested_content_checksum, content_checksum) where
  requested_content_checksum is not NULL. Its leading column also serves identity lookups by
  requested digest.
- BlobUpload.requested_digest_algorithm and BlobUpload.requested_digest_state: the hinted
  algorithm and its resumable hash state.

The upgrade is additive, online, and safe to rerun. On PostgreSQL the columns are added as
nullable (a metadata-only change), the check constraint is added NOT VALID and validated
separately (VALIDATE takes only a SHARE UPDATE EXCLUSIVE lock), and the index is built
CONCURRENTLY, dropping an invalid leftover from a failed earlier attempt first. Other dialects
(SQLite in unit tests) use plain DDL with batch mode for the constraint.

The downgrade refuses to run while any ImageStorage row has a non-NULL
requested_content_checksum: dropping the column would expose those rows under their internal
SHA-256. Dropping the BlobUpload columns only fails in-flight hinted uploads.
"""

# revision identifiers, used by Alembic.
revision = "c55c2ec82b92"
down_revision = "78f840600e4c"

import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

CHECK_NAME = "imagestorage_requested_content_checksum_check"
INDEX_NAME = "imagestorage_requested_content_checksum_unique"

# Keep in sync with data.database.IMAGESTORAGE_REQUESTED_CHECKSUM_CHECK.
CHECK_SQL = (
    "requested_content_checksum IS NULL"
    " OR (substr(requested_content_checksum, 1, 7) = 'sha384:'"
    " AND length(requested_content_checksum) = 103)"
    " OR (substr(requested_content_checksum, 1, 7) = 'sha512:'"
    " AND length(requested_content_checksum) = 135)"
)

INDEX_SQL = (
    "CREATE UNIQUE INDEX {concurrently}{name} ON imagestorage "
    "(requested_content_checksum, content_checksum) "
    "WHERE requested_content_checksum IS NOT NULL"
)


class RequestedDigestRowsExist(Exception):
    pass


def _columns(inspector, table):
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(inspector, table):
    return {i["name"] for i in inspector.get_indexes(table)}


def _check_constraints(inspector, table):
    try:
        return {c["name"] for c in inspector.get_check_constraints(table)}
    except NotImplementedError:
        return set()


def _drop_invalid_pg_index(bind, index_name):
    is_valid = bind.execute(
        sa.text(
            "SELECT i.indisvalid FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = :name"
        ),
        {"name": index_name},
    ).scalar()
    if is_valid is not None and not is_valid:
        bind.execute(sa.text(f"DROP INDEX IF EXISTS {index_name}"))
        return True
    return False


def upgrade(op, tables, tester):
    bind = op.get_bind()
    dialect = bind.dialect.name
    inspector = Inspector.from_engine(bind)

    imagestorage_columns = _columns(inspector, "imagestorage")
    if "requested_content_checksum" not in imagestorage_columns:
        op.add_column(
            "imagestorage",
            sa.Column("requested_content_checksum", sa.String(length=255), nullable=True),
        )

    blobupload_columns = _columns(inspector, "blobupload")
    if "requested_digest_algorithm" not in blobupload_columns:
        op.add_column(
            "blobupload",
            sa.Column("requested_digest_algorithm", sa.String(length=255), nullable=True),
        )
    if "requested_digest_state" not in blobupload_columns:
        op.add_column("blobupload", sa.Column("requested_digest_state", sa.Text(), nullable=True))

    inspector = Inspector.from_engine(bind)
    if CHECK_NAME not in _check_constraints(inspector, "imagestorage"):
        if dialect == "postgresql":
            op.execute(
                f"ALTER TABLE imagestorage ADD CONSTRAINT {CHECK_NAME} CHECK ({CHECK_SQL}) NOT VALID"
            )
            op.execute(f"ALTER TABLE imagestorage VALIDATE CONSTRAINT {CHECK_NAME}")
        else:
            with op.batch_alter_table("imagestorage") as batch_op:
                batch_op.create_check_constraint(CHECK_NAME, sa.text(CHECK_SQL))

    inspector = Inspector.from_engine(bind)
    existing_indexes = _indexes(inspector, "imagestorage")
    if dialect == "postgresql":
        if INDEX_NAME in existing_indexes and _drop_invalid_pg_index(bind, INDEX_NAME):
            existing_indexes.discard(INDEX_NAME)
        if INDEX_NAME not in existing_indexes:
            with op.get_context().autocommit_block():
                op.execute(INDEX_SQL.format(concurrently="CONCURRENTLY ", name=INDEX_NAME))
    elif INDEX_NAME not in existing_indexes:
        op.execute(INDEX_SQL.format(concurrently="", name=INDEX_NAME))


def downgrade(op, tables, tester):
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)

    if "requested_content_checksum" in _columns(inspector, "imagestorage"):
        has_rows = bind.execute(
            sa.text(
                "SELECT 1 FROM imagestorage WHERE requested_content_checksum IS NOT NULL LIMIT 1"
            )
        ).scalar()
        if has_rows:
            raise RequestedDigestRowsExist(
                "Refusing to downgrade: ImageStorage rows with a requested_content_checksum exist. "
                "Dropping the column would expose SHA-384/SHA-512 content under its internal "
                "SHA-256. Remove every alternative-digest manifest and tag and let GC delete the "
                "affected rows first."
            )

    if INDEX_NAME in _indexes(inspector, "imagestorage"):
        op.execute(f"DROP INDEX {INDEX_NAME}")

    inspector = Inspector.from_engine(bind)
    if CHECK_NAME in _check_constraints(inspector, "imagestorage"):
        with op.batch_alter_table("imagestorage") as batch_op:
            batch_op.drop_constraint(CHECK_NAME, type_="check")

    inspector = Inspector.from_engine(bind)
    if "requested_content_checksum" in _columns(inspector, "imagestorage"):
        with op.batch_alter_table("imagestorage") as batch_op:
            batch_op.drop_column("requested_content_checksum")

    blobupload_columns = _columns(inspector, "blobupload")
    with op.batch_alter_table("blobupload") as batch_op:
        if "requested_digest_state" in blobupload_columns:
            batch_op.drop_column("requested_digest_state")
        if "requested_digest_algorithm" in blobupload_columns:
            batch_op.drop_column("requested_digest_algorithm")
