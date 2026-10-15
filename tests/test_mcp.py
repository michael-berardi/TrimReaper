"""The MCP server: the protocol handshake, the tool list, and tools that act exactly like the CLI."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from helpers import Home

from trimreaper import mcp


class Server(Home):
    def rpc(self, *messages):
        out = io.StringIO()
        mcp.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def tool(self, name, **arguments):
        reply = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})[0]
        return reply["result"]

    def test_handshake_and_tool_list(self):
        replies = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
                           {"jsonrpc": "2.0", "method": "notifications/initialized"},
                           {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                           {"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(len(replies), 3, "a notification gets no answer")
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertEqual(names, ["job_add", "job_status", "job_list", "reap_status", "reap", "mark"])
        self.assertNotIn("owner_acknowledge", names)
        self.assertEqual(replies[2]["error"]["code"], -32601)

    def test_reap_is_a_dry_run_unless_asked(self):
        repo = self.clone("app-j0200")
        self.put(repo, "node_modules/pkg/index.js")
        self.assertFalse(self.tool("job_add", id="J-0200", cwd=str(repo))["isError"])
        self.assertFalse(self.tool("job_status", id="J-0200", status="done")["isError"])
        with self.state() as conn:
            conn.execute("UPDATE jobs SET finished_at=finished_at-3600")

        dry = self.tool("reap", job="J-0200")
        self.assertEqual([i["outcome"] for i in dry["structuredContent"]["items"]], ["would-delete"])
        self.assertTrue((repo / "node_modules").exists())

        real = self.tool("reap", job="J-0200", dryRun=False)
        self.assertEqual(real["structuredContent"]["deleted"], 1)
        self.assertFalse((repo / "node_modules").exists())

    def test_refusals_and_bad_arguments_are_tool_errors_with_a_cause(self):
        self.tool("job_add", id="J-0201")
        refused = self.tool("reap", job="J-0201")
        self.assertTrue(refused["isError"])
        self.assertIn("job-running", refused["structuredContent"]["error"])
        self.assertIn("J-0042", self.tool("job_add", id="J-42")["structuredContent"]["error"])
        self.assertTrue(self.tool("reap", target="scratch", job="J-0201")["isError"])
        self.assertTrue(self.tool("reap", job="J-0201", dryRun="no")["isError"])
        unknown = self.rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "owner_acknowledge", "arguments": {}}})[0]
        self.assertEqual(unknown["error"]["code"], -32602)

    def test_the_cli_serves_it_over_stdio(self):
        src = str(Path(mcp.__file__).parents[1])
        done = subprocess.run([sys.executable, "-m", "trimreaper", "mcp"], input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n",
                              capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": src})
        self.assertEqual(json.loads(done.stdout), {"jsonrpc": "2.0", "id": 1, "result": {}})
