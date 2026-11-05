#!/usr/bin/env python3
"""Siteblock: bloqueia sites pelo arquivo hosts com agenda semanal e liberação temporária.

Funciona no Linux (systemd) e no Windows (Agendador de Tarefas). Dois processos, de propósito:
  enforce  roda como root/SYSTEM, não abre porta nenhuma e só aplica a agenda no hosts.
  serve    roda como seu usuário e oferece a interface em http://127.0.0.1:8377.

Instalação:  sudo python3 siteblock.py install       (Linux)
             python siteblock.py install             (Windows, terminal como administrador)
Remoção:     sudo siteblock uninstall / siteblock uninstall (terminal como administrador)
"""
from __future__ import annotations

import argparse
import contextlib
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl
    import pwd

ENFORCER_NAME = "siteblock-enforcer"
UI_NAME = "siteblock-ui"
LOGO_NAME = "logo-block-site.jpg"

if WINDOWS:
    SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    PROGRAM_DATA = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    DEFAULT_CONFIG_DIR = PROGRAM_DATA / "Siteblock"
    DEFAULT_HOSTS = SYSTEM32 / "drivers" / "etc" / "hosts"
    APP_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Siteblock"
    BIN_PATH = APP_DIR / "siteblock.py"
    CMD_PATH = APP_DIR / "siteblock.cmd"  # o comando "siteblock" no terminal
    SHARE_DIR = APP_DIR
    SHORTCUT_PATH = PROGRAM_DATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Siteblock.url"
    # O log do enforcer fica onde só o administrador escreve: ele roda como SYSTEM.
    ENFORCER_LOG = APP_DIR / "enforcer.log"
    RESTART_HINT = f"Em um terminal aberto como administrador, rode: schtasks /Run /TN {ENFORCER_NAME}"
else:
    DEFAULT_CONFIG_DIR = Path("/var/lib/siteblock")
    DEFAULT_HOSTS = Path("/etc/hosts")
    BIN_PATH = Path("/usr/local/bin/siteblock")
    UNIT_DIR = Path("/etc/systemd/system")
    ENFORCER_UNIT = f"{ENFORCER_NAME}.service"
    UI_UNIT = f"{UI_NAME}.service"
    DESKTOP_PATH = Path("/usr/share/applications/siteblock.desktop")
    SHARE_DIR = Path("/usr/local/share/siteblock")
    RESTART_HINT = f"No terminal, rode: sudo systemctl restart {ENFORCER_NAME}"

CONFIG_DIR = Path(os.environ.get("SITEBLOCK_DIR", DEFAULT_CONFIG_DIR))
HOSTS_PATH = Path(os.environ.get("SITEBLOCK_HOSTS", DEFAULT_HOSTS))
CONFIG_PATH = CONFIG_DIR / "config.json"
LOCK_PATH = CONFIG_DIR / ".lock"
UI_LOG = CONFIG_DIR / "interface.log"  # só no Windows, onde a interface roda sem terminal
PORT = int(os.environ.get("SITEBLOCK_PORT", "8377"))
HOSTS_BACKUP = HOSTS_PATH.with_name(HOSTS_PATH.name + ".siteblock.bak")
EXTENSION_DIR = SHARE_DIR / "extensao"
LOGO_PATH = SHARE_DIR / LOGO_NAME

BEGIN_MARK = "# >>> siteblock (gerenciado automaticamente, não edite este bloco)"
END_MARK = "# <<< siteblock"
ENFORCE_INTERVAL = 2
RESOLVE_TIMEOUT = 2
MIN_UNBLOCK, MAX_UNBLOCK = 1, 15
COOLDOWN_FACTOR = 3  # depois de liberar, fica sem nova liberação por 3x o tempo usado
MINUTES_IN_WEEK = 7 * 1440
TIMESTAMP_FIELDS = ("unblocked_since", "unblocked_until", "cooldown_until")

DAYS = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")  # índice = datetime.weekday()
DAY_NAMES = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
SUBDOMAINS = ("", "www.", "m.")  # o hosts não aceita curinga

LABEL_RE = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_RE = re.compile(rf"(?:{LABEL_RE}\.)+(?:[a-z][a-z0-9-]{{0,61}}[a-z0-9])")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


# ---------------------------------------------------------------- validação


def normalize_domain(raw: str) -> str:
    """Aceita domínio ou URL colada e devolve só o domínio, ou levanta ValueError."""
    text = str(raw).strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = re.split(r"[/?#]", text, maxsplit=1)[0]
    text = text.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    try:
        text = text.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError(f"Domínio inválido: {raw!r}") from None
    if len(text) > 240 or not DOMAIN_RE.fullmatch(text):
        raise ValueError(f"Domínio inválido: {raw!r}. Use algo como youtube.com")
    return text


def parse_time(text: str) -> int:
    match = TIME_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(f"Horário inválido: {text!r}. Use HH:MM")
    hours, minutes = int(match[1]), int(match[2])
    total = hours * 60 + minutes
    if minutes > 59 or total > 1440:
        raise ValueError(f"Horário inválido: {text!r}")
    return total


def format_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_intervals(items: list[str]) -> list[tuple[int, int]]:
    """Converte ["09:00-12:00", ...] em minutos, ordenado e sem sobreposição."""
    parsed = []
    for item in items:
        if not isinstance(item, str) or item.count("-") != 1:
            raise ValueError(f"Intervalo inválido: {item!r}. Use 09:00-18:00")
        start_text, end_text = item.split("-")
        start, end = parse_time(start_text), parse_time(end_text)
        if start >= end:
            raise ValueError(
                f"Intervalo inválido: {item}. O fim precisa vir depois do início; "
                "para virar a noite, use 22:00-24:00 em um dia e 00:00-02:00 no seguinte"
            )
        parsed.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(parsed):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def normalize_schedule(raw: object) -> dict[str, list[str]]:
    if not isinstance(raw, dict) or set(raw) - set(DAYS):
        raise ValueError("Agenda inválida")
    schedule = {}
    for day in DAYS:
        items = raw.get(day, [])
        if not isinstance(items, list):
            raise ValueError(f"Agenda inválida em {day}")
        try:
            intervals = parse_intervals(items)
        except ValueError as error:
            raise ValueError(f"{day}: {error}") from None
        schedule[day] = [f"{format_time(a)}-{format_time(b)}" for a, b in intervals]
    return schedule


def full_week() -> dict[str, list[str]]:
    return {day: ["00:00-24:00"] for day in DAYS}


# ------------------------------------------------------------- configuração


def validate_config(raw: object) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("sites"), dict):
        raise ValueError("Configuração inválida")
    sites = {}
    for domain, site in raw["sites"].items():
        if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain):
            raise ValueError(f"Domínio inválido na configuração: {domain!r}")
        if not isinstance(site, dict):
            raise ValueError(f"Entrada inválida na configuração: {domain}")
        entry = {"schedule": normalize_schedule(site.get("schedule", {}))}
        for field in TIMESTAMP_FIELDS:  # campos novos faltam em configurações antigas
            value = site.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Valor inválido em {field} na configuração: {domain}")
            entry[field] = value
        sites[domain] = entry
    return {"sites": sites}


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as file:
        return validate_config(json.load(file))


def replace_file(source: str, target: Path) -> None:
    """os.replace com novas tentativas: no Windows ele falha enquanto outro processo lê o destino."""
    for attempt in range(20):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not WINDOWS or attempt == 19:
                raise
            time.sleep(0.1)


def save_config(config: dict) -> None:
    descriptor, temp_name = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(config, file, indent=2, ensure_ascii=False, sort_keys=True)
            file.write("\n")
        os.chmod(temp_name, 0o644)
        replace_file(temp_name, CONFIG_PATH)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


@contextlib.contextmanager
def config_lock():
    """Trava entre a CLI e a interface, para uma não sobrescrever a outra."""
    with LOCK_PATH.open("a+b") as lock:
        if not WINDOWS:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
            return
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)  # tenta por até 10 segundos
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def editing_config():
    """Carrega, entrega para edição e salva, com trava entre CLI e interface."""
    with config_lock():
        config = load_config()
        yield config
        save_config(config)


def require_site(config: dict, domain: str) -> dict:
    site = config["sites"].get(domain)
    if site is None:
        raise ValueError(f"{domain} não está na lista")
    return site


def add_site(config: dict, raw_domain: str) -> str:
    domain = normalize_domain(raw_domain)
    if domain in config["sites"]:
        raise ValueError(f"{domain} já está na lista")
    config["sites"][domain] = {"schedule": full_week(), **dict.fromkeys(TIMESTAMP_FIELDS, 0)}
    return domain


def update_site(config: dict, domain: str, new_domain: str | None, schedule: object) -> str:
    site = require_site(config, domain)
    target = normalize_domain(new_domain) if new_domain else domain
    if target != domain and target in config["sites"]:
        raise ValueError(f"{target} já está na lista")
    if schedule is not None:
        site["schedule"] = normalize_schedule(schedule)
    if target != domain:
        config["sites"][target] = config["sites"].pop(domain)
    return target


