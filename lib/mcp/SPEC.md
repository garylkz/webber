---
id: mcp
type: module-design
status: draft
title: webber MCP server
parent: webber-architecture
---

# webber MCP server: Specification

An MCP server that offers webber's tools to MCP clients that have no shell,
such as Claude Desktop. Agents with a shell can keep running the scripts
directly, as the `SKILL.md` files describe.

Implemented in [mcp_server.py](mcp_server.py).

---

## Usage

```bash
lib/.venv/bin/python lib/mcp/mcp_server.py                       # every tool
lib/.venv/bin/python lib/mcp/mcp_server.py --tools cinema,fourd  # some groups
lib/.venv/bin/python lib/mcp/mcp_server.py --http                # http://127.0.0.1:8766/mcp
```

Use stdio for a client that starts the server itself (Claude Code, pi,
OpenCode). Use `--http` for a client that connects to a running server, such
as Unsloth Studio's chat, which blocks local commands while its remote access
is on. `--host` and `--port` change the address.

A client config entry, with absolute paths:

```json
{
  "mcpServers": {
    "webber": {
      "command": "/path/to/webber/lib/.venv/bin/python",
      "args": ["/path/to/webber/lib/mcp/mcp_server.py"]
    }
  }
}
```

Use the venv's Python: the server needs `mcp` and the scripts it runs need
`requests`.

## Design

- **The scripts stay the interface.** Each MCP tool checks its arguments,
  builds a command line for one script and runs it in a subprocess from the
  repo root. Its stdout becomes the tool result as text. The server imports no
  scraper, so the rule that no module imports another still holds, and the
  shell script (`hacker_news_comments.sh`) is served the same way as the Python ones.
- **The official SDK.** The protocol is the `mcp` package's `MCPServer`
  (pinned to `mcp==2.3.0`): the handshake, version negotiation, `tools/list`,
  `tools/call`, errors, stdio and streamable HTTP. Each tool is an async
  function whose typed signature is its input schema and whose docstring is
  its description, so the file holds only the tools, their command lines and
  the rules between arguments.
- **Few, broad tools.** Every tool's schema sits in the client's context, so
  jobs are grouped: one tool per job, not per function. GSC and TGV share the
  `cinema_*` tools through a `chain` argument, which the cinema contract makes
  possible. Options that only one chain has are rejected for the other.
- **Groups.** Each tool has a group (`cinema`, `fourd`, `northport`, `meetup`,
  `goodreads`, `youtube`, `hacker-news`, `rps`, `air-quality`, `google-maps`), and `--tools` serves only the
  named ones.
- **Calls run on worker threads** (`anyio.to_thread`), so a slow price sweep
  does not hold up a ping or another call.
- **Local HTTP.** `--http` serves the SDK's streamable HTTP transport at
  `/mcp`. There is no auth, so it binds to 127.0.0.1, and the SDK refuses a
  request whose `Origin` names another host (DNS rebinding). The default port is
  8766; 8765 is the vault's MCP server and 8000 is omlx.

## Tools

| Tool | Runs |
| --- | --- |
| `cinema_list` | `gsc`/`tgv` `--cinemas`, `--regions`, `--dates`, or (gsc) `--movies` |
| `cinema_showtimes` | `gsc`/`tgv` with a cinema and date, or `--movie`, plus filters, `--seats`, `--prices` |
| `cinema_seat_map` | `gsc`/`tgv` `--seat-map --time` |
| `gsc_films` | `gsc` `--listing`, `--all` or `--detail` |
| `fourd_results` | `fourd` with a date, `--recent` or `--from`/`--to` |
| `northport_enquiry` | `northport` `container`, `bl`, `vessel` or `booking` |
| `meetup_events` | `meetup` list mode |
| `goodreads_shelf` | `goodreads` with a user id and shelf |
| `youtube_rss` | `youtube_rss.py`, optionally `--fetch` |
| `hacker_news_feed` | `hacker_news_feed.py --page` |
| `hacker_news_comments` | `hacker_news_comments.sh` |
| `rps_feed` | `rps_feed.py --start --count` |
| `rps_article` | `rps_article.py` |
| `air_quality_report` | `air_quality_report.py` with a place, an address or a point, optionally `--hours` |
| `google_maps_link` | `google_maps_link.py` |

Left out on purpose: the bulk modes that write files and run for a long time
(`fourd --all`/`--out`, `meetup --dump`), and the tuning flags (`--workers`,
`--no-delay`), so the tools keep the scripts' polite defaults.

## Arguments and errors

- The SDK checks types, enums, patterns and minimums before a tool runs.
  Fields are strict, so `"3"` and `true` are not taken as integers, and every
  pattern is anchored to match the whole value. The tool then checks the rules
  between arguments (for example `per_person` needs `prices`). An argument the
  schema does not name is ignored, not rejected.
- A value that becomes a positional argument may not start with `-`, and
  option values are passed as `--flag=value`, so no argument can turn into
  another option.
- A bad argument or a failed run is a tool result with `isError: true`, not a
  protocol error. A schema error carries the SDK's validation message. A rule
  between arguments or a failed run carries JSON: the script's own error JSON
  when it printed one on stdout, otherwise `{"error": ...}` with the reason or
  the end of its stderr.
- Each run times out after 300 seconds.

## Testing

`test_mcp.py` is offline. It talks to the server through the SDK's in-memory
`Client`, with `run` replaced so no script starts: it pins the tool names,
checks every schema, the argument checks and the rules between arguments, and
the command line each tool builds. Two tests start the real server process,
over stdio and over HTTP on a free port, and list its tools. Nothing in it
reaches a scraper.
