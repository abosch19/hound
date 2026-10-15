"""Línea de comandos: `hound <orden>`."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import logging.handlers
import multiprocessing as mp
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import __version__, db
from .config import Config, default_config_path, load_config

log = logging.getLogger("hound")


def setup_logging(cfg: Config, name: str, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg.logs_path.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(cfg.logs_path / f"{name}.log", maxBytes=10_000_000,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    for noisy in ("httpx", "huggingface_hub", "watchdog", "mcp", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _fmt_ts(ts) -> str:
    return dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else "-"


# --- órdenes -----------------------------------------------------------------------

CONFIG_TEMPLATE = """\
# Configuración de Hound. Tras cambiarla: Restart-Service Hound

[share]
# Ruta local de la carpeta compartida en este servidor
path = '{share}'
# Ruta UNC con la que la ven los usuarios (se usa en los resultados)
unc = '{unc}'
# Nombre del recurso compartido SMB: si se indica, se aplican también sus permisos
name = '{share_name}'

[server]
host = "0.0.0.0"
port = {port}
# Para HTTPS, rutas a certificado y clave PEM:
# tls_cert = 'C:\\ProgramData\\Hound\\cert.pem'
# tls_key = 'C:\\ProgramData\\Hound\\key.pem'

[index]
# Carpetas (relativas a la carpeta compartida) que no se indexan ni se pueden consultar,
# por ejemplo las que no deben llegar a Claude:
exclude = []
# exclude = ["RRHH", "Administración/Nóminas"]
rescan_minutes = 360
extract_workers = 2

[index.schedule]
# En horario laboral el indexador va despacio ("slow") o espera a la noche si hay mucho
# trabajo pendiente ("pause"). "normal" no limita.
busy_hours = "08:00-19:00"
busy_days = "mon-fri"
busy_mode = "slow"

[ocr]
enabled = true
languages = "spa+eng"
{tessdata_line}
[cad]
# Para leer el contenido de los DWG instala ODA File Converter (gratuito) y, si no está en la
# ruta por defecto, indica aquí el ejecutable:
# oda_converter = 'C:\\Program Files\\ODA\\ODAFileConverter 26.4.0\\ODAFileConverter.exe'

