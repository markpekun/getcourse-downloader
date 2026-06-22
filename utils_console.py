import io
import sys


def configure_console_output() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        elif not sys.getdefaultencoding().lower().startswith("utf"):
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                setattr(
                    sys,
                    stream_name,
                    io.TextIOWrapper(buffer, encoding="utf-8"),
                )
