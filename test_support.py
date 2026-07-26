"""
Test scaffolding: import the bot's modules without its third-party dependencies.

aiogram, asyncpg and litellm can't be installed in every dev environment (and
none of them are needed to exercise the bot's own logic), so `install_stubs()`
registers just enough of each in sys.modules for `import bot` / `import agent`
to succeed. Real packages always win — a stub is only installed if the import
genuinely fails.

Nothing here fakes behaviour under test. The stubs cover module-level scaffolding
only (decorators, base classes, the magic filter object); tests pass their own
duck-typed objects into the functions they exercise.
"""
import os
import sys
import types


def _missing(name: str) -> bool:
    try:
        __import__(name)
        return False
    except ImportError:
        return True


def _stub_dotenv():
    mod = types.ModuleType("dotenv")
    mod.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = mod


def _stub_asyncpg():
    mod = types.ModuleType("asyncpg")

    class Pool: ...
    class Connection: ...

    mod.Pool = Pool
    mod.Connection = Connection

    async def create_pool(*a, **k):
        raise RuntimeError("asyncpg is stubbed out in tests")

    mod.create_pool = create_pool
    sys.modules["asyncpg"] = mod


class _Magic:
    """
    Stand-in for aiogram's `F` magic-filter object.

    Every attribute access, call, comparison and boolean operator returns
    another _Magic, so any filter expression bot.py builds at import time
    evaluates without error.
    """
    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return _Magic()

    def __call__(self, *a, **k):
        return _Magic()

    __or__ = __and__ = __invert__ = __eq__ = __ne__ = __call__

    def __hash__(self):
        return id(self)

    def __bool__(self):
        return True


def _stub_aiogram():
    aiogram = types.ModuleType("aiogram")

    class Bot:
        def __init__(self, *a, **k):
            self.id = 0

    class Dispatcher:
        def __init__(self, *a, **k): ...
        def include_router(self, *a, **k): ...

    class _Observer:
        """Router event observer: usable as a decorator and as a registrar."""
        def __call__(self, *filters, **k):
            def deco(fn):
                return fn
            return deco

        def outer_middleware(self, *a, **k): ...
        def middleware(self, *a, **k): ...
        def register(self, *a, **k): ...

    class Router:
        def __init__(self, *a, **k):
            self.message = _Observer()
            self.callback_query = _Observer()
            self.edited_message = _Observer()

        def include_router(self, *a, **k): ...

    class BaseMiddleware:
        async def __call__(self, handler, event, data):
            return await handler(event, data)

    aiogram.Bot = Bot
    aiogram.Dispatcher = Dispatcher
    aiogram.Router = Router
    aiogram.BaseMiddleware = BaseMiddleware
    aiogram.F = _Magic()
    aiogram.__version__ = "3.0.0-stub"

    # ── aiogram.types ──────────────────────────────────────────────────────
    types_mod = types.ModuleType("aiogram.types")

    class _Obj:
        """Permissive data holder standing in for aiogram's pydantic models."""
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

        def __getattr__(self, item):
            if item.startswith("__"):
                raise AttributeError(item)
            return None

    for name in (
        "TelegramObject", "Message", "CallbackQuery", "Chat", "User",
        "MessageEntity", "InlineKeyboardMarkup", "InlineKeyboardButton",
        "ReactionTypeEmoji", "FSInputFile", "BotCommand",
        "BotCommandScopeAllPrivateChats", "BotCommandScopeAllGroupChats",
        "BotCommandScopeAllChatAdministrators", "ChatPermissions",
        "InputRichMessage", "Update",
    ):
        setattr(types_mod, name, type(name, (_Obj,), {}))

    aiogram.types = types_mod

    # ── aiogram.filters ────────────────────────────────────────────────────
    filters_mod = types.ModuleType("aiogram.filters")

    class BaseFilter:
        async def __call__(self, *a, **k):
            return True

    class Command:
        def __init__(self, *a, **k):
            self.commands = a

    filters_mod.BaseFilter = BaseFilter
    filters_mod.Command = Command
    filters_mod.CommandStart = Command

    cbd_mod = types.ModuleType("aiogram.filters.callback_data")

    class CallbackData:
        prefix = ""

        def __init_subclass__(cls, prefix="", **kw):
            super().__init_subclass__(**kw)
            cls.prefix = prefix

        def __init__(self, **kw):
            self.__dict__.update(kw)

        def pack(self):
            vals = ":".join(str(v) for v in self.__dict__.values())
            return f"{self.prefix}:{vals}"

        @classmethod
        def filter(cls, *a, **k):
            return _Magic()

    cbd_mod.CallbackData = CallbackData
    filters_mod.callback_data = cbd_mod

    aiogram.filters = filters_mod

    # ── aiogram.utils.chat_action ──────────────────────────────────────────
    utils_mod = types.ModuleType("aiogram.utils")
    chat_action_mod = types.ModuleType("aiogram.utils.chat_action")

    class ChatActionSender:
        def __init__(self, *a, **k): ...
        @classmethod
        def typing(cls, **k):
            return cls()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    chat_action_mod.ChatActionSender = ChatActionSender
    utils_mod.chat_action = chat_action_mod
    aiogram.utils = utils_mod

    exc_mod = types.ModuleType("aiogram.exceptions")

    class TelegramAPIError(Exception): ...
    class TelegramForbiddenError(TelegramAPIError): ...
    class TelegramBadRequest(TelegramAPIError): ...

    exc_mod.TelegramAPIError = TelegramAPIError
    exc_mod.TelegramForbiddenError = TelegramForbiddenError
    exc_mod.TelegramBadRequest = TelegramBadRequest
    aiogram.exceptions = exc_mod

    for name, mod in (
        ("aiogram", aiogram),
        ("aiogram.types", types_mod),
        ("aiogram.filters", filters_mod),
        ("aiogram.filters.callback_data", cbd_mod),
        ("aiogram.utils", utils_mod),
        ("aiogram.utils.chat_action", chat_action_mod),
        ("aiogram.exceptions", exc_mod),
    ):
        sys.modules[name] = mod


