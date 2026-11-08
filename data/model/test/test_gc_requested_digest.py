"""
PROJQUAY-10117: GC keeps CAS bytes shared by distinct ImageStorage rows (SHA-256 and requested
digest rows for the same bytes) and deletes unlinked, unfilled SHA-384/512 placeholders.
"""

import hashlib
import os
from datetime import datetime, timedelta

import pytest

from app import storage
from data.database import (
    ImageStorage,
    ImageStorageLocation,
    ImageStoragePlacement,
    UploadedBlob,
)
from data.model import repository
from data.model.storage import garbage_collect_storage
from test.fixtures import *


def _row(content, requested=None, placed=True, content_checksum=True):
    sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    row = ImageStorage.create(
        content_checksum=sha256 if content_checksum else None,
        requested_content_checksum=requested,
        image_size=len(content),
        cas_path=True,
    )
    if placed:
        ImageStoragePlacement.create(storage=row, location=ImageStorageLocation.get(name="local_us"))
    return row


def _store(content):
    sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    path = storage.blob_path(sha256)
    storage.put_content(["local_us"], path, content)
    return path


@pytest.mark.parametrize("delete_first", ["sha256", "sha512"])
def test_shared_cas_object_kept_until_last_row(initialized_db, delete_first):
    content = os.urandom(64)
    sha512 = "sha512:" + hashlib.sha512(content).hexdigest()
    path = _store(content)
    rows = {"sha256": _row(content), "sha512": _row(content, sha512)}
    other = "sha512" if delete_first == "sha256" else "sha256"

    assert garbage_collect_storage([rows[delete_first].id]) == {rows[delete_first].id}
    assert not ImageStorage.select().where(ImageStorage.id == rows[delete_first].id).exists()
    # The other row still has this content_checksum, so the CAS object stays.
    assert storage.exists(["local_us"], path)

    assert garbage_collect_storage([rows[other].id]) == {rows[other].id}
    assert not storage.exists(["local_us"], path)


def test_sha384_and_sha512_rows_share_cas_object(initialized_db):
    content = os.urandom(64)
    path = _store(content)
    r384 = _row(content, "sha384:" + hashlib.sha384(content).hexdigest())
    r512 = _row(content, "sha512:" + hashlib.sha512(content).hexdigest())

    garbage_collect_storage([r384.id])
    assert storage.exists(["local_us"], path)
    garbage_collect_storage([r512.id])
    assert not storage.exists(["local_us"], path)


def test_linked_requested_row_is_not_collected(initialized_db):
    content = os.urandom(64)
    path = _store(content)
    row = _row(content, "sha512:" + hashlib.sha512(content).hexdigest())
    UploadedBlob.create(
        repository=repository.get_repository("devtable", "simple"),
        blob=row,
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    assert garbage_collect_storage([row.id]) == set()
    assert storage.exists(["local_us"], path)


def test_unlinked_unfilled_requested_placeholder_is_deleted(initialized_db):
    content = os.urandom(64)
    placeholder = _row(
        content,
        "sha512:" + hashlib.sha512(content).hexdigest(),
        placed=False,
        content_checksum=False,
    )
    assert garbage_collect_storage([placeholder.id]) == {placeholder.id}
    assert not ImageStorage.select().where(ImageStorage.id == placeholder.id).exists()


def test_linked_requested_placeholder_is_kept(initialized_db):
    content = os.urandom(64)
    placeholder = _row(
        content,
        "sha512:" + hashlib.sha512(content).hexdigest(),
        placed=False,
        content_checksum=False,
    )
    UploadedBlob.create(
        repository=repository.get_repository("devtable", "simple"),
        blob=placeholder,
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    assert garbage_collect_storage([placeholder.id]) == set()


def test_sha256_placeholder_keeps_todays_behavior(initialized_db):
    content = os.urandom(64)
    placeholder = _row(content, placed=False)
    assert garbage_collect_storage([placeholder.id]) == set()
    assert ImageStorage.select().where(ImageStorage.id == placeholder.id).exists()
