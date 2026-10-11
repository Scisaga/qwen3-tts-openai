import logging

import uvicorn

from app import app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = app.state.service.settings
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1,
                ssl_certfile=settings.tls_certfile or None, ssl_keyfile=settings.tls_keyfile or None)
