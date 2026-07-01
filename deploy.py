#!/usr/bin/env python3
"""
deploy.py — Full deployment of the Awareness Newsletter app on RHEL 9.8.

Copy this project to the target RHEL 9.8 VM and run:

    sudo python3 deploy.py

What it does (9 steps):
  1  Preflight checks (root, RHEL, project layout)
  2  Install Node.js 20 via dnf module stream
  3  Install nginx via dnf
  4  Build production artifact (dist/) with npm ci + build-dist
  5  Deploy files to /var/www/awareness/
  6  Write nginx config  (/etc/nginx/conf.d/awareness.conf)
  7  Fix SELinux file context
  8  Open port 80 in firewalld
  9  Enable + start nginx, health-check

Re-running is safe — all steps are idempotent.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT  = Path(__file__).parent.resolve()
AWARENESS_DIR = PROJECT_ROOT / "awareness"
DIST_DIR      = AWARENESS_DIR / "dist"
WEB_ROOT      = Path("/var/www/awareness")
NGINX_CONF    = Path("/etc/nginx/conf.d/awareness.conf")
NGINX_DEFAULT = Path("/etc/nginx/conf.d/default.conf")

# ── ANSI colours (disabled if not a tty) ─────────────────────────────────────
_tty = sys.stdout.isatty()
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text

def banner(msg: str) -> None:
    line = "─" * 62
    print(f"\n{_c('1;36', line)}\n{_c('1;36', '  ' + msg)}\n{_c('1;36', line)}")

def step(n: int, total: int, msg: str) -> None:
    print(f"\n{_c('1', f'[{n}/{total}] {msg}')}")

def ok(msg: str)   -> None: print(f"  {_c('0;32', '✓')}  {msg}")
def warn(msg: str) -> None: print(f"  {_c('1;33', '⚠')}  {msg}")
def info(msg: str) -> None: print(f"  {_c('0;36', '→')}  {msg}")
def err(msg: str)  -> None: print(f"  {_c('0;31', '✗')}  {msg}", file=sys.stderr)

def die(msg: str, code: int = 1) -> None:
    err(msg)
    sys.exit(code)

# ── Shell helper ──────────────────────────────────────────────────────────────
def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=False,
        cwd=cwd,
        env={**os.environ, **(env or {})},
    )
    if check and result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr, file=sys.stderr)
        die(f"Command failed (exit {result.returncode}): {' '.join(str(c) for c in cmd)}")
    return result

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Preflight
# ─────────────────────────────────────────────────────────────────────────────
def check_root() -> None:
    if os.geteuid() != 0:
        die("Must run as root or with sudo:\n\n    sudo python3 deploy.py\n")
    ok("Running as root")

def check_rhel() -> None:
    osr = Path("/etc/os-release")
    if not osr.exists():
        warn("Cannot verify OS — /etc/os-release missing. Continuing.")
        return
    content = osr.read_text().lower()
    if "rhel" not in content and "red hat" not in content:
        warn("Non-RHEL OS detected. Script is designed for RHEL 9 — continuing anyway.")
    else:
        # Extract VERSION_ID
        for line in osr.read_text().splitlines():
            if line.startswith("VERSION_ID="):
                ver = line.split("=", 1)[1].strip('"')
                if not ver.startswith("9"):
                    warn(f"RHEL version {ver} detected — designed for 9.x. Continuing.")
                else:
                    ok(f"RHEL {ver} confirmed")
                break

def check_project() -> None:
    if not AWARENESS_DIR.is_dir():
        die(
            f"awareness/ directory not found at:\n    {AWARENESS_DIR}\n\n"
            "  Copy the full project to this VM and run deploy.py from its root."
        )
    if not (AWARENESS_DIR / "index.html").exists():
        die("awareness/index.html missing — project appears incomplete.")
    if not (AWARENESS_DIR / "package.json").exists():
        die("awareness/package.json missing — project appears incomplete.")
    ok(f"Project root: {PROJECT_ROOT}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Node.js 20
# ─────────────────────────────────────────────────────────────────────────────
def _node_version() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    r = run(["node", "--version"], capture=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else None

def install_nodejs() -> bool:
    """Install Node.js 20 via RHEL AppStream module. Returns True if available."""
    ver = _node_version()
    if ver:
        major = ver.lstrip("v").split(".")[0]
        if int(major) >= 20:
            ok(f"Node.js already installed: {ver}")
            return True
        else:
            warn(f"Node.js {ver} installed but need ≥20. Upgrading via dnf module.")

    info("Enabling nodejs:20 module stream …")
    r = run(["dnf", "module", "enable", "nodejs:20", "-y"], capture=True, check=False)
    if r.returncode != 0:
        # Module stream might already be active at v20 — check the error message.
        if "already" in (r.stderr or "").lower() or "enabled" in (r.stdout or "").lower():
            pass  # fine
        else:
            warn("dnf module enable nodejs:20 failed. Trying NodeSource repo …")
            r2 = run(
                ["bash", "-c",
                 "curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -"],
                capture=True, check=False,
            )
            if r2.returncode != 0:
                warn(
                    "NodeSource setup also failed.\n"
                    "  Node.js 20 is unavailable — will serve source directory "
                    "without building dist/."
                )
                return False

    info("Installing nodejs …")
    r = run(["dnf", "install", "nodejs", "-y"], capture=True, check=False)
    if r.returncode != 0:
        warn("dnf install nodejs failed — will serve source directory without building dist/.")
        return False

    ver = _node_version()
    if ver:
        ok(f"Node.js installed: {ver}")
        return True

    warn("Node.js install may have failed — will serve source directory as fallback.")
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — nginx
# ─────────────────────────────────────────────────────────────────────────────
def install_nginx() -> None:
    if shutil.which("nginx"):
        r = run(["nginx", "-v"], capture=True, check=False)
        ver = (r.stderr or r.stdout).strip()
        ok(f"nginx already installed: {ver}")
        return
    info("Installing nginx …")
    run(["dnf", "install", "nginx", "-y"])
    ok("nginx installed")

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Build
# ─────────────────────────────────────────────────────────────────────────────
def build_dist() -> bool:
    """Run npm ci + build-dist.mjs. Returns True on success."""
    info("Running npm ci …")
    r = run(["npm", "ci"], cwd=AWARENESS_DIR, capture=True, check=False)
    if r.returncode != 0:
        warn("npm ci failed — trying npm install …")
        r2 = run(["npm", "install"], cwd=AWARENESS_DIR, capture=True, check=False)
        if r2.returncode != 0:
            warn("npm install also failed — will serve source directory as fallback.")
            return False
    ok("npm dependencies installed")

    info("Building production artifact (dist/) …")
    r = run(
        ["node", "scripts/build-dist.mjs", "--force"],
        cwd=AWARENESS_DIR,
        capture=True,
        check=False,
    )
    if r.returncode != 0:
        warn("build-dist failed — will serve source directory as fallback.")
        if r.stdout:
            print(r.stdout[-2000:])  # last 2 KB of output for diagnostics
        return False

    ok("dist/ built successfully")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Deploy files
# ─────────────────────────────────────────────────────────────────────────────
def deploy_files(source_dir: Path, is_dist: bool) -> None:
    info(f"Deploying files to {WEB_ROOT} …")
    WEB_ROOT.mkdir(parents=True, exist_ok=True)

    if shutil.which("rsync"):
        # Trailing slash on source → copy contents into dest.
        run(["rsync", "-a", "--delete", f"{source_dir}/", f"{WEB_ROOT}/"])
    else:
        # Pure Python fallback.
        if WEB_ROOT.exists():
            shutil.rmtree(WEB_ROOT)
        shutil.copytree(source_dir, WEB_ROOT)

    # dist/ does NOT include vendor/ (CDN fallbacks).
    # On a private network the CDN may be unreachable — copy vendor/ explicitly.
    vendor_src = AWARENESS_DIR / "vendor"
    if is_dist and vendor_src.is_dir():
        vendor_dst = WEB_ROOT / "vendor"
        if vendor_dst.exists():
            shutil.rmtree(vendor_dst)
        shutil.copytree(vendor_src, vendor_dst)
        ok("vendor/ copied (offline CDN fallback)")

    # Ownership + permissions.
    run(["chown", "-R", "nginx:nginx", str(WEB_ROOT)])
    run(["chmod", "-R", "u=rwX,go=rX", str(WEB_ROOT)])
    ok(f"Files deployed to {WEB_ROOT}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — nginx config
# ─────────────────────────────────────────────────────────────────────────────
def _server_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))   # non-routable; no traffic sent
            return s.getsockname()[0]
    except Exception:
        return "_"

def _extra_deny_rules() -> str:
    """Extra deny rules needed when serving the full source tree (not dist/)."""
    return """\
    location ~* ^/(tests|scripts|docs|node_modules|nessus_advisory|experiments|article-seed|ensemble-logs|playwright-report|test-results|deploy|templates/reference|templates/imported-standalone)/ {
        deny all;
    }
    location = /babel.json              { deny all; }
    location = /MEMORY.md               { deny all; }
    location = /RESTRUCTURE.md          { deny all; }
    location = /README.md               { deny all; }
    location = /.nvmrc                  { deny all; }"""

# nginx config as a plain string with @@PLACEHOLDER@@ tokens so we avoid
# escaping every brace in a Python f-string.
_NGINX_TEMPLATE = """\
# Awareness Newsletter — nginx config (generated by deploy.py)
# Re-run deploy.py to regenerate.

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name @@SERVER_NAME@@ _;

    root @@WEB_ROOT@@;
    index index.html;

    server_tokens off;
    client_max_body_size 1m;

    # ── Security headers ─────────────────────────────────────────────────────
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=(), payment=(), usb=()" always;
    add_header Cross-Origin-Opener-Policy "same-origin" always;
    add_header Cross-Origin-Resource-Policy "same-origin" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob: https:; connect-src 'self' https://api.anthropic.com https://api.openai.com https://api.allorigins.win https://corsproxy.io https://api.codetabs.com https://api.rss2json.com https:; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'" always;

    # ── Compression ──────────────────────────────────────────────────────────
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/javascript application/javascript application/json image/svg+xml application/xml;

    # ── Legacy redirect ───────────────────────────────────────────────────────
    location = /builder.html { return 301 /index.html#section-home; }

    # ── Health endpoint ───────────────────────────────────────────────────────
    location = /health.html {
        access_log off;
        add_header Cache-Control "no-store" always;
    }

    # ── Deny hidden files and sensitive paths ─────────────────────────────────
    location ~ /\.                       { deny all; }
    location ~* ^/(tests|scripts|docs|node_modules|ensemble-logs|playwright-report|test-results|deploy)/ {
        deny all;
    }
    location = /package.json            { deny all; }
    location = /package-lock.json       { deny all; }
    location = /eslint.config.js        { deny all; }
    location = /playwright.config.js    { deny all; }
    location = /baseline-critical-path-audit-results.json { deny all; }
@@EXTRA_DENY@@
    # ── Cache by file type ────────────────────────────────────────────────────
    location ~* \.html?$ {
        add_header Cache-Control "public, max-age=0, must-revalidate" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob: https:; connect-src 'self' https://api.anthropic.com https://api.openai.com https://api.allorigins.win https://corsproxy.io https://api.codetabs.com https://api.rss2json.com https:; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'" always;
        try_files $uri /index.html;
    }

    location ~* \.(js|css)$ {
        add_header Cache-Control "public, max-age=86400, must-revalidate" always;
    }

    location ~* \.(jpg|jpeg|png|gif|webp|ico|svg|woff|woff2|ttf|eot)$ {
        add_header Cache-Control "public, max-age=604800" always;
    }

    # ── SPA fallback ──────────────────────────────────────────────────────────
    location / {
        try_files $uri $uri/ /index.html;
    }
}
"""

def configure_nginx(is_dist: bool) -> None:
    server_ip = _server_ip()
    extra = _extra_deny_rules() if not is_dist else ""
    conf = (
        _NGINX_TEMPLATE
        .replace("@@SERVER_NAME@@", server_ip)
        .replace("@@WEB_ROOT@@", str(WEB_ROOT))
        .replace("@@EXTRA_DENY@@\n", (extra + "\n\n") if extra else "")
    )

    # Disable the default catch-all config (conflicts with default_server).
    if NGINX_DEFAULT.exists():
        NGINX_DEFAULT.rename(NGINX_DEFAULT.with_suffix(".conf.disabled"))
        info("Disabled /etc/nginx/conf.d/default.conf (renamed .disabled)")

    NGINX_CONF.write_text(conf)
    ok(f"nginx config written: {NGINX_CONF}")

    r = run(["nginx", "-t"], capture=True, check=False)
    if r.returncode != 0:
        err("nginx -t failed:")
        print(r.stderr)
        die("Fix the nginx config error above and re-run deploy.py.")
    ok("nginx config syntax OK")

# ─────────────────────────────────────────────────────────────────────────────
# Step 7 — SELinux
# ─────────────────────────────────────────────────────────────────────────────
def fix_selinux() -> None:
    r = run(["getenforce"], capture=True, check=False)
    if r.returncode != 0:
        info("getenforce not found — SELinux step skipped.")
        return
    mode = r.stdout.strip().lower()
    if mode == "disabled":
        info("SELinux disabled — skipping context fix.")
        return

    info(f"SELinux mode: {mode}. Setting httpd_sys_content_t on {WEB_ROOT} …")
    # chcon works immediately; restorecon makes it survive a relabel.
    run(["chcon", "-R", "-t", "httpd_sys_content_t", str(WEB_ROOT)], check=False)
    run(["restorecon", "-Rv", str(WEB_ROOT)], capture=True, check=False)
    ok("SELinux context set (httpd_sys_content_t)")

# ─────────────────────────────────────────────────────────────────────────────
# Step 8 — Firewall
# ─────────────────────────────────────────────────────────────────────────────
def configure_firewall() -> None:
    if not shutil.which("firewall-cmd"):
        warn("firewall-cmd not found — skipping firewall config.")
        return

    r = run(["systemctl", "is-active", "firewalld"], capture=True, check=False)
    if r.stdout.strip() != "active":
        warn("firewalld is not running — skipping firewall config.")
        return

    info("Opening HTTP (port 80) in firewalld …")
    run(["firewall-cmd", "--permanent", "--add-service=http"])
    run(["firewall-cmd", "--reload"])
    ok("firewalld: port 80 open")

# ─────────────────────────────────────────────────────────────────────────────
# Step 9 — Start nginx + health check
# ─────────────────────────────────────────────────────────────────────────────
def start_nginx() -> None:
    run(["systemctl", "enable", "nginx"])

    r = run(["systemctl", "is-active", "nginx"], capture=True, check=False)
    if r.stdout.strip() == "active":
        run(["systemctl", "reload", "nginx"])
        ok("nginx reloaded")
    else:
        run(["systemctl", "start", "nginx"])
        ok("nginx started and enabled")

def health_check() -> None:
    url = "http://127.0.0.1/health.html"
    info(f"Health check → {url}")
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    ok(f"Health check passed (HTTP {resp.status})")
                    return
                warn(f"Unexpected HTTP {resp.status} from {url}")
                return
        except Exception as exc:
            if attempt < 5:
                time.sleep(1)
            else:
                warn(f"Health check failed after 6 tries: {exc}")
                warn("Check:  journalctl -u nginx -n 50")

# ─────────────────────────────────────────────────────────────────────────────
# Success summary
# ─────────────────────────────────────────────────────────────────────────────
def print_success(serving: str) -> None:
    ip = _server_ip()
    banner("Deployment complete")
    print(f"""
  Serving: {_c('1', serving)}

  URLs:
    Local    →  {_c('1;36', 'http://127.0.0.1/')}
    Network  →  {_c('1;36', f'http://{ip}/')}

  Useful commands:
    systemctl status nginx
    journalctl -u nginx -f
    systemctl reload nginx

  Files:
    Web root  {WEB_ROOT}
    Config    {NGINX_CONF}
