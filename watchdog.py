import argparse
import datetime
import json
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_INCIDENTS_PATH = "incidents.jsonl"
DEFAULT_STATE_PATH = ".watchdog_state.json"
DEFAULT_TIMEOUT = 5.0
DEFAULT_INTERVAL = 30.0
DEFAULT_THRESHOLD = 3
DEFAULT_COOLDOWN = 300.0
TELEGRAM_API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"


def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    services = raw.get("services", [])
    if not isinstance(services, list) or not services:
        raise ValueError("config has no services")
    for service in services:
        if not service.get("name") or not service.get("url"):
            raise ValueError("each service needs name and url")
    return {
        "check_interval": float(raw.get("check_interval", DEFAULT_INTERVAL)),
        "failure_threshold": int(raw.get("failure_threshold", DEFAULT_THRESHOLD)),
        "incidents_file": raw.get("incidents_file", DEFAULT_INCIDENTS_PATH),
        "telegram": raw.get("telegram") or {},
        "services": services,
    }


def build_target_url(service):
    base = service.get("url", "").rstrip("/")
    path = service.get("health_path", "/") or "/"
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def parse_host_port(service_url, fallback_port=80):
    target = service_url
    if "://" not in target:
        target = "http://" + target
    parsed = urllib.parse.urlparse(target)
    host = parsed.hostname
    if not host:
        raise ValueError("unparseable host in url: " + service_url)
    if parsed.port:
        return host, parsed.port
    if parsed.scheme == "https":
        return host, 443
    return host, fallback_port


