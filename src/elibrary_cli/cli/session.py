"""``elibrary auth ...`` (session, cookies, captcha hand-off, login) and ``elibrary cache ...``.

The captcha is never solved by the program: ``auth captcha`` opens a visible browser with the CLI's
session and waits until *the user* passes the check; ``auth cookies`` imports cookies the user
copied from their own browser. Passwords are read from ``ELIBRARY_PASSWORD`` or a hidden prompt and
are never printed, logged or stored.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

import typer

from elibrary_cli.cli.common import (
    FORMAT_HELP,
    OutputFormat,
    echo_err,
    emit,
    handle_errors,
    open_client,
    read_value,
    run,
)
from elibrary_cli.errors import CAPTCHA_HELP, DependencyMissing

auth_app = typer.Typer(
    help="Сессия eLIBRARY.RU: состояние, проверка, импорт cookies, капча, вход и выход.",
    no_args_is_help=True,
)
cache_app = typer.Typer(help="Локальный кэш страниц и полных текстов.", no_args_is_help=True)

Fmt = Annotated[OutputFormat | None, typer.Option("--format", "-f", help=FORMAT_HELP, show_default=False)]

COOKIES_HOWTO = (
    "Как получить cookies: откройте https://elibrary.ru в своём браузере (при необходимости пройдите "
    "проверку), нажмите F12 → Консоль, выполните copy(document.cookie) и передайте строку: "
    "elibrary auth cookies \"<строка>\" (или @файл, или - для чтения из stdin). User-Agent браузера: "
    "navigator.userAgent → --user-agent."
)


@auth_app.command("status")
def auth_status(fmt: Fmt = None) -> None:
    """Состояние сессии без запросов к сайту: cookies (только имена), вход, лимиты, предохранитель."""
    status = run(lambda c: c.status())
    if status.get("breaker_open"):
        if status.get("breaker_gate") == "captcha":
            echo_err("Запросы приостановлены после проверки «Тест Тьюринга». Пройдите её: `elibrary auth captcha` "
                     f"или вручную ({status.get('breaker_url') or 'https://elibrary.ru'}). {COOKIES_HOWTO}")
        elif status.get("breaker_gate") in ("ip_blocked", "forbidden"):
            echo_err(f"Запросы приостановлены после блокировки доступа («{status.get('breaker_gate')}»). Не "
                     "повторяйте запросы: напишите в support@elibrary.ru (укажите ваш IP); кэш доступен через --offline.")
        else:
            echo_err(f"Запросы приостановлены после ответа «{status.get('breaker_gate')}». Войдите в библиотеку в "
                     f"браузере и импортируйте cookies (`elibrary auth cookies`). {COOKIES_HOWTO}")
    emit(status, fmt)


@auth_app.command("check")
def auth_check(
    force: Annotated[bool, typer.Option(
        "--force", help="Снять любую паузу предохранителя, даже после блокировки IP (только по решению человека)")] = False,
    fmt: Fmt = None,
) -> None:
    """Одним запросом проверить сессию (вход, капча). Пауза после капчи снимается, только если после неё
    импортированы cookies с SUserID; пауза после блокировки IP — только с --force."""
    emit(run(lambda c: c.check_session(force=force)), fmt)


@auth_app.command("cookies")
def auth_cookies(
    value: Annotated[str, typer.Argument(
        help="Строка cookies (copy(document.cookie), заголовок Cookie, JSON, cookies.txt), @файл или - (stdin)")],
    user_agent: Annotated[str | None, typer.Option(
        "--user-agent", help="User-Agent браузера, из которого взяты cookies (navigator.userAgent)")] = None,
    merge: Annotated[bool, typer.Option("--merge", help="Добавить к сохранённым cookies, а не заменить их")] = False,
    fmt: Fmt = None,
) -> None:
    """Импортировать cookies из браузера (после прохождения капчи или входа). Значения не выводятся."""
    with handle_errors():
        raw = read_value(value)
    result = run(lambda c: c.set_cookies(raw, user_agent, replace=not merge))
    for warning in result.get("warnings") or []:
        echo_err(f"Внимание: {warning}")
    if result.get("has_session_cookie"):
        echo_err("Если в браузере вы вошли в библиотеку, выполните `elibrary auth check` (1 запрос): без этого "
                 "CLI не знает о входе и не скачивает PDF с eLIBRARY.RU.")
    emit(result, fmt)


@auth_app.command("captcha")
def auth_captcha(
    url: Annotated[str | None, typer.Option(
        "--url", help="Адрес для открытия (по умолчанию страница проверки или главная)", show_default=False)] = None,
    timeout: Annotated[float, typer.Option("--timeout", min=10, help="Сколько ждать, секунд")] = 600,
    channel: Annotated[str | None, typer.Option(
        "--channel", help="Браузер Playwright: msedge, chrome (по умолчанию первый найденный)",
        show_default=False)] = None,
    fmt: Fmt = None,
) -> None:
    """Открыть окно браузера с сессией CLI, чтобы вы сами прошли проверку «Тест Тьюринга»."""
    echo_err("Откроется окно браузера. Пройдите проверку «Тест Тьюринга» сами; после этого окно закроется, "
             "а cookies сессии сохранятся для CLI. Ничего не нажимается автоматически.")
    with handle_errors(), open_client() as client:
        try:
            info = client.browser_session("captcha", url=url, timeout_s=timeout, channel=channel)
        except DependencyMissing as exc:
            echo_err(str(exc))
            captcha_url = client.status().get("breaker_url") or "https://elibrary.ru/defaultx.asp"
            echo_err("\nБез браузера Playwright пройти проверку можно вручную:\n" + CAPTCHA_HELP.format(url=captcha_url))
            raise typer.Exit(exc.exit_code) from None
    if info.message:
        echo_err(info.message)
    emit(info, fmt)
    if info.outcome not in ("solved", "logged_in"):
        raise typer.Exit(2)


@auth_app.command("login")
def auth_login(
    browser: Annotated[bool, typer.Option(
        "--browser", help="Войти в окне браузера (вы вводите пароль сами), cookies сохранятся для CLI")] = False,
    remember: Annotated[bool, typer.Option("--remember/--no-remember", help="«Запомнить меня» на сайте")] = True,
    timeout: Annotated[float, typer.Option("--timeout", min=10, help="Ожидание входа в браузере, секунд")] = 600,
    fmt: Fmt = None,
) -> None:
    """Войти в eLIBRARY.RU. Имя: ELIBRARY_LOGIN или запрос; пароль: ELIBRARY_PASSWORD или скрытый ввод."""
    if browser:
        echo_err("Откроется окно браузера: войдите в eLIBRARY.RU сами; cookies сохранятся для CLI.")
        info = run(lambda c: c.browser_session("login", timeout_s=timeout))
    else:
        login = (os.environ.get("ELIBRARY_LOGIN") or "").strip()
        if not login:
            login = typer.prompt("Имя пользователя eLIBRARY.RU", err=True).strip()
        password = os.environ.get("ELIBRARY_PASSWORD") or ""
        if not password:
            password = typer.prompt("Пароль", hide_input=True, err=True)
        info = run(lambda c: c.login(login, password, remember=remember))
    if info.message:
        echo_err(info.message)
    emit(info, fmt)
    if browser and info.outcome != "logged_in":
        raise typer.Exit(2)


@auth_app.command("logout")
def auth_logout(
    keep: Annotated[bool, typer.Option(
        "--keep", help="Сохранить данные для входа на сайте (как «ОК» в вопросе сайта при выходе): "
                       "запомненный вход восстановится при следующем запросе")] = False,
    fmt: Fmt = None,
) -> None:
    """Выйти из учётной записи (end_session.asp, по умолчанию с забыванием запомненного входа)."""
    info = run(lambda c: c.logout(remember=keep))
    if info.message:
        echo_err(info.message)
    emit(info, fmt)


@auth_app.command("clear")
def auth_clear(fmt: Fmt = None) -> None:
    """Забыть cookies, учётную запись, текущий запрос и User-Agent (без запросов к сайту)."""
    result = run(lambda c: c.clear_session())
    echo_err("Сессия очищена.")
    emit(result, fmt)


@cache_app.command("stats")
def cache_stats(fmt: Fmt = None) -> None:
    """Размер кэша страниц, полных текстов и скачанных PDF."""
    emit(run(lambda c: c.cache_stats()), fmt)


@cache_app.command("clear")
def cache_clear(
    fulltext: Annotated[bool, typer.Option("--fulltext", help="Также удалить извлечённые полные тексты")] = False,
    external: Annotated[bool, typer.Option(
        "--external", help="Также удалить кэш внешних API (Crossref, OpenAlex, ...)")] = False,
    fmt: Fmt = None,
) -> None:
    """Очистить кэш страниц (скачанные PDF сохраняются)."""
    result: dict[str, Any] = run(lambda c: c.clear_cache(fulltext=fulltext, external=external))
    emit(result, fmt)
