import os
import sys
import types

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# В среде CI/автопроверки Flask может отсутствовать; подставляем минимальный мок,
# чтобы тесты, не трогающие веб-часть, могли импортировать вспомогательные модули.
try:
    import flask  # type: ignore
except ImportError:  # pragma: no cover - защитный путь для изолированной среды
    class _DummyBlueprint:
        def __init__(self, *a, **k):
            pass

        def before_app_request(self, func):
            return func

        def after_app_request(self, func):
            return func

        def app_errorhandler(self, *_a, **_k):
            def deco(func):
                return func
            return deco

    def _dummy_render_template(*_a, **_k):
        return ""

    mock_flask = types.ModuleType("flask")
    mock_flask.Flask = object
    mock_flask.Blueprint = _DummyBlueprint
    mock_flask.g = types.SimpleNamespace()
    mock_flask.jsonify = lambda *a, **k: None
    mock_flask.render_template = _dummy_render_template
    mock_flask.request = types.SimpleNamespace()
    mock_flask.session = {}
    mock_flask.current_app = types.SimpleNamespace(config={})
    mock_flask.has_app_context = lambda: False
    mock_flask.has_request_context = lambda: False
    sys.modules["flask"] = mock_flask

try:
    import sqlalchemy  # type: ignore
except ImportError:  # pragma: no cover - защитный путь для изолированной среды
    class _Dummy:
        def __init__(self, *a, **k):
            pass

        def __call__(self, *a, **k):
            return self

        def __getattr__(self, item):
            return self

        def __iter__(self):
            return iter([])

        def on_conflict_do_update(self, **_):
            return self

        def execute(self, *_a, **_k):
            return None

    class _DummySession:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def begin(self):
            return self

        def query(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def all(self):
            return []

        def delete(self):
            return 0

        def execute(self, *_a, **_k):
            return None

        def merge(self, *_a, **_k):
            return None

    class _DummyMetadata:
        def create_all(self, *_a, **_k):
            return None

    class _DummyBase:
        metadata = _DummyMetadata()

    mock_sa = types.ModuleType("sqlalchemy")
    mock_sa.Column = _Dummy
    mock_sa.Integer = _Dummy
    mock_sa.String = _Dummy
    mock_sa.Text = _Dummy
    mock_sa.Boolean = _Dummy
    mock_sa.DateTime = _Dummy
    mock_sa.func = _Dummy()
    mock_sa.select = lambda *a, **k: None
    mock_sa.inspect = lambda *a, **k: types.SimpleNamespace(get_columns=lambda *_: [], clear_cache=lambda: None)
    mock_sa.create_engine = lambda *a, **k: _Dummy()
    mock_sa.insert = lambda *a, **k: _Dummy()

    def _declarative_base(*_a, **_k):
        return _DummyBase

    mock_sa.orm = types.SimpleNamespace(declarative_base=_declarative_base, sessionmaker=lambda *a, **k: lambda **_m: _DummySession())

    sqlite_module = types.ModuleType("sqlalchemy.dialects.sqlite")

    def _sqlite_insert(*_a, **_k):
        return _Dummy()

    sqlite_module.insert = _sqlite_insert
    sys.modules["sqlalchemy"] = mock_sa
    sys.modules["sqlalchemy.orm"] = mock_sa.orm
    sys.modules["sqlalchemy.dialects"] = types.ModuleType("sqlalchemy.dialects")
    sys.modules["sqlalchemy.dialects.sqlite"] = sqlite_module

# werkzeug.security нужен только при импорте app.dal.db; подменяем, если отсутствует.
try:
    from werkzeug import security as _wz_security  # type: ignore
except Exception:  # pragma: no cover - защитный путь
    mock_security = types.ModuleType("werkzeug.security")

    def _placeholder(value: str) -> str:
        return value

    mock_security.generate_password_hash = _placeholder
    sys.modules["werkzeug.security"] = mock_security

try:
    from werkzeug import exceptions as _wz_exceptions  # type: ignore
except Exception:  # pragma: no cover - защитный путь
    mock_exceptions = types.ModuleType("werkzeug.exceptions")

    class _DummyHTTPException(Exception):
        code = 500

    mock_exceptions.HTTPException = _DummyHTTPException
    sys.modules["werkzeug.exceptions"] = mock_exceptions

# telegram.Bot используется только при импорте; подменяем, если библиотека недоступна.
try:
    import telegram  # type: ignore
except ImportError:  # pragma: no cover - защитный путь
    mock_telegram = types.ModuleType("telegram")

    class _DummyBot:
        def __init__(self, *a, **k):
            pass

        def send_message(self, *a, **k):
            return types.SimpleNamespace(message_id=0)

    mock_telegram.Bot = _DummyBot
    sys.modules["telegram"] = mock_telegram