def install_stubs():
    """Install stubs for any missing third-party dependency. Idempotent."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    # Config validation must not run against a real environment during tests.
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
    os.environ.setdefault("DATABASE_URL", "postgres://localhost/test")
    os.environ.setdefault("ZAI_API_KEY", "test-zai-key")
    os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

    if _missing("dotenv"):
        _stub_dotenv()
    if _missing("asyncpg"):
        _stub_asyncpg()
    if _missing("aiogram"):
        _stub_aiogram()


def read_code(path: str) -> str:
    """
    Source with comments and docstrings stripped.

    Assertions like "this pattern no longer appears" must look at code only —
    the comments explaining what a fix replaced necessarily quote the old
    broken pattern, and would otherwise match themselves.
    """
    import io
    import tokenize

    with open(path, "rb") as f:
        src = f.read()

    try:
        tokens = list(tokenize.tokenize(io.BytesIO(src).readline))
    except tokenize.TokenError:
        return src.decode("utf-8", "replace")

    # Token types after which a bare STRING is a docstring rather than a value.
    # ENCODING must be included: it is the very first token of every file, so
    # without it the MODULE docstring is never recognised as one.
    statement_start = {
        tokenize.ENCODING, tokenize.INDENT, tokenize.DEDENT,
        tokenize.NEWLINE, tokenize.NL,
    }

    out, prev_end, prev_type = [], (1, 0), tokenize.ENCODING
    for tok in tokens:
        if tok.type in (tokenize.COMMENT, tokenize.ENCODING):
            continue
        if tok.type == tokenize.STRING and prev_type in statement_start:
            prev_type = tokenize.NEWLINE  # a docstring ends its own statement
            prev_end = tok.end
            continue
        if tok.start > prev_end:
            out.append(" ")
        out.append(tok.string)
        prev_end = tok.end
        prev_type = tok.type

    return "".join(out)


# ── duck-typed Telegram objects for the tests themselves ────────────────────
class FakeUser:
    def __init__(self, id, is_bot=False, username=None, full_name="Someone"):
        self.id = id
        self.is_bot = is_bot
        self.username = username
        self.full_name = full_name


class FakeEntity:
    def __init__(self, type, offset=0, length=0, user=None):
        self.type = type
        self.offset = offset
        self.length = length
        self.user = user


class FakeChat:
    def __init__(self, id=-100123, type="supergroup"):
        self.id = id
        self.type = type


class FakeMessage:
    def __init__(self, text=None, caption=None, entities=None,
                 caption_entities=None, reply_to_message=None,
                 from_user=None, chat=None):
        self.text = text
        self.caption = caption
        self.entities = entities
        self.caption_entities = caption_entities
        self.reply_to_message = reply_to_message
        self.from_user = from_user or FakeUser(5, full_name="Alex")
        self.chat = chat or FakeChat()