def remove_site(config: dict, domain: str) -> None:
    require_site(config, domain)
    del config["sites"][domain]


def duration_label(seconds: float) -> str:
    minutes = max(1, -(-int(seconds) // 60))  # arredonda para cima
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes:02d} min" if minutes else f"{hours} h"


def unblock_site(config: dict, domain: str, minutes: object, now: float | None = None) -> None:
    site = require_site(config, domain)
    now = time.time() if now is None else now
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ValueError("Informe os minutos como número inteiro")
    if not MIN_UNBLOCK <= minutes <= MAX_UNBLOCK:
        raise ValueError(f"Escolha de {MIN_UNBLOCK} a {MAX_UNBLOCK} minutos")
    wait = site.get("cooldown_until", 0) - now
    if wait > 0:
        raise ValueError(f"{domain} só pode ser liberado de novo daqui a {duration_label(wait)}")
    site["unblocked_since"] = now
    site["unblocked_until"] = now + minutes * 60
    # Se a liberação for até o fim, a espera começa quando ela acabar.
    site["cooldown_until"] = site["unblocked_until"] + COOLDOWN_FACTOR * minutes * 60


def block_now(config: dict, domain: str, now: float | None = None) -> None:
    site = require_site(config, domain)
    now = time.time() if now is None else now
    since = site.get("unblocked_since", 0)
    if site.get("unblocked_until", 0) > now and since:
        # Encerrou antes: a espera passa a valer pelo tempo realmente usado.
        site["cooldown_until"] = now + COOLDOWN_FACTOR * max(0, now - since)
    site["unblocked_until"] = 0


# ------------------------------------------------------------------- agenda


def week_map(site: dict) -> bytearray:
    """Uma posição por minuto da semana, com 1 onde o site fica bloqueado."""
    week = bytearray(MINUTES_IN_WEEK)
    for index, day in enumerate(DAYS):
        for start, end in parse_intervals(site["schedule"].get(day, [])):
            first, last = index * 1440 + start, index * 1440 + end
            week[first:last] = b"\x01" * (last - first)
    return week


def minute_of_week(moment: datetime) -> int:
    return moment.weekday() * 1440 + moment.hour * 60 + moment.minute


def is_blocked(site: dict, moment: datetime) -> bool:
    if site.get("unblocked_until", 0) > moment.timestamp():
        return False
    return bool(week_map(site)[minute_of_week(moment)])


def next_change(site: dict, moment: datetime) -> datetime | None:
    """Próxima virada da agenda, ignorando liberações temporárias."""
    week = week_map(site)
    index = minute_of_week(moment)
    current = week[index]
    floor = moment.replace(second=0, microsecond=0)
    for step in range(1, MINUTES_IN_WEEK):
        if week[(index + step) % MINUTES_IN_WEEK] != current:
            return floor + timedelta(minutes=step)
    return None


def when_label(target: datetime, moment: datetime) -> str:
    days = (target.date() - moment.date()).days
    day = "hoje" if days == 0 else "amanhã" if days == 1 else DAY_NAMES[target.weekday()]
    return f"{day} às {target:%H:%M}"


def seconds_until_blocked(site: dict, moment: datetime) -> int | None:
    """Quanto falta para o site voltar a ser bloqueado, contando liberação e agenda."""
    now = moment.timestamp()
    start = datetime.fromtimestamp(max(now, site.get("unblocked_until", 0)))
    if week_map(site)[minute_of_week(start)]:
        return max(0, int(start.timestamp() - now))
    change = next_change(site, start)
    return None if change is None else max(0, int(change.timestamp() - now))


def site_status(site: dict, moment: datetime) -> dict:
    scheduled = bool(week_map(site)[minute_of_week(moment)])
    remaining = max(0, int(site.get("unblocked_until", 0) - moment.timestamp()))
    cooldown = max(0, int(site.get("cooldown_until", 0) - moment.timestamp()))
    if remaining:
        return {
            "blocked": False,
            "scheduled": scheduled,
            "temp_seconds": remaining,
            "cooldown_seconds": cooldown,
            "blocks_in": seconds_until_blocked(site, moment),
            "label": f"Liberado por mais {duration_label(remaining)}",
        }
    blocked = scheduled
    change = next_change(site, moment)
    state = "Bloqueado" if blocked else "Liberado"
    if change:
        label = f"{state} até {when_label(change, moment)}"
    else:
        label = "Bloqueado o tempo todo" if blocked else "Liberado o tempo todo"
    return {
        "blocked": blocked,
        "scheduled": scheduled,
        "temp_seconds": 0,
        "cooldown_seconds": cooldown,
        "blocks_in": None if blocked else seconds_until_blocked(site, moment),
        "label": label,
    }


def blocked_domains(config: dict, moment: datetime) -> list[str]:
    return sorted(domain for domain, site in config["sites"].items() if is_blocked(site, moment))


# -------------------------------------------------------------------- hosts


def strip_managed_block(text: str) -> str:
    pattern = re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK) + r"\n?"
    return re.sub(r"\n?" + pattern, "\n", text, flags=re.DOTALL)


def render_hosts(original: str, domains: list[str]) -> str:
    base = strip_managed_block(original).rstrip("\n") + "\n"
    if not domains:
        return base
    lines = [BEGIN_MARK]
    for domain in domains:
        for prefix in SUBDOMAINS:
            lines.append(f"0.0.0.0 {prefix}{domain}")
            lines.append(f":: {prefix}{domain}")
    lines.append(END_MARK)
    return base + "\n".join(lines) + "\n"


def read_hosts() -> str:
    # surrogateescape preserva bytes que não são UTF-8, comuns em comentários no hosts do Windows.
    with HOSTS_PATH.open(encoding="utf-8", errors="surrogateescape") as file:
        return file.read()


def write_hosts(content: str) -> None:
    descriptor, temp_name = tempfile.mkstemp(dir=HOSTS_PATH.parent, prefix=".hosts-siteblock-")
    try:
        # Em modo texto, o Windows grava as quebras de linha como CRLF, como o hosts dele espera.
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="surrogateescape") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_name, 0o644)
        if WINDOWS:  # um hosts marcado como somente leitura não pode ser substituído
            with contextlib.suppress(OSError):
                os.chmod(HOSTS_PATH, 0o666)
        replace_file(temp_name, HOSTS_PATH)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise
    if WINDOWS:  # o cache de DNS do Windows guardaria o endereço antigo
        with contextlib.suppress(OSError):
            run_hidden([str(SYSTEM32 / "ipconfig.exe"), "/flushdns"])
    elif shutil.which("restorecon"):  # distros com SELinux
        subprocess.run(["restorecon", str(HOSTS_PATH)], check=False, capture_output=True)


def run_hidden(command: list[str]) -> subprocess.CompletedProcess:
    """Roda um programa sem abrir janela de console no Windows."""
    flags = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
    return subprocess.run(command, capture_output=True, check=False, creationflags=flags)


_log_timestamps = False


def log(message: str) -> None:
    if _log_timestamps:
        message = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(f"siteblock: {message}", file=sys.stderr, flush=True)


def log_to_file(path: Path) -> None:
    """No Windows os serviços rodam com pythonw, sem terminal: a saída vai para um arquivo."""
    global _log_timestamps
    if sys.stdout is not None and sys.stderr is not None:
        return
    with contextlib.suppress(OSError):
        if path.stat().st_size > 1_000_000:
            path.unlink()
    try:
        file = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        file = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr = file
    _log_timestamps = True


def managed_names(hosts_text: str) -> set[str]:
    """Nomes que já estão no bloco do Siteblock."""
    pattern = re.escape(BEGIN_MARK) + r"(.*?)" + re.escape(END_MARK)
    return {
        fields[1]
        for block in re.findall(pattern, hosts_text, flags=re.DOTALL)
        for fields in (line.split() for line in block.splitlines())
        if len(fields) == 2
    }


