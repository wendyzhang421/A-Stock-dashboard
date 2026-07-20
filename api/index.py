def app(environ, start_response):
    body = b"A-Stock dashboard Python entrypoint is healthy."
    start_response(
        "200 OK",
        [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]
