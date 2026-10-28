"""Exception hierarchy shared by the CLI and the MCP server."""

from __future__ import annotations

CAPTCHA_HELP = (
    "eLIBRARY.RU показал проверку «Тест Тьюринга» (reCAPTCHA): с этого IP/сессии было слишком много "
    "запросов. Пройти её может только человек:\n"
    "  1) выполните `elibrary auth captcha` — откроется окно браузера с вашей сессией (нужен extra "
    "[browser]), пройдите проверку, окно закроется само; или\n"
    "  2) откройте {url} в своём браузере, пройдите проверку, затем в консоли разработчика (F12) "
    "на странице elibrary.ru выполните copy(document.cookie) и передайте строку командой "
    "`elibrary auth cookies \"<строка>\"` (или MCP-инструментом elibrary_set_cookies).\n"
    "Не повторяйте запросы до прохождения проверки — это продлевает блокировку."
)

#: Raised (as :class:`AccessBlocked`) when elibrary.ru answers with its ban page or IIS 403.
BAN_HELP = (
    "elibrary.ru заблокировал доступ для этого IP ({reason}). Новые запросы приостановлены на 24 ч. "
    "Не повторяйте запросы и не пытайтесь снять паузу: напишите в support@elibrary.ru (укажите ваш IP), "
    "не продолжайте с этой сети; кэш доступен через --offline. VPN и зарубежные/облачные IP elibrary "
    "блокирует почти сразу."
)
#: Raised (as :class:`RateLimited`) while the 24 h pause after a ban is on.
BAN_PAUSE_HELP = (
    "Запросы к elibrary.ru приостановлены ещё на ~{minutes} мин: сайт заблокировал доступ для этого IP "
    "({reason}). Не повторяйте запросы и не пытайтесь снять паузу (импорт cookies и проверка сессии её не "
    "снимают): напишите в support@elibrary.ru (укажите ваш IP) и не продолжайте с этой сети; кэш доступен "
    "через --offline."
)
#: Raised (as :class:`IpRestricted`) when elibrary.ru redirects to ip_restricted.asp.
IP_RESTRICTED_HELP = (
    "elibrary.ru ограничил анонимный доступ с этого IP (ip_restricted.asp). Войдите в библиотеку: "
    "`elibrary auth login` или импортируйте cookies вошедшей сессии (`elibrary auth cookies`). "
    "Не используйте VPN."
)


class ElibraryError(Exception):
    """Base class for all errors raised by elibrary-cli."""

    exit_code = 2

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.url = url

    def __str__(self) -> str:
        return self.message


class CaptchaRequired(ElibraryError):
    """elibrary.ru redirected to page_captcha.asp (reCAPTCHA "Тест Тьюринга")."""

    def __init__(self, url: str | None = None, *, captcha_url: str | None = None) -> None:
        self.captcha_url = captcha_url or "https://elibrary.ru/page_captcha.asp"
        super().__init__(CAPTCHA_HELP.format(url=self.captcha_url), url=url)


class AccessBlocked(ElibraryError):
    """The IP address or session was blocked by elibrary.ru (ip_blocked.asp, IIS 403)."""


class LoginRequired(ElibraryError):
    """The requested resource (e.g. a full-text PDF) is available only to signed-in readers."""


class IpRestricted(LoginRequired):
    """elibrary.ru redirected to ip_restricted.asp: anonymous access from this IP is restricted."""


class RateLimited(ElibraryError):
    """The local request budget (ELIBRARY_MAX_PER_HOUR / ELIBRARY_MAX_PER_DAY) is exhausted,
    or the circuit breaker is open after a challenge page."""


class NotFound(ElibraryError):
    """The requested publication, author, organization or journal does not exist."""


class ParseError(ElibraryError):
    """The page layout was not recognised (elibrary.ru may have changed its HTML)."""


class NetworkError(ElibraryError):
    exit_code = 3


class OfflineCacheMiss(ElibraryError):
    """ELIBRARY_OFFLINE=1 is set and the page is not in the cache."""


class DependencyMissing(ElibraryError):
    """An optional extra (pdf, zotero, browser) is not installed."""

    exit_code = 1

    def __init__(self, extra: str, package: str) -> None:
        super().__init__(
            f"Для этой функции нужен пакет {package!r}. Установите extra: "
            f"pip install 'elibrary-cli[{extra}]'  (или uvx --from 'elibrary-cli[{extra}]' ...)"
        )
        self.extra = extra
        self.package = package