def resolve_addresses(domains: list[str], timeout: float = RESOLVE_TIMEOUT) -> set[str]:
    """IPs atuais dos domínios. Sem internet devolve o que der, sem travar o bloqueio."""
    found: list[str] = []

    def lookup(name: str) -> None:
        with contextlib.suppress(OSError):
            found.extend(info[4][0] for info in socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM))

    threads = [
        threading.Thread(target=lookup, args=(prefix + domain,), daemon=True)
        for domain in domains
        for prefix in SUBDOMAINS
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    return {address for address in map(public_address, list(found)) if address}


def public_address(text: str) -> str | None:
    """Descarta endereços locais: derrubar conexões locais afetaria outros programas."""
    address = ipaddress.ip_address(text.split("%", 1)[0])
    if address.is_loopback or address.is_unspecified or address.is_link_local:
        return None
    return str(address)


def kill_connections(addresses: set[str]) -> None:
    """O hosts só vale para conexões novas: as abertas precisam ser derrubadas."""
    if not addresses:
        return
    if WINDOWS:
        kill_connections_windows(addresses)
        return
    expression: list[str] = []
    for address in sorted(addresses):
        expression += ["or"] if expression else []
        expression += ["dst", f"[{address}]" if ":" in address else address]
    try:
        result = subprocess.run(
            ["ss", "-K", "-n", "-t", "-u", *expression], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        log(f"conexões abertas continuam ativas: {error}")
        return
    if result.returncode:
        log(f"conexões abertas continuam ativas: {result.stderr.strip()}")


def kill_connections_windows(addresses: set[str]) -> None:
    """Fecha as conexões TCP abertas com SetTcpEntry, o mesmo que o TCPView faz.

    O Windows só permite isso para IPv4, e só para um administrador ou SYSTEM.
    """
    import ctypes
    from ctypes import wintypes

    class TcpRow(ctypes.Structure):  # MIB_TCPROW
        _fields_ = [(name, wintypes.DWORD) for name in ("state", "local_addr", "local_port", "remote_addr", "remote_port")]

    targets = {address for address in addresses if ":" not in address}
    if not targets:
        return
    iphlpapi = ctypes.windll.iphlpapi
    size = wintypes.DWORD(0)
    for _ in range(5):  # a tabela pode crescer entre a consulta do tamanho e a leitura
        buffer = ctypes.create_string_buffer(max(size.value, ctypes.sizeof(wintypes.DWORD)))
        result = iphlpapi.GetTcpTable(buffer, ctypes.byref(size), False)
        if result != 122:  # ERROR_INSUFFICIENT_BUFFER
            break
    if result == 232:  # ERROR_NO_DATA: nenhuma conexão aberta
        return
    if result:
        log(f"conexões abertas continuam ativas: GetTcpTable devolveu o erro {result}")
        return
    count = wintypes.DWORD.from_buffer(buffer).value
    rows = (TcpRow * count).from_buffer(buffer, ctypes.sizeof(wintypes.DWORD))
    failures = 0
    for row in rows:
        # O endereço vem na ordem de bytes da rede, guardado num DWORD little-endian.
        remote = socket.inet_ntoa(row.remote_addr.to_bytes(4, "little"))
        if row.state != 5 or remote not in targets:  # 5 = MIB_TCP_STATE_ESTAB
            continue
        entry = TcpRow(12, row.local_addr, row.local_port, row.remote_addr, row.remote_port)  # 12 = DELETE_TCB
        if iphlpapi.SetTcpEntry(ctypes.byref(entry)):
            failures += 1
    if failures:
        log(f"{failures} conexões abertas continuam ativas: SetTcpEntry falhou")


def enforce_once() -> bool:
    """Aplica a agenda. Devolve True se o hosts mudou."""
    desired = blocked_domains(load_config(), datetime.now())
    current = read_hosts()
    updated = render_hosts(current, desired)
    if updated == current:
        return False
    existing = managed_names(current)
    addresses = resolve_addresses([domain for domain in desired if domain not in existing])  # antes do hosts mudar
    write_hosts(updated)
    kill_connections(addresses)  # depois do hosts: a reconexão já encontra o bloqueio
    return True


def enforce_forever() -> None:
    last_error = None
    while True:
        try:
            enforce_once()
            last_error = None
        except (OSError, ValueError) as error:
            # Configuração ruim não pode virar "libera tudo": o hosts fica como está.
            message = f"mantendo o estado atual: {type(error).__name__}: {error}"
            if message != last_error:
                log(message)
            last_error = message
        time.sleep(ENFORCE_INTERVAL)


def manual_entries(hosts_text: str) -> set[str]:
    """Nomes bloqueados à mão, fora do bloco do Siteblock."""
    names: set[str] = set()
    for line in strip_managed_block(hosts_text).splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and (fields[0].startswith("127.") or fields[0] in ("0.0.0.0", "::", "::1")):
            names.update(name.lower() for name in fields[1:])
    return names


def has_manual_conflict(domain: str, names: set[str]) -> bool:
    return any(prefix + domain in names for prefix in SUBDOMAINS)


# ---------------------------------------------------------------- interface


def build_state() -> dict:
    config = load_config()
    moment = datetime.now()
    hosts_text = read_hosts()
    manual = manual_entries(hosts_text)
    desired = blocked_domains(config, moment)
    sites = []
    for domain in sorted(config["sites"]):
        site = config["sites"][domain]
        sites.append({
            "domain": domain,
            "schedule": site["schedule"],
            "manual": has_manual_conflict(domain, manual),
            **site_status(site, moment),
        })
    return {
        "sites": sites,
        "applied": render_hosts(hosts_text, desired) == hosts_text,
        "now": {"day": DAYS.index(DAYS[moment.weekday()]), "minute": moment.hour * 60 + moment.minute},
        "limits": {"min": MIN_UNBLOCK, "max": MAX_UNBLOCK},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "Siteblock"
    sys_version = ""

    def log_message(self, *args):
        pass

    def respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.respond(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def host_allowed(self) -> bool:
        # Barra DNS rebinding: um site externo não fala com esta porta pelo próprio nome.
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self.host_allowed():
            return self.send_json(403, {"error": "Origem não permitida"})
        if self.path == "/":
            return self.respond(200, render_page().encode(), "text/html; charset=utf-8")
        if self.path == "/logo.jpg" and (logo := find_logo()):
            return self.respond(200, logo.read_bytes(), "image/jpeg")
        if self.path == "/api/state":
            try:
                return self.send_json(200, build_state())
            except (OSError, ValueError) as error:
                return self.send_json(500, {"error": str(error)})
        return self.send_json(404, {"error": "Não encontrado"})

    def do_POST(self):
        # O cabeçalho próprio força preflight de CORS, que nunca é aprovado:
        # assim uma página qualquer aberta no navegador não consegue liberar sites.
        if not self.host_allowed() or self.headers.get("X-Siteblock") != "1":
            return self.send_json(403, {"error": "Origem não permitida"})
        if self.headers.get("Content-Type") != "application/json":
            return self.send_json(415, {"error": "Envie JSON"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("Tamanho inválido")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Corpo inválido")
            result = self.apply(self.path, body)
        except KeyError as error:
            return self.send_json(400, {"error": f"Campo ausente: {error}"})
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})
        except OSError as error:
            return self.send_json(500, {"error": str(error)})
        return self.send_json(200, result)

    def apply(self, path: str, body: dict) -> dict:
        with editing_config() as config:
            if path == "/api/sites/add":
                return {"domain": add_site(config, body["domain"])}
            if path == "/api/sites/update":
                domain = update_site(config, body["domain"], body.get("new_domain"), body.get("schedule"))
                return {"domain": domain}
            if path == "/api/sites/remove":
                remove_site(config, body["domain"])
            elif path == "/api/sites/unblock":
                unblock_site(config, body["domain"], body["minutes"])
            elif path == "/api/sites/block":
                block_now(config, body["domain"])
            else:
                raise ValueError("Ação desconhecida")
            return {}


def find_logo() -> Path | None:
    """O logo instalado ou, rodando direto da pasta do projeto, o que está ao lado do script."""
    for path in (LOGO_PATH, Path(__file__).resolve().parent / LOGO_NAME):
        if path.is_file():
            return path
    return None


def render_page() -> str:
    page = PAGE
    for key, value in (("__EXTENSION_DIR__", EXTENSION_DIR), ("__HOSTS__", HOSTS_PATH), ("__RESTART_HINT__", RESTART_HINT)):
        page = page.replace(key, html.escape(str(value)))
    return page


def serve() -> None:
    # No Windows, SO_REUSEADDR deixaria dois processos ouvirem a mesma porta.
    ThreadingHTTPServer.allow_reuse_address = not WINDOWS
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Siteblock em http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


PAGE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Siteblock</title>
<link rel="icon" href="/logo.jpg">
<style>
:root{
  --bg:#e8eef0; --ink:#14303a; --muted:#50676f; --line:#c4d1d6; --field:#f6f9fa;
  --blocked:#96244f; --free:#cfe0d9; --open:#1f7a5c; --now:#14303a; --warn:#7a4a00;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#102027; --ink:#e4eef0; --muted:#9db2b9; --line:#2f464f; --field:#18303a;
  --blocked:#e0628f; --free:#234a44; --open:#6fd0ab; --now:#e4eef0; --warn:#f0c26e;
  color-scheme:dark;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 Cantarell,Ubuntu,"Noto Sans","Segoe UI",sans-serif}
main{max-width:940px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:2rem;line-height:1.2;margin:0 0 20px;font-weight:700}
form.add{display:flex;gap:8px;margin-bottom:12px}
input[type=text]{font:inherit;color:inherit;background:var(--field);border:1px solid var(--line);border-radius:4px;padding:8px 10px;min-width:0}
form.add input{flex:1}
button{font:inherit;color:inherit;background:transparent;border:1px solid var(--muted);border-radius:4px;padding:8px 12px;cursor:pointer;white-space:nowrap}
button:hover{background:var(--line)}
button.primary{background:var(--ink);border-color:var(--ink);color:var(--bg);font-weight:600}
button.primary:hover{opacity:.88;background:var(--ink)}
button.danger{border-color:var(--blocked);color:var(--blocked)}
:focus-visible{outline:2px solid var(--open);outline-offset:2px}
#alert{min-height:1.5em;margin:0 0 12px;color:var(--blocked);font-weight:600}
#health{display:none;margin:0 0 16px;color:var(--warn);font-weight:600}
#empty{color:var(--muted);padding:24px 0;border-top:1px solid var(--line)}
.site{border-top:1px solid var(--line);padding:16px 0}
.summary{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px 24px;align-items:center}
.name{font-size:1.25rem;font-weight:700;overflow-wrap:anywhere}
.status{color:var(--muted)}
.status.blocked{color:var(--blocked);font-weight:600}
.status.open{color:var(--open);font-weight:600}
.manual{grid-column:1/-1;color:var(--warn);margin:0}
.actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.wait{color:var(--muted);font-variant-numeric:tabular-nums}
.note{color:var(--muted);margin:32px 0 0;padding-top:16px;border-top:1px solid var(--line);font-size:.9rem}
.note code{overflow-wrap:anywhere}
input[type=range]{width:120px;accent-color:var(--blocked)}
.editor{margin-top:16px}
.rename{display:flex;gap:8px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.rename input{flex:1;min-width:220px}
.hint{color:var(--muted);margin:0 0 8px}
.presets{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.week{display:grid;grid-template-columns:40px minmax(0,1fr) 190px;gap:6px 10px;align-items:center}
.ruler{grid-column:2;position:relative;height:1.2em;font-size:.8rem;color:var(--muted)}
.ruler span{position:absolute;transform:translateX(-50%)}
.ruler span:first-child{transform:none}.ruler span:last-child{transform:translateX(-100%)}
.day{font-weight:600}
.bar{position:relative;display:grid;grid-template-columns:repeat(96,1fr);height:30px;background:var(--free);cursor:crosshair;touch-action:none;user-select:none}
.bar i{display:block;border-right:1px solid transparent}
.bar i:nth-child(24n){border-right-color:color-mix(in srgb,var(--ink) 35%,transparent)}
.bar i.on{background:var(--blocked)}
.bar b{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--now);pointer-events:none}
.week input[aria-invalid=true]{border-color:var(--blocked)}
.footer{display:flex;gap:8px;justify-content:space-between;margin-top:18px;flex-wrap:wrap}
@media (max-width:680px){
  main{padding:24px 16px 60px}
  .summary{grid-template-columns:minmax(0,1fr)}
  .actions{justify-content:flex-start}
  .week{grid-template-columns:40px minmax(0,1fr)}
  .week input{grid-column:2}
  .filler{display:none}
}
</style>
</head>
<body>
<main>
  <h1>Siteblock</h1>
  <form class="add" id="add">
    <input type="text" id="domain" aria-label="Site para adicionar" placeholder="youtube.com" autocomplete="off" spellcheck="false">
    <button class="primary">Adicionar site</button>
  </form>
  <p id="alert" role="alert"></p>
  <p id="health" role="status">A agenda não está sendo aplicada. __RESTART_HINT__</p>
  <div id="list"></div>
  <p id="empty" hidden>Nenhum site na lista. Adicione o primeiro acima.</p>
  <p class="note">Depois de uma liberação, o site só pode ser liberado de novo após 3 vezes o tempo que ficou liberado.
  Para ver o tempo restante dentro do próprio site, carregue a extensão de <code>__EXTENSION_DIR__</code>
  em chrome://extensions ou edge://extensions (modo do desenvolvedor, "Carregar sem compactação").</p>
</main>
<template id="row">
  <section class="site">
    <div class="summary">
      <div><div class="name"></div><div class="status" aria-live="polite"></div></div>
      <div class="actions">
        <span class="wait" hidden></span>
        <input type="range" step="1" value="5" aria-label="Minutos de liberação">
        <button class="unblock"></button>
        <button class="lock" hidden>Bloquear agora</button>
        <button class="edit" aria-expanded="false">Editar</button>
      </div>
      <p class="manual" hidden>Este site também está direto no __HOSTS__. Apague a linha de lá para a agenda e a liberação funcionarem.</p>
    </div>
    <div class="editor" hidden></div>
  </section>
</template>
<script>
const DAYS=[["seg","Seg"],["ter","Ter"],["qua","Qua"],["qui","Qui"],["sex","Sex"],["sab","Sáb"],["dom","Dom"]];
const CELL=15, CELLS=96;
const rows=new Map();
let openDomain=null, misses=0, limits={min:1,max:15}, now={day:0,minute:0};
const el=id=>document.getElementById(id);

async function api(path,body){
  const options=body?{method:"POST",headers:{"Content-Type":"application/json","X-Siteblock":"1"},body:JSON.stringify(body)}:{};
  const response=await fetch(path,options);
  const data=await response.json();
  if(!response.ok) throw new Error(data.error||"A ação falhou");
  return data;
}
function report(error){el("alert").textContent=error?error.message:""}
async function act(path,body){
  try{const data=await api(path,body);report();await refresh();return data}
  catch(error){report(error);return null}
}

function toMinutes(text){
  const match=/^(\d{1,2}):(\d{2})$/.exec(text.trim());
  if(!match) return null;
  const value=+match[1]*60+ +match[2];
  return +match[2]<60&&value<=1440?value:null;
}
function parseDay(text){
  const out=[];
  for(const part of text.split(",").map(p=>p.trim()).filter(Boolean)){
    const ends=part.split("-");
    if(ends.length!==2) return null;
    const start=toMinutes(ends[0]), end=toMinutes(ends[1]);
    if(start===null||end===null||start>=end) return null;
    out.push([start,end]);
  }
  return out;
}
const pad=n=>String(n).padStart(2,"0");
const clock=m=>pad(Math.floor(m/60))+":"+pad(m%60);
function cellsToText(cells){
  const out=[];let start=null;
  for(let i=0;i<=CELLS;i++){
    if(i<CELLS&&cells[i]){if(start===null)start=i}
    else if(start!==null){out.push(clock(start*CELL)+"-"+clock(i*CELL));start=null}
  }
  return out.join(", ");
}

function paintBar(bar,intervals){
  [...bar.querySelectorAll("i")].forEach((cell,i)=>{
    const a=i*CELL,b=a+CELL;
    cell.classList.toggle("on",intervals.some(([s,e])=>s<b&&e>a));
  });
}

function buildEditor(row,site){
  const box=row.editor;box.replaceChildren();
  const rename=document.createElement("label");rename.className="rename";
  const nameInput=document.createElement("input");nameInput.type="text";nameInput.value=site.domain;nameInput.spellcheck=false;
  rename.append("Endereço do site",nameInput);

  const hint=document.createElement("p");hint.className="hint";
  hint.textContent="As faixas coloridas são os horários bloqueados. Arraste para pintar ou apagar, ou digite os horários, como 09:00-12:00, 14:00-18:00.";

  const presets=document.createElement("div");presets.className="presets";
  const week=document.createElement("div");week.className="week";
  const spacer=document.createElement("span");
  const ruler=document.createElement("div");ruler.className="ruler";ruler.setAttribute("aria-hidden","true");
  for(const hour of [0,6,12,18,24]){const s=document.createElement("span");s.textContent=hour+"h";s.style.left=(hour/24*100)+"%";ruler.append(s)}
  const filler=document.createElement("span");filler.className="filler";
  week.append(spacer,ruler,filler);

  const inputs={},bars={};
  let painting=null;
  const syncFromBar=key=>{
    const cells=[...bars[key].querySelectorAll("i")].map(c=>c.classList.contains("on"));
    inputs[key].value=cellsToText(cells);inputs[key].removeAttribute("aria-invalid");
  };
  DAYS.forEach(([key,label],index)=>{
    const name=document.createElement("span");name.className="day";name.textContent=label;
    const bar=document.createElement("div");bar.className="bar";bar.setAttribute("aria-hidden","true");
    for(let i=0;i<CELLS;i++)bar.append(document.createElement("i"));
    if(index===now.day){const mark=document.createElement("b");mark.style.left=(now.minute/1440*100)+"%";bar.append(mark)}
    const input=document.createElement("input");input.type="text";input.value=site.schedule[key].join(", ");
    input.setAttribute("aria-label","Horários bloqueados: "+label);input.placeholder="Liberado o dia todo";
    input.addEventListener("input",()=>{
      const parsed=parseDay(input.value);
      if(parsed){input.removeAttribute("aria-invalid");paintBar(bar,parsed)}else input.setAttribute("aria-invalid","true");
    });
    const cellOf=event=>{
      const box=bar.getBoundingClientRect();
      return Math.max(0,Math.min(CELLS-1,Math.floor((event.clientX-box.left)/box.width*CELLS)));
    };
    const paint=event=>{bar.children[cellOf(event)].classList.toggle("on",painting.value);syncFromBar(key)};
    bar.addEventListener("pointerdown",event=>{
      event.preventDefault();
      painting={value:!bar.children[cellOf(event)].classList.contains("on"),bar};
      paint(event);
    });
    bar.addEventListener("pointermove",event=>{if(painting)paint(event)});
    bar.addEventListener("pointerenter",event=>{if(painting)paint(event)});
    inputs[key]=input;bars[key]=bar;
    paintBar(bar,parseDay(input.value)||[]);
    week.append(name,bar,input);
  });
  const stop=()=>{painting=null};
  document.addEventListener("pointerup",stop);
  row.stopPaint=()=>document.removeEventListener("pointerup",stop);

  const fill=(days,value)=>{for(const [key] of DAYS){inputs[key].value=days.includes(key)?value:"";inputs[key].dispatchEvent(new Event("input"))}};
  const workdays=["seg","ter","qua","qui","sex"];
  for(const [label,action] of [
    ["Bloquear sempre",()=>fill(DAYS.map(d=>d[0]),"00:00-24:00")],
    ["Liberar o fim de semana",()=>fill(workdays,"00:00-24:00")],
    ["Bloquear seg a sex, 8h às 18h",()=>fill(workdays,"08:00-18:00")],
    ["Limpar",()=>fill([],"")],
  ]){const b=document.createElement("button");b.type="button";b.textContent=label;b.onclick=action;presets.append(b)}

  const footer=document.createElement("div");footer.className="footer";
  const save=document.createElement("button");save.className="primary";save.textContent="Salvar";
  save.onclick=async()=>{
    const schedule={};
    for(const [key,label] of DAYS){
      if(!parseDay(inputs[key].value)){report(new Error(label+": use horários como 09:00-18:00"));inputs[key].focus();return}
      schedule[key]=inputs[key].value.split(",").map(p=>p.trim()).filter(Boolean);
    }
    const data=await act("/api/sites/update",{domain:site.domain,new_domain:nameInput.value,schedule});
    if(data)closeEditor();
  };
  const cancel=document.createElement("button");cancel.textContent="Cancelar";cancel.onclick=closeEditor;
  const remove=document.createElement("button");remove.className="danger";remove.textContent="Remover site";
  remove.onclick=async()=>{
    if(confirm("Remover "+site.domain+" da lista? Ele deixa de ser bloqueado.")){
      const data=await act("/api/sites/remove",{domain:site.domain});
      if(data){openDomain=null}
    }
  };
  const left=document.createElement("div");left.style.display="flex";left.style.gap="8px";left.append(save,cancel);
  footer.append(left,remove);
  box.append(rename,hint,presets,week,footer);
}

function closeEditor(){
  const row=rows.get(openDomain);
  if(row){row.editor.hidden=true;row.edit.setAttribute("aria-expanded","false");row.editor.replaceChildren();if(row.stopPaint)row.stopPaint()}
  openDomain=null;
}

function createRow(site){
  const node=el("row").content.firstElementChild.cloneNode(true);
  const row={node,site,deadline:0,cooldown:0,wait:node.querySelector(".wait"),
    name:node.querySelector(".name"),status:node.querySelector(".status"),range:node.querySelector("input[type=range]"),
    unblock:node.querySelector(".unblock"),lock:node.querySelector(".lock"),edit:node.querySelector(".edit"),
    manual:node.querySelector(".manual"),editor:node.querySelector(".editor")};
  row.name.textContent=site.domain;
  row.range.min=limits.min;row.range.max=limits.max;
  const label=()=>{row.unblock.textContent="Liberar por "+row.range.value+" min"};
  row.range.oninput=label;label();
  row.unblock.onclick=()=>act("/api/sites/unblock",{domain:row.site.domain,minutes:+row.range.value});
  row.lock.onclick=()=>act("/api/sites/block",{domain:row.site.domain});
  row.edit.onclick=()=>{
    const same=openDomain===row.site.domain;
    closeEditor();
    if(same)return;
    openDomain=row.site.domain;
    buildEditor(row,row.site);row.editor.hidden=false;row.edit.setAttribute("aria-expanded","true");
  };
  return row;
}

function countdown(seconds){
  const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=seconds%60;
  return h?h+":"+pad(m)+":"+pad(s):m+":"+pad(s);
}
function showStatus(row){
  const site=row.site;
  const left=Math.ceil((row.deadline-Date.now())/1000);
  const wait=Math.ceil((row.cooldown-Date.now())/1000);
  const cooling=!(left>0)&&wait>0;
  row.lock.hidden=!(left>0);
  row.lock.textContent=site.scheduled?"Bloquear agora":"Encerrar liberação";
  row.range.hidden=row.unblock.hidden=left>0||cooling;
  row.wait.hidden=!cooling;
  if(cooling)row.wait.textContent="Nova liberação em "+countdown(wait);
  if(left>0){
    row.status.textContent="Liberado por mais "+countdown(left);
    row.status.className="status open";
  }else{
    row.status.textContent=site.label;
    row.status.className="status"+(site.blocked?" blocked":"");
  }
}

function render(state){
  limits=state.limits;now=state.now;
  const seen=new Set(state.sites.map(site=>site.domain));
  for(const [domain,row] of rows)if(!seen.has(domain)){
    if(openDomain===domain)closeEditor();
    row.node.remove();rows.delete(domain);
  }
  // Só mexe no DOM se a ordem mudou: mover um nó tira o foco e perde o clique em andamento.
  const list=el("list");
  let cursor=list.firstElementChild;
  for(const site of state.sites){
    let row=rows.get(site.domain);
    if(!row){row=createRow(site);rows.set(site.domain,row)}
    row.site=site;
    row.deadline=site.temp_seconds?Date.now()+site.temp_seconds*1000:0;
    row.cooldown=site.cooldown_seconds?Date.now()+site.cooldown_seconds*1000:0;
    row.manual.hidden=!site.manual;
    showStatus(row);
    if(row.node===cursor)cursor=cursor.nextElementSibling;
    else list.insertBefore(row.node,cursor);
  }
  el("empty").hidden=state.sites.length>0;
}

async function refresh(){
  try{
    const state=await api("/api/state");
    render(state);
    return state;
  }catch(error){report(error);return null}
}
async function poll(){
  const state=await refresh();
  if(state){misses=state.applied?0:misses+1;el("health").style.display=misses>=3?"block":"none"}
}

el("add").addEventListener("submit",async event=>{
  event.preventDefault();
  const input=el("domain");
  const data=await act("/api/sites/add",{domain:input.value});
  if(data){input.value="";input.focus()}
});
setInterval(()=>rows.forEach(showStatus),1000);
setInterval(poll,4000);
poll();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- extensão
#
# O hosts bloqueia antes de o navegador chegar a qualquer página, então só uma
# extensão consegue mostrar algo no lugar do erro ou por cima do site. Ela é só
# visual: o bloqueio continua no /etc/hosts e funciona sem ela.

EXTENSION_MANIFEST = """{
  "manifest_version": 3,
  "name": "Siteblock",
  "version": "1.0",
  "description": "Mostra quanto falta para o Siteblock bloquear um site e quando ele pode ser liberado de novo.",
  "permissions": ["declarativeNetRequestWithHostAccess", "webNavigation", "alarms"],
  "host_permissions": ["<all_urls>"],
  "background": {"service_worker": "background.js"},
  "content_scripts": [{"matches": ["http://*/*", "https://*/*"], "js": ["content.js"], "run_at": "document_idle"}],
  "web_accessible_resources": [{"resources": ["blocked.html"], "matches": ["<all_urls>"]}]
}
"""

EXTENSION_BACKGROUND = r"""// Fala com o Siteblock local e manda os sites bloqueados para a página com o timer.
const API = "__API__";
const PAGE = chrome.runtime.getURL("blocked.html");
const PREFIXES = ["", "www.", "m."];  // os mesmos nomes que o Siteblock põe no hosts
let cache = {state: null, at: 0};
let queue = Promise.resolve(null);

function hostOf(url) {
  try {
    const parsed = new URL(url);
    return /^https?:$/.test(parsed.protocol) ? parsed.hostname.replace(/\.$/, "") : null;
  } catch { return null; }
}

function siteIn(state, host) {
  if (!state || !host) return null;
  return state.sites.find(site => PREFIXES.some(prefix => prefix + site.domain === host)) || null;
}

const escapeRe = text => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Busca o estado e troca as regras de redirecionamento. Uma de cada vez, para os ids não colidirem.
function sync() {
  queue = queue.then(async () => {
    let state = null;
    try {
      const response = await fetch(API + "/api/state", {cache: "no-store"});
      if (response.ok) state = await response.json();
    } catch {}
    cache = {state, at: state ? Date.now() : 0};
    // Sem o Siteblock no ar, nada é redirecionado: o hosts continua bloqueando sozinho.
    const blocked = state ? state.sites.filter(site => site.blocked) : [];
    const old = await chrome.declarativeNetRequest.getSessionRules();
    await chrome.declarativeNetRequest.updateSessionRules({
      removeRuleIds: old.map(rule => rule.id),
      addRules: blocked.map((site, index) => ({
        id: index + 1,
        priority: 1,
        action: {type: "redirect", redirect: {regexSubstitution: PAGE + "#\\0"}},
        condition: {
          regexFilter: "^https?://(www\\.|m\\.)?" + escapeRe(site.domain) + "\\.?([:/?#].*)?$",
          resourceTypes: ["main_frame"],
        },
      })),
    });
    return state;
  }).catch(() => null);
  return queue;
}

async function statusFor(url) {
  const host = hostOf(url);
  if (!host) return null;
  let state = cache.state;
  // Sites da lista sempre consultam na hora; os outros usam o estado do último minuto.
  if (!state || Date.now() - cache.at > 60000 || siteIn(state, host)) state = await sync();
  return siteIn(state, host);
}

function showBlocked(tabId, url) {
  chrome.tabs.update(tabId, {url: PAGE + "#" + url});
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message.type === "status") {
    statusFor(message.url).then(site => reply({ok: cache.state !== null, site}));
    return true;
  }
  if (message.type === "expired" && sender.tab) {
    statusFor(message.url).then(site => { if (site && site.blocked) showBlocked(sender.tab.id, message.url); });
  }
  return false;
});

// Antes de abrir um site da lista, atualiza as regras para não usar um estado velho.
chrome.webNavigation.onBeforeNavigate.addListener(details => {
  if (details.frameId === 0 && siteIn(cache.state, hostOf(details.url))) sync();
});

// Se o bloqueio começou entre duas atualizações, o navegador mostra o erro: troca pela página.
chrome.webNavigation.onErrorOccurred.addListener(async details => {
  if (details.frameId !== 0 || details.error === "net::ERR_ABORTED") return;
  const site = await statusFor(details.url);
  if (site && site.blocked) showBlocked(details.tabId, details.url);
});

const schedule = () => chrome.alarms.create("sync", {periodInMinutes: 0.5});
chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener(sync);
sync();
"""

EXTENSION_CONTENT = r"""// Janelinha com o tempo até o site ser bloqueado de novo.
(() => {
  if (window.top !== window) return;
  const SHOW_WITHIN = 3600;  // só aparece quando falta até 1 hora
  let host = null, parts = null, deadline = 0, domain = "", collapsed = false, expired = false;

  function build() {
    host = document.createElement("div");
    host.style.cssText = "all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483647";
    const root = host.attachShadow({mode: "closed"});
    root.innerHTML = `<style>
      button{all:initial;box-sizing:border-box;display:flex;flex-direction:column;gap:2px;cursor:pointer;
        padding:10px 14px;border-radius:10px;background:#14303a;color:#e4eef0;
        font:13px/1.3 Cantarell,Ubuntu,"Noto Sans","Segoe UI",system-ui,sans-serif;
        box-shadow:0 4px 18px rgba(0,0,0,.35);transition:background .3s}
      button:focus-visible{outline:2px solid #6fd0ab;outline-offset:2px}
      .time{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.15}
      .urgent{background:#96244f}
      .collapsed{padding:6px 10px}
      .collapsed .label{display:none}
      .collapsed .time{font-size:15px}
    </style><button type="button" title="Siteblock: clique para minimizar"><span class="label"></span><span class="time"></span></button>`;
    const button = root.querySelector("button");
    parts = {button, label: root.querySelector(".label"), time: root.querySelector(".time")};
    button.addEventListener("click", () => { collapsed = !collapsed; render(); });
  }

  const pad = n => String(n).padStart(2, "0");
  function clock(seconds) {
    const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
    return h ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
  }

  function render() {
    if (!deadline) { if (host) host.remove(); return; }
    if (!host) build();
    if (!host.isConnected) document.documentElement.append(host);
    const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    parts.label.textContent = left ? domain + " bloqueia em" : "Bloqueando " + domain;
    parts.time.textContent = clock(left);
    parts.button.classList.toggle("urgent", left <= 60);
    parts.button.classList.toggle("collapsed", collapsed);
    parts.button.setAttribute("aria-label", parts.label.textContent + " " + parts.time.textContent);
    if (!left && !expired) {
      expired = true;
      setTimeout(() => {
        chrome.runtime.sendMessage({type: "expired", url: location.href}).catch(() => {});
        setTimeout(check, 4000);
      }, 1000);
    }
  }

  async function check() {
    let reply;
    try { reply = await chrome.runtime.sendMessage({type: "status", url: location.href}); }
    catch { return; }  // extensão recarregada: esta aba fica sem a janelinha até ser recarregada
    const site = reply && reply.site;
    if (!site || site.blocked || site.blocks_in === null || site.blocks_in > SHOW_WITHIN) {
      deadline = 0;
    } else {
      deadline = Date.now() + site.blocks_in * 1000;
      domain = site.domain;
      expired = false;
    }
    render();
  }

  check();
  setInterval(check, 30000);
  setInterval(() => { if (deadline) render(); }, 1000);
})();
"""

EXTENSION_BLOCKED_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Site bloqueado</title>
<style>
:root{
  --bg:#e8eef0; --ink:#14303a; --muted:#50676f; --line:#c4d1d6; --blocked:#96244f; --open:#1f7a5c;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#102027; --ink:#e4eef0; --muted:#9db2b9; --line:#2f464f; --blocked:#e0628f; --open:#6fd0ab;
  color-scheme:dark;
}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
  font:16px/1.5 Cantarell,Ubuntu,"Noto Sans","Segoe UI",sans-serif}
main{width:min(560px,100%);padding:40px 24px}
.eyebrow{margin:0 0 4px;color:var(--muted);font-weight:600;letter-spacing:.04em;text-transform:uppercase;font-size:.8rem}
h1{margin:0 0 4px;font-size:2rem;line-height:1.2;overflow-wrap:anywhere}
#label{margin:0;color:var(--blocked);font-weight:600}
.panel{margin:32px 0;padding:24px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
#caption{margin:0;color:var(--muted)}
#timer{margin:4px 0 0;font-size:4rem;line-height:1.1;font-weight:700;font-variant-numeric:tabular-nums}
#ready{margin:0;font-size:1.1rem}
.actions{display:flex;gap:8px;flex-wrap:wrap}
a,button{font:inherit;color:inherit;background:transparent;border:1px solid var(--muted);border-radius:4px;
  padding:8px 14px;cursor:pointer;text-decoration:none}
a.primary{background:var(--ink);border-color:var(--ink);color:var(--bg);font-weight:600}
:focus-visible{outline:2px solid var(--open);outline-offset:2px}
.open #label{color:var(--open)}
</style>
</head>
<body>
<main>
  <p class="eyebrow">Siteblock</p>
  <h1 id="title">Site bloqueado</h1>
  <p id="label"></p>
  <div class="panel" aria-live="polite">
    <div id="waiting" hidden>
      <p id="caption">Pode ser liberado de novo em</p>
      <p id="timer"></p>
    </div>
    <p id="ready" hidden></p>
  </div>
  <div class="actions">
    <a class="primary" id="panel" href="__API__/">Abrir o Siteblock</a>
    <button type="button" id="back">Voltar</button>
  </div>
</main>
<script src="blocked.js"></script>
</body>
</html>
"""

EXTENSION_BLOCKED_JS = r"""// Página mostrada no lugar do erro enquanto o site está bloqueado.
const el = id => document.getElementById(id);
const pad = n => String(n).padStart(2, "0");
let target = null;
try {
  const parsed = new URL(location.hash.slice(1));
  if (/^https?:$/.test(parsed.protocol)) target = parsed.href;  // nunca abre javascript: e afins
} catch {}
let cooldownEnd = 0, reopening = false, reachable = true;

function clock(seconds) {
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
  return h ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
}

function render() {
  if (reopening) return;
  const left = Math.max(0, Math.ceil((cooldownEnd - Date.now()) / 1000));
  el("waiting").hidden = !reachable || !left;
  el("ready").hidden = reachable && left > 0;
  if (!reachable) el("ready").textContent = "Não foi possível falar com o Siteblock. Veja se o serviço siteblock-ui está rodando.";
  else if (left) el("timer").textContent = clock(left);
  else el("ready").textContent = "Você já pode liberar este site no Siteblock.";
}

function reopen(message) {
  if (reopening || !target) return;
  reopening = true;
  document.body.classList.add("open");
  el("label").textContent = message;
  el("waiting").hidden = true;
  el("ready").hidden = false;
  el("ready").textContent = "Abrindo o site…";
  // O Siteblock reescreve o hosts a cada 2 segundos: espera ele soltar o site.
  setTimeout(() => location.replace(target), 2500);
}

async function check() {
  if (reopening) return;
  let reply = null;
  try { reply = await chrome.runtime.sendMessage({type: "status", url: target || ""}); } catch {}
  reachable = Boolean(reply && reply.ok);
  const site = reply && reply.site;
  if (reachable && target && !site) return reopen("Este site saiu da lista.");
  if (site) {
    if (!site.blocked) return reopen("Liberado.");
    el("label").textContent = site.label;
    cooldownEnd = Date.now() + site.cooldown_seconds * 1000;
  }
  render();
}

const host = target ? new URL(target).hostname.replace(/^www\./, "") : "";
if (host) {
  el("title").textContent = host + " está bloqueado";
  document.title = host + " bloqueado";
}
el("back").addEventListener("click", () => history.back());
check();
setInterval(check, 5000);
setInterval(render, 1000);
"""


def extension_files() -> dict[str, str]:
    api = f"http://127.0.0.1:{PORT}"
    files = {
        "manifest.json": EXTENSION_MANIFEST,
        "background.js": EXTENSION_BACKGROUND,
        "content.js": EXTENSION_CONTENT,
        "blocked.html": EXTENSION_BLOCKED_HTML,
        "blocked.js": EXTENSION_BLOCKED_JS,
    }
    return {name: text.replace("__API__", api) for name, text in files.items()}


def write_extension(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name, text in extension_files().items():
        (target / name).write_text(text, encoding="utf-8")
        (target / name).chmod(0o644)
    target.chmod(0o755)


EXTENSION_HELP = (
    "Para ver o timer no navegador: abra chrome://extensions (ou edge://extensions), ligue o Modo do desenvolvedor,\n"
    "clique em Carregar sem compactação e escolha {path}\n"
    "(Depois de atualizar o Siteblock, clique em recarregar no cartão da extensão.)"
)


# --------------------------------------------------------------- instalação


def require_root() -> None:
    if WINDOWS:
        import ctypes

        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise SystemExit(
                "Este comando precisa de um terminal aberto como administrador.\n"
                "No menu Iniciar, procure Terminal (ou PowerShell), clique com o botão direito\n"
                "e escolha Executar como administrador."
            )
    elif os.geteuid() != 0:
        raise SystemExit("Este comando precisa de sudo.")


def install() -> None:
    if WINDOWS:
        install_windows()
    elif sys.platform.startswith("linux"):
        install_linux()
    else:
        raise SystemExit("O Siteblock funciona no Linux e no Windows.")


def uninstall() -> None:
    if WINDOWS:
        uninstall_windows()
    else:
        uninstall_linux()


# ------------------------------------------------------- instalação: Linux


def systemctl(*args: str, check: bool = True) -> None:
    subprocess.run(["systemctl", *args], check=check)


def install_linux() -> None:
    require_root()
    user = os.environ.get("SUDO_USER")
    if not user or user == "root":
        raise SystemExit("Rode a partir do seu usuário: sudo python3 siteblock.py install")
    account = pwd.getpwnam(user)
    python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable

    source = Path(__file__).resolve()
    if source != BIN_PATH:
        shutil.copy2(source, BIN_PATH)
    BIN_PATH.chmod(0o755)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config({"sites": {}})
    LOCK_PATH.touch()
    for path in (CONFIG_DIR, CONFIG_PATH, LOCK_PATH):
        os.chown(path, account.pw_uid, account.pw_gid)

    if not HOSTS_BACKUP.exists():
        shutil.copy2(HOSTS_PATH, HOSTS_BACKUP)

    (UNIT_DIR / ENFORCER_UNIT).write_text(f"""[Unit]
Description=Siteblock: aplica a agenda no /etc/hosts

[Service]
ExecStart={python} {BIN_PATH} enforce
Restart=always
RestartSec=2
NoNewPrivileges=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
""", encoding="utf-8")
    (UNIT_DIR / UI_UNIT).write_text(f"""[Unit]
Description=Siteblock: interface local

[Service]
User={user}
ExecStart={python} {BIN_PATH} serve
Restart=on-failure
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
""", encoding="utf-8")
    install_shared_files(source)
    icon = LOGO_PATH if LOGO_PATH.is_file() else "preferences-system-privacy"

    DESKTOP_PATH.write_text(f"""[Desktop Entry]
Type=Application
Name=Siteblock
Comment=Bloquear e liberar sites
Exec=xdg-open http://127.0.0.1:{PORT}
Icon={icon}
Terminal=false
Categories=Utility;
""", encoding="utf-8")

    systemctl("daemon-reload")
    systemctl("enable", ENFORCER_UNIT, UI_UNIT)
    systemctl("restart", ENFORCER_UNIT, UI_UNIT)
    print(f"Pronto. Abra http://127.0.0.1:{PORT} ou procure Siteblock no menu de aplicativos.\n")
    print(EXTENSION_HELP.format(path=EXTENSION_DIR))


def uninstall_linux() -> None:
    require_root()
    systemctl("disable", "--now", ENFORCER_UNIT, UI_UNIT, check=False)
    for path in (UNIT_DIR / ENFORCER_UNIT, UNIT_DIR / UI_UNIT, DESKTOP_PATH, BIN_PATH):
        path.unlink(missing_ok=True)
    shutil.rmtree(SHARE_DIR, ignore_errors=True)
    systemctl("daemon-reload", check=False)
    remove_hosts_block()
    print(f"Removido. Sua lista continua em {CONFIG_DIR} e o hosts original em {HOSTS_BACKUP}.")


def remove_hosts_block() -> None:
    current = read_hosts()
    updated = render_hosts(current, [])
    if updated != current:
        write_hosts(updated)


def install_shared_files(source: Path) -> None:
    """Extensão e logo, iguais nos dois sistemas."""
    write_extension(EXTENSION_DIR)
    SHARE_DIR.chmod(0o755)
    logo_source = source.parent / LOGO_NAME
    if logo_source.is_file() and logo_source != LOGO_PATH:
        shutil.copyfile(logo_source, LOGO_PATH)
        LOGO_PATH.chmod(0o644)


# ----------------------------------------------------- instalação: Windows
#
# Os serviços do systemd viram duas tarefas no Agendador de Tarefas:
#   siteblock-enforcer  roda como SYSTEM desde o boot e reescreve o hosts.
#   siteblock-ui        roda como você quando você entra no Windows e serve a interface.
# As duas são reabertas a cada 5 minutos se tiverem parado (e ignoradas se ainda estiverem rodando).


def windows_run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = run_hidden(list(args))
    if check and result.returncode:
        message = (result.stderr or result.stdout).decode("oem", errors="replace").strip()
        raise SystemExit(f"Erro ao rodar {Path(args[0]).name}: {message}")
    return result


def schtasks(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return windows_run(str(SYSTEM32 / "schtasks.exe"), *args, check=check)


def windows_user() -> str:
    """A conta que abriu o terminal como administrador: é ela que usa a interface."""
    name, domain = os.environ.get("USERNAME", ""), os.environ.get("USERDOMAIN", "")
    if not name or name.upper() == "SYSTEM" or name.endswith("$"):
        raise SystemExit("Rode a partir da sua conta, em um terminal aberto como administrador.")
    return f"{domain}\\{name}" if domain else name


def windows_python() -> tuple[Path, Path]:
    """O python.exe (para o comando no terminal) e o pythonw.exe (para as tarefas, sem janela)."""
    python = Path(getattr(sys, "_base_executable", "") or sys.executable).resolve()
    if "WindowsApps" in python.parts:
        raise SystemExit(
            "O Python da Microsoft Store não pode ser usado pelas tarefas do Windows.\n"
            "Instale o Python de https://www.python.org/downloads/ e rode a instalação de novo."
        )
    pythonw = python.with_name("pythonw.exe")
    return python, pythonw if pythonw.is_file() else python


def short_path(path: Path) -> str:
    """Nome curto (8.3) do caminho: o cmd lê o .cmd na página de código do console, sem acentos."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        return buffer.value
    return str(path)


def write_cmd(python: Path) -> None:
    # Tudo numa linha com "exit /b": o cmd não tenta ler o arquivo de novo depois de um uninstall.
    for target in (str(python), short_path(python)):
        line = f'@"{target}" "{BIN_PATH}" %* & exit /b\r\n'
        try:
            CMD_PATH.write_bytes(line.encode("oem"))
            return
        except UnicodeEncodeError:
            continue
    raise SystemExit(f"Não consegui criar {CMD_PATH}: o caminho do Python tem caracteres não suportados.")


def task_xml(description: str, command: Path, arguments: str, user: str | None) -> str:
    """Definição da tarefa. Sem limite de duração e sem parar na bateria, ao contrário do padrão."""
    esc = lambda text: html.escape(str(text), quote=False)  # noqa: E731
    repeat = "<Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
    if user is None:  # enforcer: SYSTEM, no boot e a cada logon de qualquer conta
        triggers = (f"<BootTrigger>{repeat}<Enabled>true</Enabled></BootTrigger>"
                    "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>")
        principal = "<UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel>"
    else:  # interface: só a sua conta, sem privilégios, quando você entra
        triggers = f"<LogonTrigger>{repeat}<Enabled>true</Enabled><UserId>{esc(user)}</UserId></LogonTrigger>"
        principal = (f"<UserId>{esc(user)}</UserId><LogonType>InteractiveToken</LogonType>"
                     "<RunLevel>LeastPrivilege</RunLevel>")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{esc(description)}</Description></RegistrationInfo>
  <Triggers>{triggers}</Triggers>
  <Principals><Principal id="Author">{principal}</Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{esc(command)}</Command>
      <Arguments>{esc(arguments)}</Arguments>
      <WorkingDirectory>{esc(APP_DIR)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def register_task(name: str, xml: str) -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"{name}.xml"
        path.write_text(xml, encoding="utf-16")
        schtasks("/Create", "/TN", name, "/XML", str(path), "/F")


def windows_path_entry(add: bool) -> None:
    """Põe ou tira a pasta do Siteblock do PATH do sistema, para o comando "siteblock" funcionar."""
    import ctypes
    import winreg

    key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        value, kind = winreg.QueryValueEx(key, "Path")
        parts = [part for part in value.split(";") if part]
        mine = os.path.normcase(str(APP_DIR))
        others = [part for part in parts if os.path.normcase(part.rstrip("\\")) != mine]
        updated = others + [str(APP_DIR)] if add else others
        if updated == parts:
            return
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(updated))
    # Avisa o Windows: terminais abertos a partir de agora já enxergam o PATH novo.
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(ctypes.c_size_t()))


def install_windows() -> None:
    require_root()
    user = windows_user()
    python, pythonw = windows_python()
    if Path.home().resolve() in python.parents:
        print(
            "Aviso: o Python está instalado só para a sua conta, dentro da sua pasta de usuário.\n"
            "Funciona, mas o ideal é instalar o Python para todos os usuários (opção do instalador do\n"
            "python.org), porque o enforcer roda como SYSTEM com esse Python.\n"
        )

    for name in (ENFORCER_NAME, UI_NAME):  # numa atualização, para as tarefas antes de trocar os arquivos
        schtasks("/End", "/TN", name, check=False)

    APP_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    if source != BIN_PATH:
        shutil.copy2(source, BIN_PATH)
    write_cmd(python)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config({"sites": {}})
    LOCK_PATH.touch()
    # A interface roda como você e precisa editar a lista; o hosts continua só para o administrador.
    windows_run(str(SYSTEM32 / "icacls.exe"), str(CONFIG_DIR), "/grant", f"{user}:(OI)(CI)M", "/T", "/Q")

    if not HOSTS_BACKUP.exists():
        shutil.copy2(HOSTS_PATH, HOSTS_BACKUP)

    install_shared_files(source)
    SHORTCUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHORTCUT_PATH.write_text(f"[InternetShortcut]\r\nURL=http://127.0.0.1:{PORT}/\r\n", encoding="ascii")
    windows_path_entry(add=True)

    register_task(ENFORCER_NAME, task_xml(
        "Siteblock: aplica a agenda no arquivo hosts", pythonw, f'"{BIN_PATH}" enforce', None))
    register_task(UI_NAME, task_xml("Siteblock: interface local", pythonw, f'"{BIN_PATH}" serve', user))
    for name in (ENFORCER_NAME, UI_NAME):
        schtasks("/Run", "/TN", name)

    print(f"Pronto. Abra http://127.0.0.1:{PORT} ou procure Siteblock no menu Iniciar.")
    print("Para usar o comando siteblock, abra um terminal novo.\n")
    print(EXTENSION_HELP.format(path=EXTENSION_DIR))


def uninstall_windows() -> None:
    require_root()
    for name in (ENFORCER_NAME, UI_NAME):
        schtasks("/End", "/TN", name, check=False)
        schtasks("/Delete", "/TN", name, "/F", check=False)
    remove_hosts_block()
    windows_path_entry(add=False)
    SHORTCUT_PATH.unlink(missing_ok=True)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    print(f"Removido. Sua lista continua em {CONFIG_DIR} e o hosts original em {HOSTS_BACKUP}.")
    if APP_DIR.exists():
        print(f"Não consegui apagar tudo em {APP_DIR}; apague a pasta à mão.")


# --------------------------------------------------------------------- CLI


def expand_days(text: str) -> list[str]:
    days: list[str] = []
    for part in text.split(","):
        ends = part.strip().split("-")
        if len(ends) > 2 or any(day not in DAYS for day in ends):
            raise ValueError(f"Dia inválido: {part!r}. Use seg, ter, qua, qui, sex, sab, dom")
        first = DAYS.index(ends[0])
        last = DAYS.index(ends[-1])
        days.extend(DAYS[(first + step) % 7] for step in range((last - first) % 7 + 1))
    return days


def apply_schedule_specs(site: dict, specs: list[str]) -> None:
    """Exemplos de spec: seg-sex=09:00-18:00  sab,dom=livre  qua=tudo"""
    schedule = dict(site["schedule"])
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"Use DIAS=HORÁRIOS, como seg-sex=09:00-18:00 (recebi {spec!r})")
        days_text, value = spec.split("=", 1)
        if value == "livre":
            intervals = []
        elif value == "tudo":
            intervals = ["00:00-24:00"]
        else:
            intervals = [item for item in value.split(",") if item]
        for day in expand_days(days_text):
            schedule[day] = intervals
    site["schedule"] = normalize_schedule(schedule)


def print_list() -> None:
    config = load_config()
    moment = datetime.now()
    if not config["sites"]:
        print("Nenhum site na lista.")
    for domain in sorted(config["sites"]):
        status = site_status(config["sites"][domain], moment)
        line = f"{domain}: {status['label']}"
        if status["cooldown_seconds"] and not status["temp_seconds"]:
            line += f" (nova liberação em {duration_label(status['cooldown_seconds'])})"
        print(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="siteblock", description="Bloqueia sites com agenda semanal.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="mostra os sites e o estado de cada um")
    commands.add_parser("add", help="adiciona sites, bloqueados o tempo todo").add_argument("domains", nargs="+")
    edit = commands.add_parser("edit", help="troca o endereço de um site")
    edit.add_argument("domain")
    edit.add_argument("new_domain")
    commands.add_parser("rm", help="remove um site").add_argument("domain")
    schedule = commands.add_parser(
        "schedule", help="define horários bloqueados: seg-sex=09:00-18:00 sab,dom=livre qua=tudo"
    )
    schedule.add_argument("domain")
    schedule.add_argument("specs", nargs="+")
    unblock = commands.add_parser("unblock", help=f"libera por {MIN_UNBLOCK} a {MAX_UNBLOCK} minutos")
    unblock.add_argument("domain")
    unblock.add_argument("minutes", type=int)
    commands.add_parser("block", help="encerra uma liberação temporária").add_argument("domain")
    commands.add_parser("extension", help="grava a extensão do navegador em uma pasta").add_argument("dir")
    for name, text in (("enforce", "aplica a agenda (serviço root/SYSTEM)"), ("serve", "abre a interface"),
                       ("install", "instala os serviços"), ("uninstall", "remove tudo")):
        commands.add_parser(name, help=text)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    command = args.command
    try:
        if command == "enforce":
            if WINDOWS:
                log_to_file(ENFORCER_LOG)
            enforce_forever()
        elif command == "serve":
            log_to_file(UI_LOG)
            serve()
        elif command == "install":
            install()
        elif command == "uninstall":
            uninstall()
        elif command == "list":
            print_list()
        elif command == "extension":
            target = Path(args.dir).expanduser().resolve()
            write_extension(target)
            print(EXTENSION_HELP.format(path=target))
        else:
            with editing_config() as config:
                if command == "add":
                    for domain in args.domains:
                        print(f"{add_site(config, domain)} adicionado, bloqueado o tempo todo.")
                elif command == "edit":
                    update_site(config, normalize_domain(args.domain), args.new_domain, None)
                elif command == "rm":
                    remove_site(config, normalize_domain(args.domain))
                elif command == "schedule":
                    apply_schedule_specs(require_site(config, normalize_domain(args.domain)), args.specs)
                elif command == "unblock":
                    unblock_site(config, normalize_domain(args.domain), args.minutes)
                elif command == "block":
                    block_now(config, normalize_domain(args.domain))
            print_list()
    except (OSError, ValueError) as error:
        raise SystemExit(f"Erro: {error}") from None


if __name__ == "__main__":
    main()
