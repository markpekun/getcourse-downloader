import asyncio

from getcourse_downloader.infrastructure.media.audio import (
    AudioSource,
    DirectAudioDownloader,
    DirectAudioDownloadStatus,
    extract_audio_sources,
)


class _ByteStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _Response:
    def __init__(self, *, chunks: list[bytes], status: int = 200) -> None:
        self.status = status
        self.content = _ByteStream(chunks)
        self.content_length = sum(map(len, chunks))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, response: _Response, requests: list[dict[str, object]], **_kwargs) -> None:
        self._response = response
        self._requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def get(self, url: str, **kwargs) -> _Response:
        self._requests.append({"url": url, **kwargs})
        return self._response


def test_extract_audio_sources_reads_audio_src_and_title():
    sources = extract_audio_sources(
        '<audio preload="metadata" src="/files/day.mp3" title="Идеальный день"></audio>',
        "https://school.example/lesson/1",
    )

    assert sources == (
        AudioSource(
            url="https://school.example/files/day.mp3",
            title="Идеальный день",
        ),
    )


def test_extract_audio_sources_reads_nested_source_and_deduplicates_urls():
    sources = extract_audio_sources(
        """
        <audio title="Практика"><source src="audio/one.mp3" type="audio/mpeg"></audio>
        <audio src="audio/one.mp3"></audio>
        <audio><source src="https://cdn.example/two.m4a"></audio>
        """,
        "https://school.example/course/lesson",
    )

    assert sources == (
        AudioSource("https://school.example/course/audio/one.mp3", "Практика"),
        AudioSource("https://cdn.example/two.m4a", ""),
    )


def test_extract_audio_sources_ignores_non_http_and_empty_urls():
    sources = extract_audio_sources(
        '<audio src="javascript:alert(1)"></audio><audio src=""></audio>',
        "https://school.example/lesson/1",
    )

    assert sources == ()


def test_direct_audio_downloader_streams_redirectable_response_and_finalizes_atomically(tmp_path):
    requests: list[dict[str, object]] = []
    downloader = DirectAudioDownloader(
        session_factory=lambda **kwargs: _Session(
            _Response(chunks=[b"abc", b"def"]),
            requests,
            **kwargs,
        )
    )
    output = tmp_path / "lesson.mp3"

    result = asyncio.run(
        downloader.download("https://school.example/audio?signature=secret", output)
    )

    assert result.status is DirectAudioDownloadStatus.DOWNLOADED
    assert output.read_bytes() == b"abcdef"
    assert not output.with_name(".lesson.mp3.gcd-part").exists()
    assert requests[0]["allow_redirects"] is True
    assert requests[0]["headers"] == {}


def test_direct_audio_progress_does_not_emit_for_every_network_chunk(tmp_path):
    chunks = [b"x"] * 128
    downloader = DirectAudioDownloader(
        session_factory=lambda **kwargs: _Session(_Response(chunks=chunks), [], **kwargs)
    )
    progress = []

    result = asyncio.run(
        downloader.download(
            "https://school.example/audio",
            tmp_path / "lesson.mp3",
            on_progress=lambda current, total: progress.append((current, total)),
        )
    )

    assert result.status is DirectAudioDownloadStatus.DOWNLOADED
    assert progress[-1] == (128, 128)
    assert len(progress) <= 4


def test_direct_audio_downloader_resumes_partial_response(tmp_path):
    requests: list[dict[str, object]] = []
    downloader = DirectAudioDownloader(
        session_factory=lambda **kwargs: _Session(
            _Response(chunks=[b"def"], status=206), requests, **kwargs
        )
    )
    output = tmp_path / "lesson.mp3"
    output.with_name(".lesson.mp3.gcd-part").write_bytes(b"abc")

    result = asyncio.run(downloader.download("https://school.example/audio", output))

    assert result.status is DirectAudioDownloadStatus.DOWNLOADED
    assert output.read_bytes() == b"abcdef"
    assert requests[0]["headers"] == {"Range": "bytes=3-"}


def test_direct_audio_downloader_cancellation_keeps_partial_file(tmp_path):
    requests: list[dict[str, object]] = []
    cancelled = False

    def is_cancelled() -> bool:
        return cancelled

    class _CancellingStream(_ByteStream):
        async def iter_chunked(self, _size: int):
            nonlocal cancelled
            yield b"abc"
            cancelled = True
            yield b"def"

    response = _Response(chunks=[])
    response.content = _CancellingStream([])
    downloader = DirectAudioDownloader(
        session_factory=lambda **kwargs: _Session(response, requests, **kwargs)
    )
    output = tmp_path / "lesson.mp3"

    result = asyncio.run(
        downloader.download("https://school.example/audio", output, is_cancelled=is_cancelled)
    )

    assert result.status is DirectAudioDownloadStatus.CANCELLED
    assert not output.exists()
    assert output.with_name(".lesson.mp3.gcd-part").read_bytes() == b"abc"


def test_direct_audio_downloader_stops_while_waiting_for_the_next_chunk(tmp_path):
    requests: list[dict[str, object]] = []

    class _SlowStream:
        def __init__(self) -> None:
            self.waiting = asyncio.Event()

        async def iter_chunked(self, _size: int):
            yield b"abc"
            self.waiting.set()
            await asyncio.sleep(10)
            yield b"def"

    async def scenario():
        cancelled = asyncio.Event()
        response = _Response(chunks=[])
        stream = _SlowStream()
        response.content = stream
        downloader = DirectAudioDownloader(
            session_factory=lambda **kwargs: _Session(response, requests, **kwargs)
        )
        output = tmp_path / "lesson.mp3"
        task = asyncio.create_task(
            downloader.download(
                "https://school.example/audio",
                output,
                is_cancelled=cancelled.is_set,
                cancellation_event=cancelled,
            )
        )
        await stream.waiting.wait()
        cancelled.set()
        result = await asyncio.wait_for(task, timeout=0.5)
        return result, output

    result, output = asyncio.run(scenario())

    assert result.status is DirectAudioDownloadStatus.CANCELLED
    assert not output.exists()
    assert output.with_name(".lesson.mp3.gcd-part").read_bytes() == b"abc"
