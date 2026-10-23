"""Public navigation metadata; never includes model or account configuration."""
import os
from urllib.parse import urlsplit


def tools_configuration(environment=None):
    values = os.environ if environment is None else environment
    enabled = values.get("CLIPPER_ENABLED", "").strip().lower() in {"1", "true", "yes"}
    try:
        port = int(values.get("CLIPPER_PORT", "6880"))
        if not 1 <= port <= 65535:
            raise ValueError()
    except (TypeError, ValueError):
        enabled, port = False, 6880
    public_url = str(values.get("CLIPPER_PUBLIC_URL", "")).strip()
    if public_url:
        try:
            parsed = urlsplit(public_url)
            valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and parsed.path in {"", "/"}
                     and not parsed.query and not parsed.fragment
                     and not any(ord(char) < 32 for char in public_url))
            parsed.port  # Validate malformed port spellings.
            if not valid:
                raise ValueError()
            public_url = public_url.rstrip("/") + "/"
        except ValueError:
            enabled, public_url = False, ""
    return {"clipping": {"enabled": enabled, "port": port, "public_url": public_url}}
