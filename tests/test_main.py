from types import SimpleNamespace

from app import main


def test_create_server_passes_application_once(monkeypatch):
    captured = {}

    class Config:
        def __init__(self, application, **options):
            captured["application"] = application
            captured["options"] = options

    class Server:
        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(main.uvicorn, "Config", Config)
    monkeypatch.setattr(main.uvicorn, "Server", Server)
    application = object()
    settings = SimpleNamespace(
        web_host="0.0.0.0", web_port=8080, log_level="INFO"
    )

    main.create_server(application, settings)

    assert captured == {
        "application": application,
        "options": {
            "host": "0.0.0.0",
            "port": 8080,
            "log_level": "info",
            "proxy_headers": True,
            "forwarded_allow_ips": "127.0.0.1",
        },
    }
