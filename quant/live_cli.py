"""CLI live-режима (фазы 6–7).

Локально (без Docker): `quant live run` — коллектор + сигналы + API + бот в одном процессе, шина в памяти.
Прод (docker-compose): отдельные процессы `quant collector run`, `quant signals run`, `quant api serve`,
`quant bot run`, общая шина Redis Streams (QUANT_REDIS_URL).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

log = logging.getLogger(__name__)


def _symbols(args: argparse.Namespace) -> list[str] | None:
    return [s.strip().upper() for s in args.symbols.split(",")] if getattr(args, "symbols", None) else None


def _metrics(port: int | None) -> None:
    if port:
        from prometheus_client import start_http_server

        start_http_server(port)


async def _serve_api(app, host: str, port: int) -> None:  # noqa: ANN001
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info", loop="asyncio"))
    await server.serve()


def _live_run(args: argparse.Namespace) -> int:
    from collector.binance_ws import BinanceCollector
    from quant import instruments as I
    from quant.bus import get_bus
    from quant.config import get_settings
    from api.app import create_app
    from signals.service import SignalService

    bus = get_bus()
    syms = _symbols(args)
    insts = I.select(syms) if syms else list(I.instruments())
    service = SignalService(bus, strategy=args.strategy, symbols=[i.hh_symbol for i in insts])
    app = create_app(bus=bus, service=service, strategy=args.strategy)
    _metrics(args.metrics_port)

    async def main():
        tasks = [service.start(), _serve_api(app, args.host, args.port)]
        if not args.no_collector:
            from collector.binance_rest import BinanceRestPoller, run_auto

            if args.collector_mode == "ws":
                tasks.append(BinanceCollector(insts, bus).run())
            elif args.collector_mode == "rest":
                tasks.append(BinanceRestPoller(insts, bus).run())
            else:
                tasks.append(run_auto(insts, bus))
        if not args.no_bot and get_settings().telegram_token:
            from bot.telegram import TelegramBot

            tasks.append(TelegramBot(bus).run())
        await asyncio.gather(*tasks)

    asyncio.run(main())
    return 0


def _live_start(args: argparse.Namespace) -> int:
    from quant.live import background

    argv = [sys.executable, "-m", "quant", *(["-v"] if args.verbose else []), "live", "run",
            "--strategy", args.strategy, "--host", args.host, "--port", str(args.port),
            "--collector-mode", args.collector_mode]
    if args.symbols:
        argv += ["--symbols", args.symbols]
    if args.no_collector:
        argv.append("--no-collector")
    if args.no_bot:
        argv.append("--no-bot")
    if args.metrics_port:
        argv += ["--metrics-port", str(args.metrics_port)]
    return background.start(argv, args.host, args.port)


def _live_stop(args: argparse.Namespace) -> int:
    from quant.live import background

    return background.stop()


def _live_status(args: argparse.Namespace) -> int:
    from quant.live import background

    return background.status()


def _collector(args: argparse.Namespace) -> int:
    from collector.binance_ws import BinanceCollector
    from collector.bybit_ws import BybitBackup
    from quant import instruments as I
    from quant.bus import get_bus

    bus = get_bus()
    syms = _symbols(args)
    insts = I.select(syms) if syms else list(I.instruments())
    _metrics(args.metrics_port)

    async def main():
        from collector.binance_rest import BinanceRestPoller, run_auto

        primary = BinanceCollector(insts, bus)
        if args.mode == "ws":
            tasks = [primary.run()]
        elif args.mode == "rest":
            tasks = [BinanceRestPoller(insts, bus, stats=primary.stats).run()]
        else:
            tasks = [run_auto(insts, bus, stats=primary.stats)]
        if not args.no_bybit:
            tasks.append(BybitBackup(insts, bus, primary.stats).run())
        await asyncio.gather(*tasks)

    asyncio.run(main())
    return 0


def _signals(args: argparse.Namespace) -> int:
    from quant.bus import get_bus
    from signals.service import SignalService

    _metrics(args.metrics_port)
    asyncio.run(SignalService(get_bus(), strategy=args.strategy, symbols=_symbols(args)).start())
    return 0


def _api(args: argparse.Namespace) -> int:
    import uvicorn

    from api.app import create_app

    uvicorn.run(create_app(strategy=args.strategy), host=args.host, port=args.port)
    return 0


def _bot(args: argparse.Namespace) -> int:
    from bot.telegram import TelegramBot
    from quant.bus import get_bus

    asyncio.run(TelegramBot(get_bus()).run())
    return 0


def _create_user(args: argparse.Namespace) -> int:
    from api.auth import create_user

    pw = args.password or getpass.getpass("Пароль: ")
    create_user(args.username, pw)
    print(f"Пользователь {args.username} создан (или пароль обновлён).")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("live", help="всё в одном процессе: коллектор + сигналы + API + бот")
    s = p.add_subparsers(dest="cmd", required=True)
    for name, func, help_ in (("run", _live_run, "в текущем терминале (Ctrl+C — остановить)"),
                              ("start", _live_start, "то же в фоне; лог — data/logs/live.log")):
        r = s.add_parser(name, help=help_)
        r.add_argument("--strategy", default="confluence_v1")
        r.add_argument("--symbols")
        r.add_argument("--host", default="127.0.0.1")
        r.add_argument("--port", type=int, default=8000)
        r.add_argument("--no-collector", action="store_true")
        r.add_argument("--collector-mode", choices=["auto", "ws", "rest"], default="auto")
        r.add_argument("--no-bot", action="store_true")
        r.add_argument("--metrics-port", type=int)
        r.set_defaults(func=func)
    s.add_parser("stop", help="остановить фоновый процесс").set_defaults(func=_live_stop)
    s.add_parser("status", help="запущен ли фоновый процесс").set_defaults(func=_live_status)

    c = sub.add_parser("collector", help="коллектор Binance WS (+ резерв Bybit)")
    cs = c.add_subparsers(dest="cmd", required=True)
    cr = cs.add_parser("run")
    cr.add_argument("--symbols")
    cr.add_argument("--no-bybit", action="store_true")
    cr.add_argument("--mode", choices=["auto", "ws", "rest"], default="auto",
                    help="auto: WebSocket, при отсутствии данных — REST-поллинг")
    cr.add_argument("--metrics-port", type=int, default=9101)
    cr.set_defaults(func=_collector)

    g = sub.add_parser("signals", help="сервис сигналов")
    gs = g.add_subparsers(dest="cmd", required=True)
    gr = gs.add_parser("run")
    gr.add_argument("--strategy", default="confluence_v1")
    gr.add_argument("--symbols")
    gr.add_argument("--metrics-port", type=int, default=9102)
    gr.set_defaults(func=_signals)

    a = sub.add_parser("api", help="FastAPI (REST + WebSocket) + раздача SPA")
    as_ = a.add_subparsers(dest="cmd", required=True)
    ar = as_.add_parser("serve")
    ar.add_argument("--strategy", default="confluence_v1")
    ar.add_argument("--host", default="0.0.0.0")
    ar.add_argument("--port", type=int, default=8000)
    ar.set_defaults(func=_api)

    b = sub.add_parser("bot", help="Telegram-бот")
    bs = b.add_subparsers(dest="cmd", required=True)
    bs.add_parser("run").set_defaults(func=_bot)

    u = sub.add_parser("auth", help="пользователь веб-интерфейса (пароль)")
    us = u.add_subparsers(dest="cmd", required=True)
    uc = us.add_parser("create-user")
    uc.add_argument("--username", required=True)
    uc.add_argument("--password", help="если не указан — спросит интерактивно")
    uc.set_defaults(func=_create_user)
