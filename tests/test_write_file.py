from contextlib import contextmanager
from tempfile import NamedTemporaryFile

import pytest

from tools import filesystem
from tools.filesystem import WriteFileTool


@pytest.mark.parametrize("stage", ["write", "flush", "fsync", "chmod", "replace"])
def test_failed_write_cleans_temporary_file_and_preserves_original(tmp_path, monkeypatch, stage):
    target = tmp_path / "existing.txt"
    target.write_text("original")

    def fail(*args, **kwargs):
        raise OSError(f"simulated {stage} failure")

    @contextmanager
    def temporary_file(**kwargs):
        with NamedTemporaryFile(**kwargs) as actual:
            # Inject a failure into real temporary-file I/O, retaining real close behavior.
            class FileProxy:
                name = actual.name
                write = staticmethod(fail if stage == "write" else actual.write)
                flush = staticmethod(fail if stage == "flush" else actual.flush)
                fileno = staticmethod(actual.fileno)

            yield FileProxy()

    monkeypatch.setattr(filesystem, "NamedTemporaryFile", temporary_file)
    if stage in {"fsync", "chmod"}:
        monkeypatch.setattr(filesystem.os, stage, fail)
    elif stage == "replace":
        monkeypatch.setattr(filesystem.Path, "replace", fail)

    result = WriteFileTool(tmp_path).execute(
        {
            "path": "existing.txt",
            "content": "replacement",
            "overwrite": True,
        }
    )

    assert not result.success
    assert result.error_code == "WRITE_ERROR"
    assert target.read_text() == "original"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("existing", [False, True])
def test_success_does_not_leave_temporary_file(tmp_path, existing):
    target = tmp_path / "file.txt"
    if existing:
        target.write_text("original")
    result = WriteFileTool(tmp_path).execute(
        {
            "path": "file.txt",
            "content": "replacement",
            "overwrite": existing,
        }
    )
    assert result.success
    assert target.read_text() == "replacement"
    assert list(tmp_path.iterdir()) == [target]


def test_cleanup_failure_logs_warning_without_masking_write_error(tmp_path, monkeypatch, caplog):
    def fail_sync(*args):
        raise PermissionError("original write error")

    def fail_cleanup(*args, **kwargs):
        raise OSError("cleanup error")

    with monkeypatch.context() as scoped:
        scoped.setattr(filesystem.os, "fsync", fail_sync)
        scoped.setattr(filesystem.Path, "unlink", fail_cleanup)
        result = WriteFileTool(tmp_path).execute({"path": "new.txt", "content": "content"})

    assert not result.success
    assert result.error_code == "PERMISSION_DENIED"
    assert not (tmp_path / "new.txt").exists()
    assert "Unable to remove temporary file" in caplog.text
    assert "cleanup error" in caplog.text
    leftovers = list(tmp_path.iterdir())
    assert len(leftovers) == 1
    assert str(leftovers[0]) in caplog.text
