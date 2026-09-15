"""Application-specific exception hierarchy."""


class DownloaderError(Exception):
    """Base error safe to present to a user."""

    code = "DOWNLOADER_ERROR"
    technical_details = ""


class InvalidDataError(DownloaderError):
    """Persisted or transferred data does not match the expected schema."""


class DownloadConfigurationError(DownloaderError):
    """A download request cannot be executed with the supplied settings."""

    code = "INVALID_CONFIGURATION"


class ExternalServiceError(DownloaderError):
    """GetCourse, Rutube, Playwright, or another external service failed."""

    code = "EXTERNAL_SERVICE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        technical_details: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code or type(self).code
        self.technical_details = technical_details
