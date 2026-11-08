from storage.basestorage import BaseStorage
from storage.distributedstorage import DistributedStorage
from storage.fakestorage import FakeStorage
from storage.local import LocalStorage
from storage.storagecontext import StorageContext


def test_local_get_size(tmpdir):
    engine = LocalStorage(StorageContext("nyc", None, None, None), str(tmpdir))
    assert engine.get_size("some/path") is None
    engine.put_content("some/path", b"12345")
    assert engine.get_size("some/path") == 5


def test_fake_get_size():
    engine = FakeStorage("local")
    assert engine.get_size("some/path") is None
    engine.put_content("some/path", b"123")
    assert engine.get_size("some/path") == 3


def test_distributed_get_size():
    engine = FakeStorage("local")
    distributed = DistributedStorage({"local_us": engine}, ["local_us"])
    engine.put_content("p", b"abcd")
    assert distributed.get_size({"local_us"}, "p") == 4
    assert distributed.get_size({"local_us"}, "missing") is None


class _StreamOnly(BaseStorage):
    def __init__(self, content):
        super().__init__()
        self._content = content

    def exists(self, path):
        return self._content is not None

    def stream_read(self, path):
        yield self._content[:2]
        yield self._content[2:]


def test_base_get_size_default_streams():
    assert _StreamOnly(b"hello").get_size("x") == 5
    assert _StreamOnly(None).get_size("x") is None
