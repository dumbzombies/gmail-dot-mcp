"""Stdio smoke test: speak MCP to server.py the way a client does.

    python tests/stdio_smoke.py

Starts the server, initializes, lists tools, then exercises new_address / inbox /
wait_code (short timeout) and prints the raw JSON-RPC replies so schema or
serialization problems show up instead of hiding behind a client wrapper.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

SERVER = pathlib.Path(__file__).resolve().parent.parent / "server.py"


class Client:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False) -> dict:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self.next_id += 1
            payload["id"] = self.next_id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        if notify:
            return {}
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed the stream")
            line = line.strip()
            if not line:
                continue
            reply = json.loads(line)
            if reply.get("id") == payload["id"]:
                return reply

    def call(self, name: str, arguments: dict | None = None) -> dict:
        return self.send("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def text_of(reply: dict) -> str:
    result = reply.get("result") or {}
    content = result.get("content") or []
    return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))


def main() -> int:
    client = Client()
    failures: list[str] = []
    try:
        init = client.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stdio-smoke", "version": "1"},
            },
        )
        print("initialize:", (init.get("result") or {}).get("serverInfo"))
        client.send("notifications/initialized", notify=True)

        tools = client.send("tools/list", {})
        names = [tool["name"] for tool in (tools.get("result") or {}).get("tools", [])]
        print("tools:", names)
        for expected in ("new_address", "wait_code", "inbox"):
            if expected not in names:
                failures.append(f"missing tool {expected}")
        for tool in (tools.get("result") or {}).get("tools", []):
            if not tool.get("description"):
                failures.append(f"tool {tool['name']} has no description")

        created = client.call("new_address", {"label": "smoke"})
        created_text = text_of(created)
        print("new_address:", created_text.replace("\n", " ")[:200])
        try:
            address = json.loads(created_text)["address"]
        except Exception:
            failures.append(f"new_address returned unparseable payload: {created_text[:200]!r}")
            address = ""

        listed = client.call("inbox", {"address": address, "limit": 3})
        listed_text = text_of(listed)
        print("inbox:", listed_text.replace("\n", " ")[:200])
        try:
            payload = json.loads(listed_text)
            if not isinstance(payload.get("messages"), list):
                failures.append("inbox payload has no messages list")
        except Exception:
            failures.append(f"inbox returned unparseable payload: {listed_text[:200]!r}")

        waited = client.call("wait_code", {"address": address, "timeout": 8})
        waited_text = text_of(waited)
        print("wait_code:", waited_text.replace("\n", " ")[:300])
        try:
            payload = json.loads(waited_text)
            if "found" not in payload:
                failures.append("wait_code payload has no found flag")
        except Exception:
            failures.append(f"wait_code returned unparseable payload: {waited_text[:300]!r}")

        bad = client.call("new_address", {"mode": "nonsense"})
        if not (bad.get("result") or {}).get("isError"):
            failures.append("invalid mode was not reported as an error")

        missing = client.call("wait_code", {"address": "nobody@example.com", "timeout": 5})
        if not (missing.get("result") or {}).get("isError"):
            failures.append("unknown alias was not reported as an error")
        print("error paths:", text_of(missing)[:160].replace("\n", " "))
    finally:
        client.close()
        try:
            stderr = client.proc.stderr.read() if client.proc.stderr else ""
        except Exception:
            stderr = ""
        noisy = [line for line in stderr.splitlines() if "Traceback" in line or "Error" in line]
        if noisy:
            print("stderr noise:", noisy[:5])

    if failures:
        print("\nFAILURES:")
        for item in failures:
            print(" -", item)
        return 1
    print("\nstdio smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())