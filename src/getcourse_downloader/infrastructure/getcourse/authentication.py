from urllib.parse import parse_qs, urlsplit


def is_authentication_url(url: str) -> bool:
    """Recognize GetCourse login redirects without matching school names."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if "login" in parts.path.casefold().split("/"):
        return True
    return "true" in parse_qs(parts.query.casefold()).get("required", [])
