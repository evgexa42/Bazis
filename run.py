from app import config as app_config
from app import create_app, logger

app = create_app()

if __name__ == "__main__":
    logger.info("Сервер запущен: http://%s:%s", "0.0.0.0", app_config.SERVER_PORT)
    app.run(
        host="0.0.0.0",
        port=app_config.SERVER_PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )