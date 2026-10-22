"""Start the voice studio with ``python -m qwen3_tts_openai``."""

import logging

import uvicorn


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from qwen3_tts_openai.app import app

    settings = app.state.service.settings
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1,
                ssl_certfile=settings.tls_certfile or None, ssl_keyfile=settings.tls_keyfile or None)


if __name__ == "__main__":
    main()
