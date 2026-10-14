"""Deterministic local MCP test server for Phase 7.6 stdio transport tests.

Communicates over standard I/O using newline-delimited JSON-RPC 2.0.
Contains ZERO network dependencies and ZERO third-party libraries.
"""
import argparse
import base64
import json
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic MCP Stdio Test Server")
    parser.add_argument("--crash-on-start", action="store_true", help="Exit immediately with code 99")
    parser.add_argument("--hang-on-start", action="store_true", help="Sleep indefinitely on start")
    parser.add_argument("--junk-on-start", action="store_true", help="Output garbage to stdout on start")
    parser.add_argument("--log-to-stderr", action="store_true", help="Log diagnostic messages to stderr")
    parser.add_argument("--server-name", default="sutra-test-stdio-server", help="Server name")
    parser.add_argument("--server-version", default="1.0.0", help="Server version")
    args = parser.parse_args()

    if args.log_to_stderr:
        sys.stderr.write("[test-server] Server starting up\n")
        sys.stderr.flush()

    if args.crash_on_start:
        if args.log_to_stderr:
            sys.stderr.write("[test-server] Crashing on startup as requested\n")
            sys.stderr.flush()
        sys.exit(99)

    if args.hang_on_start:
        while True:
            time.sleep(10)

    if args.junk_on_start:
        sys.stdout.write("<<<THIS IS INVALID PROTOCOL JUNK>>>\n")
        sys.stdout.flush()

    # Main line-based JSON-RPC dispatch loop
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except Exception as exc:
            if args.log_to_stderr:
                sys.stderr.write(f"[test-server] Failed parsing JSON: {exc}\n")
                sys.stderr.flush()
            continue

        if not isinstance(req, dict):
            continue

        msg_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # Handle one-way notifications (no id)
        if msg_id is None:
            if method == "notifications/initialized":
                if args.log_to_stderr:
                    sys.stderr.write("[test-server] Client sent notifications/initialized\n")
                    sys.stderr.flush()
            continue

        # Handle requests
        res = None
        err = None

        if method == "initialize":
            client_version = params.get("protocolVersion", "2026-07-28")
            res = {
                "protocolVersion": client_version,
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"subscribe": True, "listChanged": True},
                    "prompts": {"listChanged": True},
                    "logging": {},
                },
                "serverInfo": {
                    "name": args.server_name,
                    "version": args.server_version,
                },
                "instructions": "Deterministic stdio test server for Sutra",
            }

        elif method == "ping":
            res = {}

        elif method == "tools/list":
            cursor = params.get("cursor")
            if cursor == "page_2":
                res = {
                    "tools": [
                        {
                            "name": "tool_page_2",
                            "description": "Page 2 tool",
                            "inputSchema": {"type": "object"},
                        }
                    ],
                }
            elif cursor == "paginate":
                res = {
                    "tools": [
                        {
                            "name": "tool_page_1",
                            "description": "Page 1 tool",
                            "inputSchema": {"type": "object"},
                        }
                    ],
                    "nextCursor": "page_2",
                }
            else:
                res = {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echoes back the message",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                            },
                        },
                        {
                            "name": "add",
                            "description": "Adds two numbers",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                            },
                        },
                        {
                            "name": "emit_stderr",
                            "description": "Emits lines to stderr",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                            },
                        },
                        {
                            "name": "crash",
                            "description": "Immediately exits process with code 42",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "sleep",
                            "description": "Sleeps for specified duration",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"seconds": {"type": "number"}},
                            },
                        },
                        {
                            "name": "malformed_output",
                            "description": "Prints malformed non-JSON data to stdout",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "secret_tool",
                            "description": "Returns sensitive token string",
                            "inputSchema": {"type": "object"},
                        },
                    ]
                }

        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments") or {}

            if tool_name == "echo":
                msg_val = tool_args.get("message", "")
                res = {
                    "content": [{"type": "text", "text": f"echo: {msg_val}"}],
                    "isError": False,
                }
            elif tool_name == "add":
                a = float(tool_args.get("a", 0))
                b = float(tool_args.get("b", 0))
                res = {
                    "content": [{"type": "text", "text": str(a + b)}],
                    "structuredContent": {"sum": a + b},
                    "isError": False,
                }
            elif tool_name == "emit_stderr":
                stderr_text = tool_args.get("text", "Diagnostics on stderr")
                sys.stderr.write(f"[test-server-stderr] {stderr_text}\n")
                sys.stderr.flush()
                res = {
                    "content": [{"type": "text", "text": "Wrote to stderr"}],
                    "isError": False,
                }
            elif tool_name == "crash":
                if args.log_to_stderr:
                    sys.stderr.write("[test-server] Crashing on command\n")
                    sys.stderr.flush()
                sys.exit(42)
            elif tool_name == "sleep":
                secs = float(tool_args.get("seconds", 1.0))
                time.sleep(secs)
                res = {
                    "content": [{"type": "text", "text": f"Slept for {secs}s"}],
                    "isError": False,
                }
            elif tool_name == "malformed_output":
                sys.stdout.write("<<<CORRUPT NON-JSON OUTPUT LINE>>>\n")
                sys.stdout.flush()
                continue
            elif tool_name == "secret_tool":
                res = {
                    "content": [{"type": "text", "text": "Token returned: sk-ant-api03-abcdef1234567890abcdef"}],
                    "isError": False,
                }
            else:
                err = {"code": -32601, "message": f"Method or tool not found: {tool_name}"}

        elif method == "resources/list":
            res = {
                "resources": [
                    {
                        "uri": "test://resource/1",
                        "name": "Resource One",
                        "description": "First test resource",
                        "mimeType": "text/plain",
                    },
                    {
                        "uri": "test://resource/binary",
                        "name": "Binary Resource",
                        "mimeType": "application/octet-stream",
                    },
                ]
            }

        elif method == "resources/read":
            uri = params.get("uri")
            if uri == "test://resource/1":
                res = {
                    "contents": [
                        {"uri": uri, "mimeType": "text/plain", "text": "Hello from stdio resource 1"}
                    ]
                }
            elif uri == "test://resource/binary":
                sample_bytes = b"Binary stream payload"
                b64 = base64.b64encode(sample_bytes).decode("ascii")
                res = {
                    "contents": [
                        {"uri": uri, "mimeType": "application/octet-stream", "blob": b64}
                    ]
                }
            else:
                err = {"code": -32002, "message": f"Resource not found: {uri}"}

        elif method == "prompts/list":
            res = {
                "prompts": [
                    {
                        "name": "greeting",
                        "description": "Generates a greeting",
                        "arguments": [
                            {"name": "name", "description": "Person name", "required": True}
                        ],
                    }
                ]
            }

        elif method == "prompts/get":
            prompt_name = params.get("name")
            p_args = params.get("arguments") or {}
            if prompt_name == "greeting":
                target_name = p_args.get("name", "World")
                res = {
                    "description": f"Greeting for {target_name}",
                    "messages": [
                        {
                            "role": "user",
                            "content": {"type": "text", "text": f"Hello, {target_name}!"},
                        }
                    ],
                }
            else:
                err = {"code": -32601, "message": f"Prompt not found: {prompt_name}"}

        else:
            err = {"code": -32601, "message": f"Unknown method: {method}"}

        # Write response back on stdout
        resp_obj = {"jsonrpc": "2.0", "id": msg_id}
        if err is not None:
            resp_obj["error"] = err
        else:
            resp_obj["result"] = res

        out_line = json.dumps(resp_obj, separators=(",", ":")) + "\n"
        sys.stdout.write(out_line)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
