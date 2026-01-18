#!/usr/bin/env python3
import json
import subprocess
import time
import tempfile
import os
import threading
import socketserver
import http.server
import sys

SINGBOX = "/usr/bin/sing-box"
OPENSSL = "/usr/bin/openssl"
CURL = "/usr/bin/curl"

BASE_CLIENT_PORT = 15000
BASE_SERVER_PORT = 20000

HTTP_SERVER_PORT = 8000

DURATION = 12  # 稍微长一点更稳定
MAX_TEST_BYTES = 8 << 30  # 最多发 8GiB 防止意外

WORKDIR = tempfile.mkdtemp(prefix="sb-bench-")

# ─── HTTP infinite stream server ─────────────────────────────────────


class InfiniteHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/bench":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

        chunk = b"\0" * (1024 * 1024) * 1  # 1 MiB chunk
        sent = 0

        try:
            while sent < MAX_TEST_BYTES:
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            print("HTTP stream error:", e)

    def log_message(self, *args):
        pass


def start_http_server():
    httpd = socketserver.TCPServer(("127.0.0.1", HTTP_SERVER_PORT), InfiniteHandler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.6)
    return httpd


# ─── helpers ─────────────────────────────────────────────────────────


def gen_password(bits=256):
    byte_len = bits // 8
    cmd = [OPENSSL, "rand", "-base64", str(byte_len)]
    out = subprocess.check_output(cmd, text=True).strip()
    return out


def get_pwd_for_method(method):
    if "128" in method:
        return gen_password(128)
    return gen_password(256)


def write_cfg(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def start_singbox(cfg_path, name=""):
    p = subprocess.Popen(
        [SINGBOX, "run", "-c", cfg_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.8)

    # 快速检查是否启动成功
    if p.poll() is not None:
        err = p.stderr.read()
        print(f"{name} sing-box 启动失败:\n{err}")
        return None
    return p


def terminate_process(p):
    if not p:
        return
    try:
        p.terminate()
        p.wait(timeout=4)
    except Exception:
        pass
    finally:
        try:
            p.kill()
        except Exception:
            pass


# ─── config generators ───────────────────────────────────────────────


def gen_ss_config(method, password, server_port, client_port):
    # 2024+ 版本统一使用 shadowsocks 类型
    # ss2022 只是 method 名称不同

    server = {
        "log": {"level": "error", "timestamp": False},
        "inbounds": [
            {
                "type": "shadowsocks",
                "tag": "ss-in",
                "listen": "127.0.0.1",
                "listen_port": server_port,
                "method": method,
                "password": password,
            }
        ],
        "outbounds": [{"type": "direct", "tag": "direct"}],
    }

    client = {
        "log": {"level": "error", "timestamp": False},
        "inbounds": [
            {
                "type": "socks",
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "listen_port": client_port,
            }
        ],
        "outbounds": [
            {
                "type": "shadowsocks",
                "tag": "ss-out",
                "server": "127.0.0.1",
                "server_port": server_port,
                "method": method,
                "password": password,
            },
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"rules": [{"outbound": "ss-out"}]},
    }

    return server, client


# ─── benchmark ───────────────────────────────────────────────────────


def run_curl(client_port):
    cmd = [
        CURL,
        "--silent",
        "--show-error",
        "-o",
        "/dev/null",
        "-x",
        f"socks5h://127.0.0.1:{client_port}",
        "--max-time",
        str(DURATION + 2),
        f"http://127.0.0.1:{HTTP_SERVER_PORT}/bench",
        "-w",
        "%{speed_download}\\n",
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
        speed_bps = float(out)
        return speed_bps / (1024 * 1024)
    except Exception as e:
        print("curl failed:", e)
        if hasattr(e, "output"):
            print(e.output)
        return None


def main():
    httpd = start_http_server()

    tests = [
        ("none",),
        ("aes-128-gcm",),
        ("aes-256-gcm",),
        ("chacha20-ietf-poly1305",),
        ("2022-blake3-aes-128-gcm",),
        ("2022-blake3-aes-256-gcm",),
    ]

    results = []
    sp = BASE_SERVER_PORT
    cp = BASE_CLIENT_PORT

    for (method,) in tests:
        print(f"\n=== {method} ===")
        password = get_pwd_for_method(method)

        server_cfg, client_cfg = gen_ss_config(method, password, sp, cp)

        s_path = os.path.join(WORKDIR, f"server-{method}.json")
        c_path = os.path.join(WORKDIR, f"client-{method}.json")

        write_cfg(server_cfg, s_path)
        write_cfg(client_cfg, c_path)

        srv = start_singbox(s_path, "server")
        if not srv:
            results.append((method, None))
            sp += 1
            cp += 1
            continue

        cli = start_singbox(c_path, "client")
        if not cli:
            terminate_process(srv)
            results.append((method, None))
            sp += 1
            cp += 1
            continue

        time.sleep(1.2)

        speed_mib = run_curl(cp)
        if speed_mib is not None:
            gbps = speed_mib * 8 / 1000
            print(f"  {speed_mib:6.1f} MiB/s   ≈ {gbps:5.2f} Gbps")
            results.append((method, speed_mib))
        else:
            print("  FAILED")
            results.append((method, None))

        terminate_process(cli)
        terminate_process(srv)

        time.sleep(0.4)  # 给端口释放一点时间
        sp += 2
        cp += 2

    httpd.shutdown()
    httpd.server_close()

    print("\n" + "=" * 40)
    print("           SUMMARY")
    print("-" * 40)
    for method, speed in results:
        if speed is not None:
            gbps = speed * 8 / 1000
            print(f"{method:32} {speed:6.1f} MiB/s  {gbps:5.2f} Gbps")
        else:
            print(f"{method:32} FAILED")

    print("-" * 40)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
