import uvicorn
from .config import configuration_environment

values, _ = configuration_environment()
uvicorn.run("clipper.server:create_app", factory=True,
            host=values.get("CLIPPER_HOST") or values.get("HOST") or "127.0.0.1",
            port=int(values.get("CLIPPER_PORT") or "6880"))
