"""Allowlisted network ops tools for Aion."""
from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from config import CONFIG
from google_calendar import handle_calendar_message

log = logging.getLogger("aion.tools")

_HOST_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*[a-zA-Z0-9]$")
_DEFAULT_FFUF_WORDLIST = "/workspace/aion/data/admin_wordlists/ffuf_quick.txt"
_TOOL_REGISTRY = None
SAFE_LOCAL_DIAGNOSTIC_TOOL_IDS = frozenset(
    {
        "dig",
        "nmap_ping_sweep",
        "nslookup",
        "ping",
        "traceroute",
    }
)


@dataclass
class ToolInvocation:
    tool_id: str
    label: str
    args: dict


@dataclass
class ToolExecution:
    tool_id: str
    label: str
    args: dict
    output: str


class ToolRuntimeError(RuntimeError):
    """A registered tool failed before it could produce a safe result."""

    def __init__(self, tool_id: str, label: str, original: Exception):
        self.tool_id = tool_id
        self.label = label
        self.error_type = type(original).__name__
        super().__init__(f"{self.error_type}: {original}")


@dataclass
class RegisteredTool:
    tool_id: str
    label: str
    description: str
    matcher: Callable[[str], Optional[dict]]
    executor: Callable[[dict, dict], str]
    installed: Callable[[], bool]
    risk: str = "read"
    schema: dict | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: list[RegisteredTool] = []

    def register(self, tool: RegisteredTool) -> None:
        self._tools.append(tool)

    def list_tools(self) -> list[dict]:
        return [
            {
                "id": tool.tool_id,
                "label": tool.label,
                "description": tool.description,
                "installed": bool(tool.installed()),
                "risk": tool.risk,
                "schema": tool.schema or {},
            }
            for tool in self._tools
        ]

    def match(self, message: str) -> ToolInvocation | None:
        text = (message or "").strip()
        log.info(f"[tool_debug] match() attempting match for: {text}")
        for tool in self._tools:
            log.info(f"[tool_debug] testing {tool.tool_id}")
            args = tool.matcher(text)
            if args is not None:
                log.info(f"[tool_debug] MATCHED {tool.tool_id} with args {args}")
                return ToolInvocation(tool_id=tool.tool_id, label=tool.label, args=args)
        return None

    def dispatch(self, message: str, context: dict | None = None) -> ToolExecution | None:
        invocation = self.match(message)
        if not invocation:
            return None
        return self.execute(invocation, context)

    def execute(self, invocation: ToolInvocation, context: dict | None = None) -> ToolExecution:
        context = context or {}
        tool = self._tool_by_id(invocation.tool_id)
        try:
            output = tool.executor(invocation.args, context)
        except Exception as exc:
            raise ToolRuntimeError(tool.tool_id, tool.label, exc) from exc
        return ToolExecution(
            tool_id=invocation.tool_id,
            label=invocation.label,
            args=invocation.args,
            output=output,
        )

    def _tool_by_id(self, tool_id: str) -> RegisteredTool:
        for tool in self._tools:
            if tool.tool_id == tool_id:
                return tool
        raise KeyError(tool_id)


def _normalize_target(target: str) -> str:
    return (target or "").strip().lower().rstrip(".")


def _extract_target(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"//{value}", scheme="http")
    host = parsed.hostname or value
    return _normalize_target(host)


def _unsupported_command_help() -> str:
    return (
        "Unsupported command. Try one of: "
        "ping <host>, dig <host> [A|AAAA|MX|TXT|CNAME|NS|SOA], "
        "nslookup <host>, whois <host>, traceroute <host>, "
        "scan <host>, web scan <host>, ping sweep <cidr>, "
        "httpx <url>, whatweb <url>, nikto <url>, testssl <host>, "
        "zap <url>, ffuf <url-or-host>, "
        "web search <query>, scrape <url>, "
        "calendar <title> <today|tomorrow|YYYY-MM-DD|MM/DD/YYYY> at <time> "
        "notes: <optional notes>. "
        "Kali tools (via Draydev): "
        "kali nmap <host>, kali web <url>, kali whois <host>, kali dirb <url>, kali exec <cmd>."
    )


# ---------------------------------------------------------------------------
# Kali Docker tools — runs commands inside kali-custom:latest on Draydev
# Credentials loaded from config_local.py (gitignored)
# ---------------------------------------------------------------------------

def _run_kali_command(cmd: str, timeout: int = 90) -> str:
    """Run a shell command inside the Kali Docker container on Draydev via SSH."""
    if not CONFIG.get("kali_enabled", False):
        return "[kali] Kali integration is disabled. Set kali_enabled=True in config_local.py."
    host = CONFIG.get("kali_host", "192.168.0.200")
    user = CONFIG.get("kali_user", "draygen")
    password = CONFIG.get("kali_password", "")
    image = CONFIG.get("kali_image", "kali-custom:latest")
    if not password:
        return "[kali] kali_password not set in config_local.py."
    safe_cmd = cmd.replace("'", "'\"'\"'")
    ssh_cmd = [
        "sshpass", "-p", password,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "PreferredAuthentications=password",
        "-o", "PasswordAuthentication=yes",
        "-o", "PubkeyAuthentication=no",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}",
        f"docker exec kali bash -c '{safe_cmd}'",
    ]
    return _run_tool(ssh_cmd, timeout=timeout, max_chars=4000)


def run_kali_nmap(target: str, flags: str = "-sV --open") -> str:
    target = _extract_target(target)
    err = _ensure_authorized(target)
    if err:
        return err
    return _run_kali_command(f"nmap {flags} {target}")


def run_kali_nikto(target: str) -> str:
    url = _normalize_web_target(target)
    err = _ensure_authorized(_extract_target(target))
    if err:
        return err
    return _run_kali_command(f"nikto -h {url} -maxtime 60")


