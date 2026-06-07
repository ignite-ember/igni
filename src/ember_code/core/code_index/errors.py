"""Plain-Python exceptions raised by the code_index package."""

from __future__ import annotations


class CodeIndexError(Exception):
    """Base class for all code_index errors."""


class FileOrFolderDoesNotExist(CodeIndexError):
    pass


class FolderAlreadyExist(CodeIndexError):
    pass


class FileWithTheSameNameAlreadyCreated(CodeIndexError):
    pass


class FileOrFolderWithTheSameNameAlreadyExist(CodeIndexError):
    pass


class FileWithTheSameContentIsAlreadyCreated(CodeIndexError):
    pass


class FileWithTheSameUUIDAlreadyExists(CodeIndexError):
    pass


class ParentFolderDoesNotExist(CodeIndexError):
    def __init__(self, parent_id: str | None = None):
        msg = "Parent folder does not exist"
        if parent_id:
            msg += f" (parent_id={parent_id})"
        super().__init__(msg)
        self.parent_id = parent_id


class TargetFolderDoesNotExist(CodeIndexError):
    pass


class OnlyEmptyFolderCanBeDeleted(CodeIndexError):
    pass


class FolderCanNotBeMoved(CodeIndexError):
    pass


class FileOrFolderDeletionFailed(CodeIndexError):
    pass


class FileContentVectorizationFailed(CodeIndexError):
    def __init__(self, original: BaseException):
        super().__init__(f"File content vectorization failed: {original}")
        self.original = original
