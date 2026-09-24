"""Bounded multipart reception directly on the uploads volume."""
from pathlib import Path

from fastapi import HTTPException
from python_multipart.exceptions import MultipartParseError
from starlette.formparsers import MultiPartException, MultiPartParser


class MediaUploadParser(MultiPartParser):
    """Keep Starlette's multipart decoding, but never spool audio into /tmp.

    The caller reserves a job before parsing and owns removal of the path on
    failure. Starlette's file writes run in its thread pool.
    """

    def __init__(self, headers, stream, *, path: Path, max_bytes: int, validate_header, validate_filename):
        super().__init__(headers, stream, max_files=1, max_fields=1, max_part_size=1200)
        self.path = path
        self.max_bytes = max_bytes
        self.validate_header = validate_header
        self.validate_filename = validate_filename
        self.file_bytes = 0
        self.media_header = bytearray()
        self.finished = False

    def on_headers_finished(self):
        super().on_headers_finished()
        part = self._current_part
        if part.file is not None:
            if part.field_name != "file":
                raise MultiPartException("Expected the file field")
            self.validate_filename(part.file.filename or "")
            # super() has only allocated an empty in-memory spool at this point.
            # Replace it before the first byte can spill onto the default tmpfs.
            part.file.file.close()
            target = self.path.open("xb+")
            self._files_to_close_on_error.append(target)
            part.file.file = target
        elif part.field_name != "defendant":
            raise MultiPartException("Unexpected form field")

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > self.max_bytes:
                raise HTTPException(413, "Файл слишком большой")
            self.media_header.extend(data[start:min(end, start + 16 - len(self.media_header))])
            if len(self.media_header) == 16:
                self._check_header()
        super().on_part_data(data, start, end)

    def _check_header(self):
        if not self.validate_header(bytes(self.media_header)):
            raise HTTPException(400, "Содержимое файла не соответствует поддерживаемому аудио/видео формату")

    def on_end(self):
        self.finished = True

    async def receive(self):
        try:
            form = await self.parse()
            if not self.finished:
                raise HTTPException(400, "Загрузка файла прервана")
            if not self.file_bytes:
                raise HTTPException(400, "Загруженный файл пуст")
            self._check_header()
            return form
        except MultipartParseError as exc:
            raise HTTPException(400, "Некорректная multipart-форма") from exc
        finally:
            for handle in self._files_to_close_on_error:
                handle.close()