""")

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    TOTAL = 9
    banner("Awareness Newsletter — RHEL 9.8 Deployment Script")

    step(1, TOTAL, "Preflight checks")
    check_root()
    check_rhel()
    check_project()

    step(2, TOTAL, "Install Node.js 20")
    node_ok = install_nodejs()

    step(3, TOTAL, "Install nginx")
    install_nginx()

    step(4, TOTAL, "Build production artifact (dist/)")
    if node_ok:
        built = build_dist()
    else:
        warn("Node.js unavailable — skipping build.")
        built = False

    # Choose source: dist/ if build succeeded, else awareness/ directly.
    if built and DIST_DIR.is_dir():
        web_source = DIST_DIR
        is_dist    = True
        info("Using dist/ (clean production artifact)")
    else:
        web_source = AWARENESS_DIR
        is_dist    = False
        warn("Falling back to awareness/ source directory")

    step(5, TOTAL, "Deploy files to web root")
    deploy_files(web_source, is_dist)

    step(6, TOTAL, "Configure nginx")
    configure_nginx(is_dist)

    step(7, TOTAL, "Fix SELinux file context")
    fix_selinux()

    step(8, TOTAL, "Configure firewall")
    configure_firewall()

    step(9, TOTAL, "Start nginx + health check")
    start_nginx()
    health_check()

    serving_label = "dist/ (production build)" if is_dist else "awareness/ (source — Node.js was unavailable)"
    print_success(serving_label)


if __name__ == "__main__":
    main()