def probe_http(target_url, timeout):
    request = urllib.request.Request(target_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except Exception:
        return False
    return 200 <= status < 400


def probe_tcp(service_url, timeout):
    try:
        host, port = parse_host_port(service_url)
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_service(service, timeout):
    effective = service.get("timeout", timeout)
    if service.get("probe", "http") == "tcp":
        return probe_tcp(service.get("url", ""), effective)
    return probe_http(build_target_url(service), effective)


def run_restart_command(service, dry_run=False):
    command = service.get("restart_command", "")
    if dry_run or not command:
        return {"executed": False, "returncode": None, "command": command, "dry_run": dry_run}
    try:
        completed = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    except Exception as error:
        return {"executed": False, "returncode": -1, "command": command, "error": str(error)}
    return {"executed": True, "returncode": completed.returncode, "command": command, "stdout": completed.stdout, "stderr": completed.stderr}


def append_incident(incidents_path, incident):
    with open(incidents_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(incident) + "\n")


def format_alert(name, failures, restart_info):
    return "<b>watchdog</b> service {name} unhealthy {count}x, restart returncode={code}".format(name=name, count=failures, code=restart_info.get("returncode"))


def send_telegram_message(telegram_config, message):
    telegram_config = telegram_config or {}
    if not telegram_config.get("enabled", False):
        return False
    token = telegram_config.get("bot_token", "")
    chat_id = telegram_config.get("chat_id", "")
    if not token or not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": message, "parse_mode": telegram_config.get("parse_mode", "HTML")}
    request = urllib.request.Request(TELEGRAM_API_TEMPLATE.format(token=token), data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def service_cooldown_remaining(service, last_restart, now):
    if not last_restart:
        return 0
    remaining = float(service.get("cooldown", DEFAULT_COOLDOWN)) - (now - float(last_restart))
    return remaining if remaining > 0 else 0


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {"failures": {}, "last_restart": {}}
    return {"failures": raw.get("failures", {}), "last_restart": raw.get("last_restart", {})}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def check_all_services(config, default_timeout=DEFAULT_TIMEOUT, dry_run=False):
    results = []
    for service in config["services"]:
        name = service.get("name", "unknown")
        target = service.get("url", "")
        if service.get("probe", "http") != "tcp":
            target = build_target_url(service)
        if dry_run:
            print("DRY-RUN {name}: would probe {target}".format(name=name, target=target))
            results.append({"name": name, "target": target, "healthy": None, "dry_run": True})
            continue
        healthy = probe_service(service, default_timeout)
        print("{status} {name}: {target}".format(status="OK" if healthy else "FAIL", name=name, target=target))
        results.append({"name": name, "target": target, "healthy": healthy, "dry_run": False})
    return results


def handle_service(service, state, failure_threshold, default_timeout, incidents_path, telegram_config, dry_run, now):
    name = service.get("name", "unknown")
    failures = state.setdefault("failures", {})
    restarts = state.setdefault("last_restart", {})
    if dry_run:
        print("DRY-RUN {name}: would probe and restart on failure".format(name=name))
        return {"name": name, "healthy": None, "failures": failures.get(name, 0), "restarted": False, "restart": {"executed": False, "dry_run": True}, "cooldown_remaining": 0, "telegram_sent": False, "dry_run": True}
    healthy = probe_service(service, default_timeout)
    count = failures.get(name, 0)
    count = 0 if healthy else count + 1
    failures[name] = count
    threshold = service.get("failure_threshold", failure_threshold)
    remaining = service_cooldown_remaining(service, restarts.get(name), now)
    result = {"name": name, "healthy": healthy, "failures": count, "restarted": False, "restart": {"executed": False}, "cooldown_remaining": remaining, "telegram_sent": False, "dry_run": False}
    if healthy or count < threshold or remaining > 0:
        print("{status} {name}: failures={count}".format(status="OK" if healthy else "WARN", name=name, count=count))
        return result
    restart_info = run_restart_command(service)
    restarts[name] = now
    failures[name] = 0
    result["restart"] = restart_info
    result["restarted"] = bool(restart_info.get("executed"))
    result["failures"] = 0
    append_incident(incidents_path, {"timestamp": datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).isoformat(), "service": name, "url": service.get("url", ""), "healthy": False, "failures": count, "restart_command": service.get("restart_command", ""), "returncode": restart_info.get("returncode"), "restarted": result["restarted"]})
    result["telegram_sent"] = send_telegram_message(telegram_config, format_alert(name, count, restart_info))
    print("RESTART {name}: returncode={code}".format(name=name, code=restart_info.get("returncode")))
    return result


def run_all_services(config, state, default_timeout=DEFAULT_TIMEOUT, incidents_path=None, dry_run=False, now=None):
    active = now if now is not None else time.time()
    path = incidents_path or config.get("incidents_file", DEFAULT_INCIDENTS_PATH)
    return [handle_service(service, state, config.get("failure_threshold", DEFAULT_THRESHOLD), default_timeout, path, config.get("telegram", {}), dry_run, active) for service in config["services"]]


def build_parser():
    parser = argparse.ArgumentParser(prog="watchdog")
    sub = parser.add_subparsers(dest="command", required=True)
    checker = sub.add_parser("check", help="probe services once, no restarts")
    checker.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    checker.add_argument("--timeout", type=float, default=None)
    checker.add_argument("--dry-run", action="store_true")
    runner = sub.add_parser("run", help="probe once and restart failures")
    runner.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    runner.add_argument("--state", default=DEFAULT_STATE_PATH)
    runner.add_argument("--incidents", default=None)
    runner.add_argument("--timeout", type=float, default=None)
    runner.add_argument("--dry-run", action="store_true")
    daemon = sub.add_parser("daemon", help="loop run forever")
    daemon.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    daemon.add_argument("--state", default=DEFAULT_STATE_PATH)
    daemon.add_argument("--incidents", default=None)
    daemon.add_argument("--timeout", type=float, default=None)
    daemon.add_argument("--interval", type=float, default=None)
    daemon.add_argument("--iterations", type=int, default=0)
    daemon.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as error:
        print("config error: {error}".format(error=error), file=sys.stderr)
        return 2
    timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT
    if args.command == "check":
        results = check_all_services(config, timeout, args.dry_run)
        return 1 if any(item.get("healthy") is False for item in results) else 0
    state = load_state(args.state)
    incidents = args.incidents or config.get("incidents_file", DEFAULT_INCIDENTS_PATH)
    if args.command == "run":
        results = run_all_services(config, state, timeout, incidents, args.dry_run)
        if not args.dry_run:
            save_state(args.state, state)
        return 1 if any(item.get("healthy") is False for item in results) else 0
    interval = args.interval if args.interval is not None else float(config.get("check_interval", DEFAULT_INTERVAL))
    completed = 0
    try:
        while True:
            run_all_services(config, state, timeout, incidents, args.dry_run)
            if not args.dry_run:
                save_state(args.state, state)
            completed += 1
            if args.iterations and completed >= args.iterations:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
