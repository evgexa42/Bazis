from app import SERVER_PORT, app, logger

if __name__ == "__main__":
    logger.info("Сервер запущен: http://%s:%s", "0.0.0.0", SERVER_PORT)
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, threaded=True, use_reloader=False)