[permissions]
# "ntfs": cada usuario solo ve lo que puede abrir en el disco. "none": todos ven todo.
mode = "{perm_mode}"
"""


def cmd_init(args) -> int:
    path = Path(args.config) if args.config else default_config_path()
    if path.exists() and not args.force:
        print(f"Ya existe {path}. Usa --force para sobrescribirlo.")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    unc = args.unc
    if not unc and args.share_name:
        import socket
        unc = f"\\\\{socket.gethostname()}\\{args.share_name}"
    tessdata = Path(args.tessdata) if args.tessdata else None
    path.write_text(CONFIG_TEMPLATE.format(
        share=args.share, unc=unc or "", share_name=args.share_name or "", port=args.port,
        tessdata_line=f"tessdata = '{tessdata}'\n" if tessdata else "",
        perm_mode="ntfs" if sys.platform == "win32" else "none",
    ), encoding="utf-8")
    print(f"Configuración creada en {path}")
    return 0


def cmd_model(args, cfg: Config) -> int:
    from .embed import download_model, load_embedder

    if args.action == "download":
        print(f"Descargando {cfg.model.repo} ({cfg.model.file}) en {cfg.models_path} …")
        download_model(cfg.model, cfg.models_path)
    emb = load_embedder(cfg.model, cfg.models_path)
    v = emb.embed_query("prueba")
    print(f"Modelo listo: {emb.name} (dimensión {len(v)})")
    return 0


def _make_indexer(cfg: Config):
    from .embed import load_embedder
    from .indexer import Indexer
    from .security import get_security

    return Indexer(cfg, load_embedder(cfg.model, cfg.models_path), get_security(cfg.permissions.mode))


def cmd_index(args, cfg: Config) -> int:
    setup_logging(cfg, "indexer", args.verbose)
    ix = _make_indexer(cfg)
    if args.watch:
        stop = threading.Event()
        try:
            ix.run_forever(stop)
        except KeyboardInterrupt:
            stop.set()
        return 0
    st = ix.scan()
    print(f"Escaneo: {st.seen} ficheros, {st.new} nuevos, {st.changed} modificados, {st.deleted} borrados")
    total = 0
    t0 = time.time()
    try:
        while True:
            n = ix.process_pending(max_files=100)
            if not n:
                break
            total += n
            pending = ix.conn.execute("SELECT count(*) FROM files WHERE status = 'pending'").fetchone()[0]
            print(f"  {total} procesados, {pending} pendientes ({time.time() - t0:.0f} s)", flush=True)
    finally:
        ix.close()
    print(f"Listo: {total} ficheros procesados en {time.time() - t0:.0f} s")
    return 0


def _make_search(cfg: Config):
    from .embed import load_embedder
    from .search import SearchIndex
    from .security import get_security

    if not cfg.db_path.exists():
        db.init_db(cfg.db_path).close()
    return SearchIndex(cfg, load_embedder(cfg.model, cfg.models_path, threads=2), get_security(cfg.permissions.mode))


def cmd_serve(args, cfg: Config) -> int:
    setup_logging(cfg, "server", args.verbose)
    serve(cfg)
    return 0


def serve(cfg: Config) -> None:
    import uvicorn

    from .auth import TokenStore
    from .server import build_app

    db.init_db(cfg.db_path).close()
    index = _make_search(cfg)
    index.start_refresher()
    app = build_app(index, TokenStore(cfg.db_path))
    ssl = {}
    if cfg.server.tls_cert and cfg.server.tls_key:
        ssl = {"ssl_certfile": cfg.server.tls_cert, "ssl_keyfile": cfg.server.tls_key}
    scheme = "https" if ssl else "http"
    log.info("Servidor MCP en %s://%s:%d/mcp", scheme, cfg.server.host, cfg.server.port)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_config=None, **ssl)


def _indexer_process(config_path: str, verbose: bool, parent_pid: int) -> None:
    import psutil

    cfg = load_config(config_path or None)
    setup_logging(cfg, "indexer", verbose)
    stop = threading.Event()

    def watch_parent():  # si el servicio muere, el indexador no se queda huérfano
        while not stop.wait(5):
            if not psutil.pid_exists(parent_pid):
                log.warning("El proceso principal ha terminado: se detiene el indexador")
                stop.set()

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        _make_indexer(cfg).run_forever(stop)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("El indexador ha fallado")
        raise


def cmd_run(args, cfg: Config) -> int:
    """Modo servicio: indexador en un proceso aparte (se reinicia si falla) + servidor MCP."""
    setup_logging(cfg, "server", args.verbose)
    db.init_db(cfg.db_path).close()
    stop = threading.Event()
    ctx = mp.get_context("spawn")

    def supervise():
        delay = 5
        while not stop.is_set():
            # No es "daemon": necesita lanzar sus propios procesos de extracción.
            p = ctx.Process(target=_indexer_process, args=(cfg.config_path, args.verbose, os.getpid()),
                            name="hound-indexer")
            started = time.monotonic()
            p.start()
            while p.is_alive() and not stop.is_set():
                p.join(1)
            if stop.is_set():
                p.terminate()
                p.join(10)
                return
            log.error("El indexador terminó (código %s). Reinicio en %d s", p.exitcode, delay)
            delay = 5 if time.monotonic() - started > 600 else min(delay * 2, 600)
            stop.wait(delay)

    t = threading.Thread(target=supervise, name="hound-supervisor", daemon=True)
    t.start()
    try:
        serve(cfg)
    finally:
        stop.set()
        t.join(15)
    return 0


def cmd_token(args, cfg: Config) -> int:
    from .auth import create_token, list_tokens, revoke
    from .security import get_security

    conn = db.init_db(cfg.db_path)
    if args.action == "create":
        name, token = create_token(conn, get_security(cfg.permissions.mode), args.account, args.label or "")
        host = args.host or _guess_host()
        scheme = "https" if cfg.server.tls_cert else "http"
        print(f"Token creado para {name}. Guárdalo ahora: no se vuelve a mostrar.\n")
        print(f"  {token}\n")
        print("Para conectarlo desde Claude Code en el ordenador del usuario (con la VPN conectada):\n")
        print(f'  claude mcp add --transport http --scope user hound {scheme}://{host}:{cfg.server.port}/mcp '
              f'--header "Authorization: Bearer {token}"')
    elif args.action == "list":
        rows = list_tokens(conn)
        if not rows:
            print("No hay tokens.")
        for r in rows:
            state = f"revocado {_fmt_ts(r['revoked_at'])}" if r["revoked_at"] else "activo"
            print(f"{r['id']:>4}  {r['name']:<30} {r['label'] or '':<20} creado {_fmt_ts(r['created_at'])}  "
                  f"último uso {_fmt_ts(r['last_used'])}  {state}")
    elif args.action == "revoke":
        n = revoke(conn, args.target)
        print(f"{n} token(s) revocado(s). Dejan de funcionar en menos de 30 s.")
    return 0


def _guess_host() -> str:
    import socket

    return socket.gethostname()


def cmd_status(args, cfg: Config) -> int:
    if not cfg.db_path.exists():
        print(f"Todavía no hay índice en {cfg.db_path}")
        return 1
    conn = db.connect(cfg.db_path, readonly=True)
    print(f"Hound {__version__}")
    print(f"Configuración: {cfg.config_path or '(por defecto)'}")
    print(f"Índice: {cfg.db_path} ({cfg.db_path.stat().st_size / 1e6:.0f} MB)")
    print(f"Carpeta: {cfg.share.path}")
    print(f"Modelo: {db.get_meta(conn, 'model', '-')}")
    for r in conn.execute("SELECT status, count(*) n FROM files GROUP BY status"):
        print(f"  {r['status']:<8} {r['n']:>9} ficheros")
    segs = conn.execute("SELECT max(id) FROM segments").fetchone()[0] or 0
    print(f"  fragmentos ~{segs}")
    print(f"Último escaneo completo: {_fmt_ts(db.get_meta(conn, 'last_full_scan'))}")
    hb = db.get_meta(conn, "indexer_heartbeat")
    alive = hb and time.time() - float(hb) < 600
    print(f"Indexador: {'activo' if alive else 'sin actividad reciente'} (último latido {_fmt_ts(hb)})")
    print("Por tipo:")
    for r in conn.execute("SELECT kind, count(*) n FROM files GROUP BY kind ORDER BY n DESC"):
        print(f"  {r['kind']:<11} {r['n']:>9}")
    return 0


def cmd_errors(args, cfg: Config) -> int:
    conn = db.connect(cfg.db_path, readonly=True)
    q = "SELECT path, status, error FROM files WHERE error IS NOT NULL"
    if not args.all:
        q += " AND status = 'error'"
    for r in conn.execute(q + " ORDER BY path LIMIT ?", (args.limit,)):
        print(f"[{r['status']}] {r['path']}\n    {r['error']}")
    return 0


def cmd_reindex(args, cfg: Config) -> int:
    if args.all:
        conn = db.init_db(cfg.db_path)
        conn.close()
        backup = cfg.db_path.with_suffix(f".{dt.datetime.now():%Y%m%d-%H%M%S}.bak")
        _backup(cfg.db_path, backup)
        print(f"Copia del índice anterior en {backup}")
        conn = db.connect(cfg.db_path)
        db.reset_index(conn)
        conn.close()
        print("Índice vaciado (se conservan usuarios, tokens y registro de consultas). "
              "El servicio lo reconstruirá; también puedes ejecutar 'hound index'.")
        return 0
    conn = db.init_db(cfg.db_path)
    from .paths import parse_user_path

    if args.path:
        rel = parse_user_path(args.path, cfg)
        cur = conn.execute("UPDATE files SET status = 'pending' WHERE path = ? OR (path >= ? AND path < ?)",
                           (rel, rel + "/", rel + "0") if rel else ("", "", "\uffff"))
    elif args.errors:
        cur = conn.execute("UPDATE files SET status = 'pending' WHERE status = 'error'")
    else:
        print("Indica --all, --errors o --path RUTA")
        return 2
    conn.commit()
    print(f"{cur.rowcount} ficheros marcados para reindexar")
    return 0


def _backup(src: Path, dest: Path) -> None:
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dest))
    with d:
        s.backup(d)
    d.close()
    s.close()


def cmd_backup(args, cfg: Config) -> int:
    dest = Path(args.dest)
    if dest.is_dir():
        dest = dest / f"hound-index-{dt.datetime.now():%Y%m%d-%H%M%S}.sqlite"
    _backup(cfg.db_path, dest)
    print(f"Copia creada: {dest}")
    return 0


def cmd_log(args, cfg: Config) -> int:
    conn = db.connect(cfg.db_path, readonly=True)
    q, params = "SELECT * FROM query_log", []
    if args.user:
        q += " WHERE user_name LIKE ?"
        params.append(f"%{args.user}%")
    for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", (*params, args.limit)):
        print(f"{_fmt_ts(r['ts'])}  {r['user_name']:<25} {r['tool']:<14} {r['args']}  "
              f"→ {r['results'] if r['results'] is not None else '-'} ({r['ms']} ms)")
    return 0


def cmd_search(args, cfg: Config) -> int:
    from .search import User
    from .security import get_security

    index = _make_search(cfg)
    if args.as_user:
        name, sid = get_security(cfg.permissions.mode).lookup_account(args.as_user)
    else:
        name, sid = "admin", ""
        if index.security.enforced:
            print("Indica --as USUARIO para buscar con los permisos de esa cuenta.")
            return 2
    t0 = time.monotonic()
    results = index.search(User(name, sid), args.query, args.folder, args.type, limit=args.limit)
    print(f"{len(results)} resultados en {(time.monotonic() - t0) * 1000:.0f} ms (como {name})\n")
    for r in results:
        print(f"{r['score']:.3f}  {r['path']}\n       {r['location']} · {r['modified']}\n       {r['excerpt'][:300]}\n")
    return 0


def cmd_doctor(args, cfg: Config) -> int:
    from .extract import ExtractOptions, capabilities
    from .extract.ocr import find_tesseract

    ok = True

    def line(good: bool, msg: str, hint: str = ""):
        nonlocal ok
        ok &= good
        print(("  OK   " if good else "  FALLO ") + msg + (f"\n         → {hint}" if hint and not good else ""))

    print(f"Hound {__version__} · Python {sys.version.split()[0]} · {sys.platform}")
    line(bool(cfg.config_path), f"Configuración: {cfg.config_path or default_config_path()}", "Ejecuta: hound init")
    line(bool(cfg.share.path) and os.path.isdir(cfg.share.path), f"Carpeta compartida: {cfg.share.path}",
         "Revisa share.path y que la cuenta del servicio pueda leerla")
    if cfg.share.path and os.path.isdir(cfg.share.path):
        try:
            next(os.scandir(cfg.share.path), None)
            line(True, "Lectura de la carpeta")
        except OSError as exc:
            line(False, f"Lectura de la carpeta: {exc}")
    from .embed import model_dir

    md = model_dir(cfg.model, cfg.models_path)
    line((md / cfg.model.file).exists(), f"Modelo: {md / cfg.model.file}", "Ejecuta: hound model download")
    tess = find_tesseract(cfg.ocr)
    line(tess is not None or not cfg.ocr.enabled, f"Tesseract (OCR): {tess or 'no encontrado'}",
         "Instálalo o desactiva [ocr] enabled")
    caps = capabilities(ExtractOptions(ocr=cfg.ocr, cad=cfg.cad))
    print(f"  INFO DWG: {'con ODA File Converter' if 'oda' in caps else 'solo por nombre (ODA File Converter no instalado)'}")
    if cfg.permissions.mode == "ntfs":
        try:
            from .security import get_security

            sec = get_security("ntfs")
            sd = sec.read_sd(cfg.share.path)
            line(bool(sd), "Lectura de permisos NTFS")
            if cfg.share.name:
                sec.share_sd(cfg.share.name)
                line(True, f"Permisos del recurso compartido '{cfg.share.name}'")
        except Exception as exc:  # noqa: BLE001
            line(False, f"Permisos NTFS: {exc}")
    else:
        print("  AVISO permissions.mode = none: todos los usuarios con token ven todo el índice")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hound", description="Buscador semántico de la carpeta compartida (MCP)")
    p.add_argument("--config", help="Ruta de config.toml (por defecto %%ProgramData%%\\Hound\\config.toml)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"hound {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Crea config.toml")
    s.add_argument("--share", required=True, help="Ruta local de la carpeta compartida")
    s.add_argument("--unc", default="", help="Ruta UNC (\\\\SERVIDOR\\Recurso)")
    s.add_argument("--share-name", default="", help="Nombre del recurso compartido SMB")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--tessdata", default="")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("model", help="Descarga o comprueba el modelo de embeddings")
    s.add_argument("action", choices=["download", "check"])

    s = sub.add_parser("index", help="Indexa ahora (en primer plano)")
    s.add_argument("--watch", action="store_true", help="Sigue vigilando cambios")

    sub.add_parser("serve", help="Solo el servidor MCP")
    sub.add_parser("run", help="Modo servicio: indexador + servidor MCP")

    s = sub.add_parser("token", help="Tokens personales del MCP")
    tsub = s.add_subparsers(dest="action", required=True)
    t = tsub.add_parser("create", help="Crea un token para una cuenta de Windows")
    t.add_argument("account", help="Cuenta: usuario, EQUIPO\\usuario o DOMINIO\\usuario")
    t.add_argument("--label", default="")
    t.add_argument("--host", default="", help="Nombre o IP del servidor en la VPN (para el comando de ejemplo)")
    tsub.add_parser("list")
    t = tsub.add_parser("revoke", help="Revoca un token (por id) o todos los de un usuario")
    t.add_argument("target")

    sub.add_parser("status", help="Estado del índice")
    s = sub.add_parser("errors", help="Ficheros que no se pudieron leer")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--all", action="store_true", help="Incluye avisos de ficheros indexados")

    s = sub.add_parser("reindex", help="Vuelve a indexar")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="Borra el índice (con copia) y lo rehace")
    g.add_argument("--errors", action="store_true", help="Reintenta los ficheros con error")
    g.add_argument("--path", help="Reindexa un fichero o carpeta")

    s = sub.add_parser("backup", help="Copia de seguridad en caliente del índice")
    s.add_argument("dest")

    s = sub.add_parser("log", help="Registro de consultas")
    s.add_argument("--user", default="")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("search", help="Busca desde la consola (pruebas)")
    s.add_argument("query")
    s.add_argument("--as", dest="as_user", default="", help="Buscar con los permisos de esta cuenta")
    s.add_argument("--folder", default=None)
    s.add_argument("--type", default=None)
    s.add_argument("--limit", type=int, default=10)

    sub.add_parser("doctor", help="Comprueba la instalación")

    args = p.parse_args(argv)
    if args.cmd == "init":
        return cmd_init(args)
    cfg = load_config(args.config)
    if args.cmd not in ("index", "serve", "run"):
        logging.basicConfig(level=logging.WARNING)
    handler = {
        "model": cmd_model, "index": cmd_index, "serve": cmd_serve, "run": cmd_run, "token": cmd_token,
        "status": cmd_status, "errors": cmd_errors, "reindex": cmd_reindex, "backup": cmd_backup,
        "log": cmd_log, "search": cmd_search, "doctor": cmd_doctor,
    }[args.cmd]
    return handler(args, cfg) or 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