def run_kali_dirb(target: str) -> str:
    url = _normalize_web_target(target)
    err = _ensure_authorized(_extract_target(target))
    if err:
        return err
    return _run_kali_command(
        f"gobuster dir -u {url} -w /usr/share/wordlists/dirb/common.txt -q --no-progress -t 10"
    )


def run_kali_whois(target: str) -> str:
    target = _extract_target(target)
    err = _ensure_authorized(target)
    if err:
        return err
    return _run_kali_command(f"whois {target}")



def run_tavily_search(query: str) -> str:
    """Perform a Tavily OSINT search."""
    import requests
    api_key = CONFIG.get("tavily_api_key")
    if not api_key:
        return "[tavily] API key not configured."
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "search_depth": "advanced"},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for res in data.get("results", []):
            results.append(f"Title: {res['title']}\nURL: {res['url']}\nContent: {res['content'][:300]}...")
        return "\n\n".join(results) or "No results found."
    except Exception as e:
        return f"[tavily] Search failed: {str(e)}"


_FIRECRAWL_API_BASE = "https://api.firecrawl.dev/v2"


def _firecrawl_key() -> str:
    return (CONFIG.get("firecrawl_api_key") or "").strip()


def _firecrawl_post(path: str, payload: dict, timeout: int = 45) -> dict:
    """POST to the Firecrawl v2 API. Returns parsed JSON, raises on failure."""
    import requests

    key = _firecrawl_key()
    if not key:
        raise RuntimeError(
            "Firecrawl API key not configured "
            "(set firecrawl_api_key in config_local.py or the FIRECRAWL_API_KEY env var)."
        )
    resp = requests.post(
        f"{_FIRECRAWL_API_BASE}{path}",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def run_firecrawl_search(query: str, limit: int | None = None) -> str:
    """Search the web via Firecrawl; return titles, URLs, and snippets."""
    query = (query or "").strip()
    if not query:
        return "[firecrawl] Empty search query."
    limit = limit or int(CONFIG.get("firecrawl_search_limit", 5) or 5)
    try:
        data = _firecrawl_post("/search", {"query": query, "limit": limit})
    except Exception as e:
        return f"[firecrawl] Search failed: {e}"
    payload = data.get("data") or {}
    # v2 returns {"data": {"web": [...]}}; tolerate a bare list too.
    results = payload.get("web") if isinstance(payload, dict) else payload
    if not results:
        return "No results found."
    blocks = []
    for res in results[:limit]:
        title = res.get("title") or "(untitled)"
        url = res.get("url") or ""
        desc = (res.get("description") or res.get("snippet") or "").strip()
        block = f"Title: {title}\nURL: {url}"
        if desc:
            block += f"\n{desc[:300]}"
        blocks.append(block)
    return "\n\n".join(blocks)


def run_firecrawl_scrape(url: str) -> str:
    """Scrape a single URL via Firecrawl; return clean markdown."""
    url = (url or "").strip()
    if not url:
        return "[firecrawl] No URL provided."
    if "://" not in url:
        url = f"https://{url}"
    try:
        data = _firecrawl_post(
            "/scrape", {"url": url, "formats": ["markdown"], "onlyMainContent": True}
        )
    except Exception as e:
        return f"[firecrawl] Scrape failed: {e}"
    payload = data.get("data") or {}
    markdown = (payload.get("markdown") or "").strip()
    if not markdown:
        return "[firecrawl] No content extracted."
    max_chars = 6000
    if len(markdown) > max_chars:
        markdown = markdown[:max_chars] + "\n... (truncated)"
    return markdown


def _hermes_url(path: str) -> str:
    base = str(CONFIG.get("hermes_adapter_url", "http://127.0.0.1:8722")).rstrip("/")
    return f"{base}{path}"


# ── Natural-language "do something on my machine" detection ──────────────────
#
# Brian shouldn't have to type "delegate:". If he asks AION to actually DO
# something on his computer, that goes to the Hermes worker; if he asks a
# question, AION answers it. The split is: an action VERB aimed at a SYSTEM
# target, and NOT phrased as a how-to / explain question.

_ACTION_VERB = (
    r"check|show|list|find|get|display|run|execute|exec|launch|start|stop|restart|"
    r"kill|install|uninstall|update|upgrade|clean|clear|delete|remove|wipe|create|"
    r"make|mkdir|move|copy|rename|open|scan|monitor|configure|build|fix|mount|"
    r"unmount|free\s*up|set\s*up|look\s+at|pull\s+up"
)
_SYSTEM_TARGET = (
    r"disk|space|storage|drive|[a-z]:\\?|memory|ram|swap|cpu|load\s*average|"
    r"process(?:es)?|port|ports|service|services|daemon|file|files|folder|folders|"
    r"director(?:y|ies)|\bdir\b|path|network|interface|\bip\b|temp|cache|logs?|"
    r"package|uptime|kernel|mount|"
    r"my\s+(?:computer|machine|system|box|pc|laptop|desktop|drive|files)|"
    r"this\s+(?:machine|computer|box|pc|system)"
)
# Conceptual questions AION should answer itself — never delegate these.
_HOWTO_EXCLUDE = re.compile(
    r"(?i)^\s*(?:how\s+(?:do|to|can|would|should|does)|what\s+is|what's\s+a\b|"
    r"what\s+are|what's\s+the\s+(?:difference|command|best)|explain|why\b|describe|"
    r"tell\s+me\s+about|what\s+does|difference\s+between|when\s+should|should\s+i\b|"
    r"is\s+it\s+(?:safe|ok|possible)|can\s+you\s+explain)\b"
)
# Question forms that ARE a machine action ("what's using port 80", "how much
# space is left") — asking AION to go find out, not to explain a concept.
_INTERROGATIVE_ACTION = re.compile(
    r"(?i)(?:what'?s?\s+(?:using|running\s+on|eating|hogging|taking\s+up|listening\s+on)"
    r"|how\s+much\s+(?:disk\s+)?(?:space|ram|memory|storage)\b"
    r"|is\s+\S+\s+running\b)"
)


def looks_like_machine_action(text: str) -> bool:
    """Heuristic: is Brian asking AION to perform a local action (→ Hermes),
    rather than asking a question (→ AION answers)? Verb + system target, minus
    how-to/explain phrasing."""
    t = (text or "").strip()
    if not t or _HOWTO_EXCLUDE.search(t):
        return False
    low = t.lower()
    has_target = re.search(rf"(?i)(?:{_SYSTEM_TARGET})", low) is not None
    if not has_target:
        return False
    has_verb = re.search(rf"(?i)\b(?:{_ACTION_VERB})\b", low) is not None
    return has_verb or _INTERROGATIVE_ACTION.search(low) is not None


_WEB_SEARCH_RE = re.compile(
    r"(?i)^\s*(?:"
    r"(?:firecrawl|web\s*search|search\s+the\s+web(?:\s+for)?|search\s+online(?:\s+for)?|"
    r"search\s+the\s+internet(?:\s+for)?|google|look\s+up)\s+(.+?)"
    r"|(?:what'?s|what\s+is)\s+the\s+latest\s+(?:news\s+)?(?:on|about|with)\s+(.+?)"
    r"|(?:find|look\s+up)\s+(.+?)\s+(?:online|on\s+the\s+web|on\s+the\s+internet)"
    r")(?:\s+(?:online|on\s+the\s+web|on\s+the\s+internet))?\s*\??\s*$"
)


def detect_web_search(text: str) -> Optional[str]:
    """Natural web-search intent → the query. Explicit web phrasings only
    ("search the web for X", "google X", "what's the latest on X"), so it won't
    swallow "search my messages" (/msg) or plain chat. Returns None if not a
    web search."""
    m = _WEB_SEARCH_RE.match((text or "").strip())
    if not m:
        return None
    query = next((g for g in m.groups() if g), "").strip()
    return query or None


def build_hermes_objective(request: str) -> str:
    """Wrap Brian's request as a directive objective for the worker. Directive
    phrasing measurably improves the 8B worker's tool-call reliability
    (AION-INTEGRATION.md guidance #2), and the C:\\ → /mnt/c note keeps it from
    fumbling Windows paths inside its Linux sandbox."""
    return (
        "Use your terminal tools to ACTUALLY run the command(s) needed and report the "
        "real output. Do not guess or fabricate results. Note: Windows paths like C:\\ "
        "are mounted under /mnt/c in this Linux environment (C:\\ = /mnt/c, D:\\ = /mnt/d). "
        f"Task: {request.strip()}"
    )


def hermes_available() -> bool:
    """True if delegation is enabled and the adapter answers /health."""
    if not CONFIG.get("hermes_enabled", True):
        return False
    import requests
    try:
        r = requests.get(_hermes_url("/health"), timeout=2)
        return r.status_code < 500
    except requests.RequestException:
        return False


def run_hermes_delegate(objective: str, *, timeout: int | None = None) -> str:
    """Hand a long-running agentic task to the Hermes worker and block until it
    finishes. POSTs /tasks, polls /tasks/{id} to a terminal state, returns the
    worker's result (or a plain failure line). Task state lives in the adapter
    (in-memory); AION is the system of record, so we return the outcome inline.

    Delegation is for multi-step work with a concrete deliverable — building or
    editing files in the sandbox workspace, running a sequence of commands — not
    for questions AION should just answer itself. Expect ~10-15s startup.
    """
    import time
    import requests

    objective = (objective or "").strip()
    if not objective:
        return "[hermes] Nothing to delegate — give me an objective."
    if not CONFIG.get("hermes_enabled", True):
        return "[hermes] Delegation is disabled (hermes_enabled=False)."

    timeout = int(timeout or CONFIG.get("hermes_default_timeout", 600))
    body = {"objective": objective, "timeout": timeout}
    toolsets = [t.strip() for t in str(CONFIG.get("hermes_toolsets", "")).split(",") if t.strip()]
    if toolsets:
        body["allowed_tools"] = toolsets
    try:
        resp = requests.post(_hermes_url("/tasks"), json=body, timeout=10)
    except requests.RequestException as exc:
        return (f"[hermes] Worker adapter unreachable at {CONFIG.get('hermes_adapter_url')}. "
                f"Start it with `bash scripts/start-hermes.sh` in hermes-aion. ({exc})")
    if resp.status_code == 400:
        return "[hermes] Rejected: invalid workspace."
    if resp.status_code >= 400:
        return f"[hermes] Adapter error {resp.status_code}: {resp.text[:200]}"

    task_id = (resp.json() or {}).get("id")
    if not task_id:
        return "[hermes] Adapter accepted the task but returned no id."

    # Poll to a terminal state. Budget a little beyond the task's own timeout so
    # the adapter's SIGTERM/SIGKILL path resolves before we give up on it.
    deadline = time.monotonic() + timeout + 30
    task = {}
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            g = requests.get(_hermes_url(f"/tasks/{task_id}"), timeout=10)
        except requests.RequestException:
            continue
        if g.status_code != 200:
            continue
        task = g.json() or {}
        if task.get("status") in ("completed", "failed", "timeout", "cancelled"):
            break
    else:
        return f"[hermes] Task {task_id} still running after {timeout + 30}s; gave up waiting."

    status = task.get("status")
    if status == "completed":
        result = (task.get("result") or "").strip()
        return result or "[hermes] Completed with no textual result."
    if status == "timeout":
        return f"[hermes] Task timed out after {timeout}s."
    if status == "cancelled":
        return "[hermes] Task was cancelled."
    err = task.get("error") or {}
    msg = err.get("message") if isinstance(err, dict) else err
    return f"[hermes] Task failed: {msg or 'unknown error'}"


def run_maigret(username: str) -> str:
    """Run Maigret username OSINT across 3000+ social sites."""
    safe = username.strip().replace("'", "")
    return _run_kali_command(
        f"/usr/local/bin/maigret {safe} --no-color --timeout 30 2>&1 | head -100",
        timeout=120,
    )


def run_holehe(email: str) -> str:
    """Check which platforms an email address is registered on using Holehe."""
    safe = email.strip().replace("'", "")
    return _run_kali_command(
        f"/usr/local/bin/holehe {safe} --no-color 2>&1",
        timeout=60,
    )


def run_theharvester_person(name: str) -> str:
    """Run theHarvester to find emails and social profiles for a person name."""
    safe = name.strip().replace('"', "")
    # google/bing/brave removed in theHarvester 4.10.1; commoncrawl too slow for names
    return _run_kali_command(
        f'theHarvester -d "{safe}" -b duckduckgo -l 50 2>&1 | head -60',
        timeout=45,
    )


def _extract_name_and_location(query: str) -> tuple[str, str]:
    """Pull a person name (1–4 capitalized words) and optional location from a query string.

    By the time this is called, the matcher has already stripped the trigger phrase,
    so the query looks like: "Melinda Gavin, she resides in Massachusetts"

    Examples:
        "Melinda Gavin, she resides in Massachusetts"  → ("Melinda Gavin", "Massachusetts")
        "Brian Wallace from Lowell MA"                 → ("Brian Wallace", "Lowell MA")
        "John Smith who lives in New York"             → ("John Smith", "New York")
        "Jane Doe"                                     → ("Jane Doe", "")
    """
    # Isolate the name portion by splitting at the first contextual separator
    name_part = re.split(
        r"(?i)[,;]|\s+(?:who\b|she\b|he\b|they\b|lives?\s+in|resides?\s+in|located\s+in|based\s+in|\bfrom\b|\bin\b)",
        query,
        maxsplit=1,
    )[0].strip()

    # Extract 1–4 leading Title-Case words as the person's name
    name_words = re.findall(r"\b[A-Z][a-z'-]+\b", name_part)
    name = " ".join(name_words[:4]) if name_words else name_part.strip()

    # Search the full query for recognizable US state names or city+state abbreviations
    _US_STATES = (
        r"Massachusetts|New\s+Hampshire|New\s+York|New\s+Jersey|New\s+Mexico|"
        r"California|Texas|Florida|Connecticut|Rhode\s+Island|Vermont|Maine|"
        r"Pennsylvania|Ohio|Michigan|Illinois|Washington|Oregon|Colorado|Nevada|Arizona|"
        r"Georgia|Virginia|Maryland|North\s+Carolina|South\s+Carolina|Tennessee|"
        r"Minnesota|Wisconsin|Missouri|Indiana|Kentucky|Alabama|Mississippi|Louisiana|"
        r"Arkansas|Oklahoma|Kansas|Nebraska|Iowa|South\s+Dakota|North\s+Dakota|Montana|"
        r"Wyoming|Idaho|Utah|Alaska|Hawaii"
    )
    _STATE_ABBR = r"MA|NH|NY|NJ|NM|CA|TX|FL|CT|RI|VT|ME|PA|OH|MI|IL|WA|OR|CO|NV|AZ|GA|VA|MD|NC|SC|TN"
    loc_match = re.search(
        rf"(?i)(?:[A-Z][a-zA-Z]+,?\s+(?:{_STATE_ABBR})\b|(?:{_US_STATES})|\b(?:{_STATE_ABBR})\b)",
        query,
    )
    location = loc_match.group(0).strip() if loc_match else ""

    return name, location


def run_osint_investigate(query: str) -> str:
    """Intelligently route an OSINT investigation query to the right tools."""
    query = query.strip()
    parts = []

    # Email address → holehe
    if "@" in query and "." in query.split("@")[-1]:
        parts.append("=== Holehe — Email Platform Check ===")
        parts.append(run_holehe(query))
        parts.append("\n=== Tavily Web Search ===")
        parts.append(run_tavily_search(query))
        return "\n".join(parts)

    # Single-word username → maigret
    if re.match(r"^[a-zA-Z0-9_.+-]+$", query) and " " not in query and len(query) < 40:
        parts.append("=== Maigret — Username OSINT ===")
        parts.append(run_maigret(query))
        return "\n".join(parts)

    # Person name / general query → theHarvester + Tavily in parallel
    import concurrent.futures
    name, location = _extract_name_and_location(query)
    log.info(f"[osint] extracted name={name!r} location={location!r} from query={query!r}")

    # Build a focused Tavily query: name + location + social profiles
    loc_hint = f" {location}" if location else ""
    tavily_query = (
        f'"{name}"{loc_hint} '
        f'site:linkedin.com OR site:github.com OR site:twitter.com OR site:facebook.com OR site:whitepages.com'
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_harvester = ex.submit(run_theharvester_person, name)
        fut_tavily = ex.submit(run_tavily_search, tavily_query)
        harvester_out = fut_harvester.result()
        tavily_out = fut_tavily.result()

    parts.append("=== theHarvester — Email & Social Enumeration ===")
    parts.append(harvester_out)
    parts.append("\n=== Tavily — Web Intelligence ===")
    parts.append(tavily_out)
    return "\n".join(parts)


def run_kali_orchestrate(user_input: str) -> str:
    """Use LLM to plan a series of Kali commands and execute them."""
    from llm import ask_llm
    import json
    system_prompt = "You are Aion, a pentesting orchestrator. Given a user request, output ONLY a JSON array of Kali Linux commands to run. Use tools like: nmap, nikto, gobuster, whois, dig, whatweb, curl. Target must be authorized. Output ONLY valid JSON, no markdown, no explanation."
    prompt = f"{system_prompt}\n\nUser request: {user_input}"
    try:
        response = ask_llm(prompt)
        match = re.search(r"\[.*\]", response, re.DOTALL)
        if not match: return f"Failed to plan commands. LLM response: {response}"
        commands = json.loads(match.group(0))
        results = []
        for cmd in commands:
            # Check authorization for each command by extracting potential targets
            parts = cmd.split()
            authorized = True
            for p in parts:
                if "." in p or "://" in p:
                    if not is_authorized_target(p):
                        results.append(f"$ {cmd}\n[Error] Target {p} not authorized.")
                        authorized = False; break
            if authorized:
                output = _run_kali_command(cmd)
                results.append(f"$ {cmd}\n{output}")
        return "\n\n".join(results)
    except Exception as e:
        return f"Orchestration error: {str(e)}"

def run_kali_exec(cmd: str) -> str:
    """Run an arbitrary (pre-validated by caller) command in Kali."""
    return _run_kali_command(cmd)


def _authorized_patterns() -> list[str]:
    return [_normalize_target(v) for v in (CONFIG.get("authorized_network_targets") or []) if _normalize_target(v)]


def _is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _is_private_or_loopback(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def is_authorized_target(target: str) -> bool:
    raw = _normalize_target(target)
    if "/" in raw:
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            return False
        for pattern in _authorized_patterns():
            try:
                allowed = ipaddress.ip_network(pattern, strict=False)
            except ValueError:
                continue
            if network.version != allowed.version:
                continue
            if network.subnet_of(allowed) or network == allowed:
                return True
        return False

    normalized = _extract_target(target)
    if not normalized or not _HOST_RE.match(normalized):
        return False
    if _is_ip_address(normalized):
        if _is_private_or_loopback(normalized) or normalized in _authorized_patterns():
            return True
        for pattern in _authorized_patterns():
            if "/" in pattern:
                try:
                    address = ipaddress.ip_address(normalized)
                    allowed = ipaddress.ip_network(pattern, strict=False)
                    if address.version != allowed.version:
                        continue
                    if address in allowed:
                        return True
                except ValueError:
                    continue
        return False
    if normalized in {"localhost"}:
        return True
    patterns = _authorized_patterns()
    for pattern in patterns:
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if normalized.endswith(suffix):
                return True
        elif normalized == pattern:
            return True
    return False


def _run_tool(args: Iterable[str], timeout: int = 20, max_chars: int = 2500) -> str:
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return "Required tool is not installed on this host."
    except Exception as e:
        return f"Error: {e}"

    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    output = output.strip() or "(no output)"
    if len(output) > max_chars:
        output = output[:max_chars] + "\n... (truncated)"
    return output


def _tool_installed(name: str) -> bool:
    return shutil.which(name) is not None


def _first_installed(*names: str) -> str | None:
    for name in names:
        if _tool_installed(name):
            return name
    return None


def _normalize_web_target(target: str) -> str:
    value = (target or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    if "/" in value:
        return f"http://{value}"
    return f"http://{value}"


def _ensure_authorized(target: str) -> str | None:
    if not is_authorized_target(target):
        return "Target is not authorized. Add it to CONFIG['authorized_network_targets'] first."
    return None


def run_nslookup(target: str) -> str:
    target = _extract_target(target)
    err = _ensure_authorized(target)
    if err:
        return err
    return _run_tool(["nslookup", target], timeout=10)


def run_whois(target: str) -> str:
    target = _extract_target(target)
    err = _ensure_authorized(target)
    if err:
        return err
    return _run_tool(["whois", target], timeout=15)


def run_dig(target: str, record_type: str = "A") -> str:
    target = _extract_target(target)
    err = _ensure_authorized(target)
    if err:
        return err
    record_type = (record_type or "A").upper()
    if record_type not in {"A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA"}:
        return "Unsupported DNS record type."
    return _run_tool(["dig", target, record_type, "+short"], timeout=10)


def run_ping(target: str, count: int = 4) -> str:
    target = _extract_target(target)
    err = _ensure_authorized(target)
    if err:
        return err
    count = max(1, min(int(count), 4))
    return _run_tool(["ping", "-c", str(count), target], timeout=10)


def run_traceroute(target: str) -> str:
    target = _extract_target(target)
    err = _ensure_authorized(target)
    if err:
        return err
    return _run_tool(["traceroute", "-m", "12", target], timeout=30)


def run_nmap_ping_sweep(target: str) -> str:
    target = _extract_target(target) if "/" not in target else _normalize_target(target)
    err = _ensure_authorized(target)
    if err:
        return err
    return _run_tool(["nmap", "-sn", target], timeout=30)


def run_nmap_service_scan(target: str) -> str:
    target = _extract_target(target)
    err = _ensure_authorized(target)
    if err:
        return err
    return _run_tool(["nmap", "-Pn", "-sV", "--top-ports", "20", target], timeout=45)


def run_httpx(target: str) -> str:
    host = _extract_target(target)
    err = _ensure_authorized(host)
    if err:
        return err
    command = _first_installed("httpx")
    if not command:
        return "httpx is not installed on this host."
    return _run_tool([command, "-u", _normalize_web_target(target), "-follow-host-redirects", "-status-code", "-title", "-tech-detect"], timeout=30)


def run_whatweb(target: str) -> str:
    host = _extract_target(target)
    err = _ensure_authorized(host)
    if err:
        return err
    if not _tool_installed("whatweb"):
        return "whatweb is not installed on this host."
    return _run_tool(["whatweb", _normalize_web_target(target)], timeout=45)


def run_nikto(target: str) -> str:
    host = _extract_target(target)
    err = _ensure_authorized(host)
    if err:
        return err
    if not _tool_installed("nikto"):
        return "nikto is not installed on this host."
    return _run_tool(["nikto", "-ask", "no", "-host", _normalize_web_target(target)], timeout=90)


def run_testssl(target: str) -> str:
    host = _extract_target(target)
    err = _ensure_authorized(host)
    if err:
        return err
    command = _first_installed("testssl.sh", "/usr/local/bin/testssl.sh")
    if not command:
        return "testssl.sh is not installed on this host."
    return _run_tool([command, "--warnings", "batch", "--fast", host], timeout=120)


def run_zap_baseline(target: str) -> str:
    host = _extract_target(target)
    err = _ensure_authorized(host)
    if err:
        return err
    command = _first_installed("zap-baseline.py", "/usr/share/zaproxy/zap-baseline.py")
    if not command:
        return "OWASP ZAP baseline script is not installed on this host."
    return _run_tool([command, "-t", _normalize_web_target(target), "-m", "1", "-T", "5", "-I"], timeout=240, max_chars=4000)


def run_ffuf(target: str) -> str:
    host = _extract_target(target)
    err = _ensure_authorized(host)
    if err:
        return err
    if not _tool_installed("ffuf"):
        return "ffuf is not installed on this host."
    url = _normalize_web_target(target).rstrip("/") + "/FUZZ"
    return _run_tool(
        [
            "ffuf",
            "-w",
            _DEFAULT_FFUF_WORDLIST,
            "-u",
            url,
            "-mc",
            "all",
            "-fc",
            "404",
            "-t",
            "20",
            "-c",
        ],
        timeout=120,
        max_chars=4000,
    )


def _format_tool_output(label: str, target: str, output: str, suffix: str = "") -> str:
    heading = f"{label} for {target}"
    if suffix:
        heading += f" {suffix}"
    return f"{heading}:\n```\n{output}\n```"


def _exact_phrase_match(*phrases: str) -> Callable[[str], Optional[dict]]:
    normalized = {phrase.lower() for phrase in phrases}

    def matcher(text: str) -> Optional[dict]:
        if text.strip().lower() in normalized:
            return {}
        return None

    return matcher


def _regex_match(pattern: str, arg_names: tuple[str, ...]) -> Callable[[str], Optional[dict]]:
    compiled = re.compile(pattern, re.IGNORECASE)

    def matcher(text: str) -> Optional[dict]:
        match = compiled.match((text or "").strip())
        if not match:
            return None
        groups = match.groups()
        return {name: groups[index] for index, name in enumerate(arg_names)}

    return matcher


def _osint_investigate_matcher(text: str) -> Optional[dict]:
    # Primary patterns: explicit investigation verbs + subject
    m = re.search(
        r"(?i)(?:investigate|find\s+info(?:rmation)?\s+(?:on|about)|digital\s+footprint|background\s+check|"
        r"(?:run\s+)?osint\s+(?:on|for|search\s+(?:on|for))|who\s+is|research\s+on|"
        r"profile\s+(?:of|on|for)?|look\s*up)\s+(.+)",
        text,
    )
    if m:
        return {"query": m.group(1).strip()}
    # Secondary: "search <name> using osint" or "osint" as trailing modifier
    m2 = re.search(
        r"(?i)(?:search|find|look\s+up|lookup)\s+(.+?)\s+(?:using|with|via|through)\s+osint",
        text,
    )
    if m2:
        return {"query": m2.group(1).strip()}
    return None


def _firecrawl_search_matcher(text: str) -> Optional[dict]:
    # Explicit web-search triggers only, so this does not swallow bare "search"
    # (owned by tavily_search) or OSINT verbs ("who is", "look up").
    m = re.match(
        r"(?i)^(?:firecrawl|web\s*search|search\s+the\s+web(?:\s+for)?|search\s+online(?:\s+for)?)\s+(.+)$",
        (text or "").strip(),
    )
    if m:
        return {"query": m.group(1).strip()}
    return None


def _hermes_delegate_matcher(text: str) -> Optional[dict]:
    # Explicit delegation verbs only — this must not swallow ordinary requests
    # AION should answer itself. "have/ask hermes|the worker to <x>",
    # "delegate: <x>", "hermes: <x>", "worker: <x>".
    m = re.match(
        r"(?i)^(?:"
        r"(?:have|ask|tell|get)\s+(?:hermes|the\s+worker)\s+to\s+(.+)"
        r"|(?:delegate|hermes|worker)\s*[:\-]\s*(.+)"
        r"|delegate\s+to\s+hermes\s*[:\-]?\s*(.+)"
        r")$",
        (text or "").strip(),
    )
    if not m:
        return None
    objective = next((g for g in m.groups() if g), "").strip()
    return {"objective": objective} if objective else None


def _calendar_matcher(text: str) -> Optional[dict]:
    lowered = (text or "").lower()
    if re.search(
        r"\b(?:about|explain|describe|workflow|setup|requirements?|configured?|behind the scenes|"
        r"what (?:can|should|would)|how (?:do|does|would|should|can)|why)\b",
        lowered,
    ):
        return None
    if re.search(
        r"(?ix)"
        r"(?:"
        r"^\s*calendar\b|"
        r"\bgoogle\s+calendar|"
        r"\b(?:set|add|create|schedule|book)\b.{0,80}\b(?:appointment|event|reminder|calendar)\b|"
        r"\b(?:appointment|event|reminder)\b.{0,80}\b(?:today|tomorrow|at|on|for|"
        r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)|"
        r"\bremind\s+me\b.{0,120}\b(?:today|tomorrow|at|"
        r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r")",
        text or "",
    ):
        return {"message": text}
    return None


def _build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            tool_id="google_calendar",
            label="Google Calendar",
            description="Create appointments and reminders on the primary Google Calendar.",
            matcher=_calendar_matcher,
            executor=lambda args, context: handle_calendar_message(args["message"]),
            installed=lambda: bool(CONFIG.get("google_calendar_enabled", True)),
            risk="personal_write",
            schema={"message": "natural language event request"},
        )
    )
    # osint_investigate registered first — catches person/username/email investigations
    if CONFIG.get("kali_enabled", False):
        registry.register(
            RegisteredTool(
                tool_id="osint_investigate",
                label="OSINT Investigation",
                description="Investigate a person, username, or email using Maigret, Holehe, theHarvester, and Tavily.",
                matcher=_osint_investigate_matcher,
                executor=lambda args, context: run_osint_investigate(args["query"]),
                installed=lambda: bool(CONFIG.get("kali_enabled")),
            )
        )
    # Hermes worker delegation — explicit "delegate/hermes: <objective>" only.
    registry.register(
        RegisteredTool(
            tool_id="hermes_delegate",
            label="Hermes Worker",
            description=("Delegate a long-running, multi-step agentic task (build/edit files "
                         "in the sandbox, run a sequence of commands) to the Hermes worker."),
            matcher=_hermes_delegate_matcher,
            executor=lambda args, context: run_hermes_delegate(args["objective"]),
            installed=lambda: bool(CONFIG.get("hermes_enabled", True)),
            risk="agentic_write",
            schema={"objective": "the task to hand to the Hermes worker"},
        )
    )
    # Firecrawl web tools — registered before tavily so "web search"/"search the
    # web" route here; bare "search" still falls through to tavily_search.
    registry.register(
        RegisteredTool(
            tool_id="firecrawl_scrape",
            label="Firecrawl Scrape",
            description="Fetch a URL and return clean markdown via Firecrawl.",
            matcher=_regex_match(
                r"^(?:scrape|fetch(?:\s+page)?|grab(?:\s+page)?|read\s+(?:the\s+)?(?:page|url))\s+(\S+)$",
                ("url",),
            ),
            executor=lambda args, context: run_firecrawl_scrape(args["url"]),
            installed=lambda: bool(CONFIG.get("firecrawl_enabled", True) and _firecrawl_key()),
            schema={"url": "page URL to scrape"},
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="firecrawl_search",
            label="Firecrawl Search",
            description="Search the web via Firecrawl and return titles, URLs, and snippets.",
            matcher=_firecrawl_search_matcher,
            executor=lambda args, context: run_firecrawl_search(args["query"]),
            installed=lambda: bool(CONFIG.get("firecrawl_enabled", True) and _firecrawl_key()),
            schema={"query": "web search query"},
        )
    )
    # Tavily for explicit web/search queries (not osint — those go to osint_investigate)
    registry.register(
        RegisteredTool(
            tool_id="tavily_search",
            label="Tavily Search",
            description="Perform a deep web search using Tavily Pro.",
            matcher=lambda text: {"query": re.sub(r"(?i).*(?:search|tavily)\s+", "", text)} if re.search(r"(?i)(?:search|tavily)\s+", text) else None,
            executor=lambda args, context: run_tavily_search(args["query"]),
            installed=lambda: bool(CONFIG.get("tavily_api_key")),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="kali_orchestrate",
            label="Kali orchestrator",
            description="Orchestrate a series of Kali commands using natural language.",
            matcher=lambda text: {"user_input": re.sub(r"(?i).*(?:kali\s+orchestrate|pentest)\s+(?:a|an)?\s*", "", text)} if re.search(r"(?i)(?:kali\s+orchestrate|pentest)\s+", text) else None,
            executor=lambda args, context: run_kali_orchestrate(args["user_input"]),
            installed=lambda: bool(CONFIG.get("kali_enabled")),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="my_ip",
            label="Public IP",
            description="Report the requester IP seen by the server.",
            matcher=_exact_phrase_match("my ip", "my public ip", "what is my ip", "whats my ip"),
            executor=lambda args, context: f"Your public IP address is: {context.get('client_ip', 'unknown')}",
            installed=lambda: True,
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="nslookup",
            label="NSLookup",
            description="Run DNS lookup for an authorized host.",
            matcher=_regex_match(r"^(?:nslookup|dns lookup|lookup)\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("NSLookup", _extract_target(args["target"]), run_nslookup(args["target"])),
            installed=lambda: _tool_installed("nslookup"),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="whois",
            label="WHOIS",
            description="Run WHOIS on an authorized host.",
            matcher=_regex_match(r"^whois\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("WHOIS", _extract_target(args["target"]), run_whois(args["target"])),
            installed=lambda: _tool_installed("whois"),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="dig",
            label="DIG",
            description="Query DNS records for an authorized host.",
            matcher=_regex_match(r"^(?:dig|dns)\s+([^\s]+)(?:\s+([a-z]+))?$", ("target", "record_type")),
            executor=lambda args, context: _format_tool_output(
                "DIG",
                _extract_target(args["target"]),
                run_dig(args["target"], args.get("record_type") or "A"),
                f"({(args.get('record_type') or 'A').upper()})",
            ),
            installed=lambda: _tool_installed("dig"),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="ping",
            label="Ping",
            description="Ping an authorized host.",
            matcher=_regex_match(r"^ping\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("PING", _extract_target(args["target"]), run_ping(args["target"])),
            installed=lambda: _tool_installed("ping"),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="traceroute",
            label="Traceroute",
            description="Run traceroute to an authorized host.",
            matcher=_regex_match(r"^(?:traceroute|trace)\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("Traceroute", _extract_target(args["target"]), run_traceroute(args["target"])),
            installed=lambda: _tool_installed("traceroute"),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="nmap_service_scan",
            label="Nmap",
            description="Run a top-ports service scan against an authorized host.",
            matcher=_regex_match(r"^(?:(?:nmap|scan|web scan|http scan))\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output(
                "Nmap service scan",
                _extract_target(args["target"]),
                run_nmap_service_scan(_extract_target(args["target"])),
            ),
            installed=lambda: _tool_installed("nmap"),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="nmap_ping_sweep",
            label="Nmap Ping Sweep",
            description="Discover live hosts in an authorized CIDR.",
            matcher=_regex_match(r"^(?:ping sweep|discover hosts)\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("Nmap ping sweep", args["target"], run_nmap_ping_sweep(args["target"])),
            installed=lambda: _tool_installed("nmap"),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="httpx",
            label="httpx",
            description="Probe an authorized URL with httpx.",
            matcher=_regex_match(r"^httpx\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("httpx", _extract_target(args["target"]), run_httpx(args["target"])),
            installed=lambda: bool(_first_installed("httpx")),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="whatweb",
            label="WhatWeb",
            description="Fingerprint an authorized URL with WhatWeb.",
            matcher=_regex_match(r"^whatweb\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("WhatWeb", _extract_target(args["target"]), run_whatweb(args["target"])),
            installed=lambda: _tool_installed("whatweb"),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="nikto",
            label="Nikto",
            description="Run Nikto against an authorized URL.",
            matcher=_regex_match(r"^nikto\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("Nikto", _extract_target(args["target"]), run_nikto(args["target"])),
            installed=lambda: _tool_installed("nikto"),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="testssl",
            label="testssl.sh",
            description="Run TLS checks against an authorized host.",
            matcher=_regex_match(r"^testssl\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("testssl.sh", _extract_target(args["target"]), run_testssl(args["target"])),
            installed=lambda: bool(_first_installed("testssl.sh", "/usr/local/bin/testssl.sh")),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="zap_baseline",
            label="OWASP ZAP",
            description="Run the ZAP baseline scan against an authorized URL.",
            matcher=_regex_match(r"^(?:zap|zap baseline)\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("OWASP ZAP baseline", _extract_target(args["target"]), run_zap_baseline(args["target"])),
            installed=lambda: bool(_first_installed("zap-baseline.py", "/usr/share/zaproxy/zap-baseline.py")),
        )
    )
    registry.register(
        RegisteredTool(
            tool_id="ffuf",
            label="ffuf",
            description="Run ffuf against an authorized URL or host.",
            matcher=_regex_match(r"^ffuf\s+([^\s]+)$", ("target",)),
            executor=lambda args, context: _format_tool_output("ffuf", _extract_target(args["target"]), run_ffuf(args["target"])),
            installed=lambda: _tool_installed("ffuf"),
        )
    )
    # Kali Docker tools (only registered when kali_enabled is True in config_local.py)
    if CONFIG.get("kali_enabled", False):
        registry.register(
            RegisteredTool(
                tool_id="kali_nmap",
                label="Kali nmap",
                description="Run nmap inside Kali Docker on Draydev against an authorized target.",
                matcher=_regex_match(r"^kali\s+nmap\s+([^\s]+)(?:\s+(.+))?$", ("target", "flags")),
                executor=lambda args, context: _format_tool_output(
                    "Kali nmap", _extract_target(args["target"]),
                    run_kali_nmap(args["target"], args.get("flags") or "-sV --open")
                ),
                installed=lambda: bool(CONFIG.get("kali_enabled")),
            )
        )
        registry.register(
            RegisteredTool(
                tool_id="kali_web",
                label="Kali web scan",
                description="Run Nikto web vulnerability scan inside Kali Docker on Draydev.",
                matcher=_regex_match(r"^kali\s+(?:web|nikto)\s+([^\s]+)$", ("target",)),
                executor=lambda args, context: _format_tool_output(
                    "Kali nikto", _extract_target(args["target"]),
                    run_kali_nikto(args["target"])
                ),
                installed=lambda: bool(CONFIG.get("kali_enabled")),
            )
        )
        registry.register(
            RegisteredTool(
                tool_id="kali_dirb",
                label="Kali dirb",
                description="Run gobuster directory enumeration inside Kali Docker on Draydev.",
                matcher=_regex_match(r"^kali\s+dirb\s+([^\s]+)$", ("target",)),
                executor=lambda args, context: _format_tool_output(
                    "Kali dirb", _extract_target(args["target"]),
                    run_kali_dirb(args["target"])
                ),
                installed=lambda: bool(CONFIG.get("kali_enabled")),
            )
        )
        registry.register(
            RegisteredTool(
                tool_id="kali_whois",
                label="Kali whois",
                description="Run whois via Kali Docker on Draydev.",
                matcher=_regex_match(r"^kali\s+whois\s+([^\s]+)$", ("target",)),
                executor=lambda args, context: _format_tool_output(
                    "Kali whois", _extract_target(args["target"]),
                    run_kali_whois(args["target"])
                ),
                installed=lambda: bool(CONFIG.get("kali_enabled")),
            )
        )
        registry.register(
            RegisteredTool(
                tool_id="kali_orchestrate",
                label="Kali orchestrator",
                description="Orchestrate a series of Kali commands using natural language.",
                matcher=_regex_match(r"^(?:kali orchestrate|pentest|scan)\s+(.+)$", ("user_input",)),
                executor=lambda args, context: run_kali_orchestrate(args["user_input"]),
                installed=lambda: bool(CONFIG.get("kali_enabled")),
            )
        )
    return registry


def get_tool_registry() -> ToolRegistry:
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is None:
        _TOOL_REGISTRY = _build_tool_registry()
    return _TOOL_REGISTRY


def available_tool_status() -> list[dict]:
    return get_tool_registry().list_tools()


def dispatch_tool_message(message: str, client_ip: str) -> ToolExecution | None:
    log.info(f'[tool_debug] dispatch_tool_message reached for: {message}')
    if not CONFIG.get("network_ops_enabled", True):
        return None
    return get_tool_registry().dispatch(message, {"client_ip": client_ip})


def dispatch_safe_diagnostic_message(message: str, client_ip: str) -> ToolExecution | None:
    """Run only the small read-only diagnostic subset exposed to local clients.

    Matching happens before execution so broader registry tools (service scans,
    OSINT, web tests, calendar writes, or Kali commands) are never invoked by
    this entry point.
    """
    if not CONFIG.get("network_ops_enabled", True):
        return None
    registry = get_tool_registry()
    invocation = registry.match(message)
    if not invocation or invocation.tool_id not in SAFE_LOCAL_DIAGNOSTIC_TOOL_IDS:
        return None
    return registry.execute(invocation, {"client_ip": client_ip})


def handle_ops_command(message: str, client_ip: str, include_help: bool = True) -> str | None:
    execution = dispatch_tool_message(message, client_ip)
    if execution:
        return execution.output
    if include_help and CONFIG.get("network_ops_enabled", True):
        return _unsupported_command_help()
    return None
