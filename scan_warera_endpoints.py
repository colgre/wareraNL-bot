"""Find tRPC procedures WarEra's frontend calls that our local openapi.json is missing.

WarEra's own published OpenAPI spec (https://api2.warera.io/openapi.json) is
stale and not kept up to date by the developer — diffing against it misses
most real endpoints (confirmed: it doesn't even list the four work.getStats*
endpoints we already know about). So instead this reverse-engineers the
actual frontend at https://app.warera.io/:

  1. Fetch the homepage to find the current Next.js build's chunk manifest.
  2. Download every JS chunk referenced by any page.
  3. Regex out tRPC call sites: `<router>.<procedure>.useQuery(`,
     `.useMutation(`, `.useSuspenseQuery(`, `.useInfiniteQuery(`, etc. tRPC's
     client is a Proxy, so these router/procedure names survive minification
     as literal property accesses in the bundled JS even though the full
     dotted path is never a string literal anywhere.
  4. Diff the discovered procedures against openapi.json.

Only fetches static JS assets from WarEra's CDN — never calls their tRPC API,
so this is safe to run as often as you like without looking like API abuse.

Queries and mutations are reported separately: queries are read-only and
reasonable to add to /api-explorer's live-test page; mutations are
side-effecting (they'd actually DO things like open a case or spend gems) and
should essentially never be added there — reported for awareness only.

A lot of what turns up is WarEra's own internal admin/moderation surface
(adminLog, banword, blacklistedIp, userAdminRights, ...) which our API key
has no access to. This script does not try to filter that out — it's a
judgment call for whoever reviews the report, not something to guess at
programmatically.

Run monthly:

    python scan_warera_endpoints.py

Pass --verify to also live-check each new query candidate against WarEra's
API using our own keys (the master key pool, falling back to the dedicated
premium pool for anything that comes back "Premium required") and sort
results into allowed / premium-only / blocked / not-a-real-endpoint. Uses
the master pool rather than any one service's dedicated key slice, spread
across keys with a delay between calls, so it doesn't eat into the rate
limit budget any live service (website, discord bot) depends on:

    python scan_warera_endpoints.py --verify
"""

from __future__ import annotations

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

APP_BASE_URL = "https://app.warera.io"
API_BASE_URL = "https://api2.warera.io/trpc"
LOCAL_SPEC_PATH = Path(__file__).resolve().parent / "openapi.json"
# Probing uses the master key pool (all keys, shared across services) rather
# than any one service's dedicated slice — spreads the burst of verify calls
# across more keys so it doesn't rate-limit the live website's own 2-key pool
# (which happened when this first used _api_keys_website.json directly).
PROBE_KEYS_PATH = Path(__file__).resolve().parent / "_api_keys.json"
PREMIUM_KEYS_PATH = Path(__file__).resolve().parent / "_api_keys_discord.json"

_BUILD_MANIFEST_RE = re.compile(r'src="(/_next/static/[^"]+/_buildManifest\.js)"')
_CHUNK_PATH_RE = re.compile(r"static/chunks/[A-Za-z0-9_./\[\]-]+\.js")

_IDENT = r"[a-zA-Z_$][a-zA-Z0-9_$]*"
_QUERY_HOOKS = (
    "useQuery", "useSuspenseQuery", "useInfiniteQuery", "useSuspenseInfiniteQuery",
)
_QUERY_CALL_RE = re.compile(
    rf"({_IDENT}\.{_IDENT})\.(?:{'|'.join(_QUERY_HOOKS)})\("
)
_MUTATION_CALL_RE = re.compile(rf"({_IDENT}\.{_IDENT})\.useMutation\(")


def _find_chunk_urls() -> list[str]:
    resp = requests.get(APP_BASE_URL + "/", timeout=15)
    resp.raise_for_status()
    m = _BUILD_MANIFEST_RE.search(resp.text)
    if not m:
        raise RuntimeError(
            "Couldn't find _buildManifest.js reference on the WarEra homepage — "
            "the site's build setup may have changed; this script needs updating."
        )
    manifest_resp = requests.get(APP_BASE_URL + m.group(1), timeout=15)
    manifest_resp.raise_for_status()
    # The build manifest only lists per-page chunks. The shared chunks
    # (pages/_app, main, ...) are referenced from the homepage HTML instead and
    # hold many procedures (league.getMyLeague, map.getMapData, ...) — scan both.
    chunk_paths = sorted(
        set(_CHUNK_PATH_RE.findall(manifest_resp.text)) | set(_CHUNK_PATH_RE.findall(resp.text))
    )
    return [f"{APP_BASE_URL}/_next/{path}" for path in chunk_paths]


def _download_chunks(urls: list[str]) -> tuple[list[str], int]:
    texts: list[str] = []
    failures = 0

    def fetch(url: str) -> str | None:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(fetch, url) for url in urls]
        for fut in as_completed(futures):
            text = fut.result()
            if text is None:
                failures += 1
            else:
                texts.append(text)

    return texts, failures


def _extract_procedures(chunk_texts: list[str]) -> tuple[set[str], set[str]]:
    queries: set[str] = set()
    mutations: set[str] = set()
    for text in chunk_texts:
        queries.update(_QUERY_CALL_RE.findall(text))
        mutations.update(_MUTATION_CALL_RE.findall(text))
    return queries, mutations


def load_local_procedures() -> set[str]:
    spec = json.loads(LOCAL_SPEC_PATH.read_text())
    return {p.lstrip("/") for p in spec.get("paths", {})}


def _load_keys(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if isinstance(data, list):
        return [str(k) for k in data]
    if isinstance(data, dict):
        return [str(k) for k in data.get("keys", [])]
    return []


def _probe(procedure: str, key: str) -> tuple[int, str]:
    """One tRPC GET with an empty input body — returns (status, error message or '').

    Retries on 429 with increasing backoff (respecting Retry-After when the
    server sends one) rather than giving up after one retry — a rate limit
    that hasn't cleared must never be silently treated as any other status.
    """
    url = f"{API_BASE_URL}/{procedure}"
    resp = None
    for attempt, backoff in enumerate((0.0, 3.0, 8.0, 15.0)):
        if backoff:
            time.sleep(backoff)
        try:
            resp = requests.get(url, params={"input": "{}"}, headers={"x-api-key": key}, timeout=10)
        except requests.RequestException as exc:
            return -1, str(exc)
        if resp.status_code != 429:
            break
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 20.0))
            except ValueError:
                pass
    try:
        msg = resp.json().get("error", {}).get("message", "")
    except Exception:
        msg = resp.text[:200]
    return resp.status_code, msg


def _classify(status: int, msg: str) -> str:
    if status == 404:
        return "not_found"          # not a real tRPC procedure (bundle-regex false positive)
    if status == 401:
        return "blocked"
    if status == 403:
        return "premium" if "premium" in msg.lower() else "blocked"
    if status == 429:
        return "rate_limited"       # never guess — surfaced separately so it gets re-checked, not silently counted
    return "allowed"                # 200 / 400 (bad input, but reachable) / 500 (bad param type) all mean: reachable


def verify_candidates(procedures: list[str]) -> dict[str, list[str]]:
    probe_keys = _load_keys(PROBE_KEYS_PATH)
    premium_keys = _load_keys(PREMIUM_KEYS_PATH)
    if not probe_keys:
        print(f"  (skipping --verify: no key file found at {PROBE_KEYS_PATH})")
        return {}

    results: dict[str, list[str]] = {
        "allowed": [], "premium": [], "blocked": [], "not_found": [], "rate_limited": [],
    }

    # Fully sequential, one key per call, deliberately slow: a monthly manual
    # scan doesn't need to be fast, and a burst against a small shared key
    # pool is exactly what produced flaky, self-contradicting results during
    # development (transient throttling misread as "blocked"). "blocked" and
    # "not_found" verdicts get one confirmation probe on a *different* key
    # before being trusted, since those are the verdicts that would cause a
    # real, usable endpoint to be silently dropped from the report.
    for i, proc in enumerate(procedures):
        print(f"  [{i + 1}/{len(procedures)}] {proc}", end="\r")
        status, msg = _probe(proc, probe_keys[i % len(probe_keys)])
        cat = _classify(status, msg)

        if cat in ("blocked", "not_found") and len(probe_keys) > 1:
            time.sleep(1.0)
            status2, msg2 = _probe(proc, probe_keys[(i + 1) % len(probe_keys)])
            cat2 = _classify(status2, msg2)
            cat = cat if cat2 == cat else "rate_limited"

        if cat == "premium" and premium_keys:
            pstatus, pmsg = _probe(proc, premium_keys[i % len(premium_keys)])
            pcat = _classify(pstatus, pmsg)
            cat = "premium" if pcat == "allowed" else pcat

        results[cat].append(proc)
        time.sleep(0.4)
    print(" " * 80, end="\r")

    for cat in results:
        results[cat].sort()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dump", metavar="PATH",
        help="Write the full set of discovered procedures (queries + mutations) as JSON to PATH.",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Live-check each new query candidate against WarEra's API (see module docstring).",
    )
    args = parser.parse_args()

    print("Fetching WarEra's current frontend build manifest...")
    chunk_urls = _find_chunk_urls()
    print(f"Found {len(chunk_urls)} JS chunks; downloading...")

    chunk_texts, failures = _download_chunks(chunk_urls)
    if failures:
        print(f"  (warning: {failures} chunk(s) failed to download, continuing without them)")

    queries, mutations = _extract_procedures(chunk_texts)
    print(f"Discovered {len(queries)} query-type and {len(mutations)} mutation-type procedure calls.\n")

    if args.dump:
        Path(args.dump).write_text(json.dumps({
            "queries": sorted(queries),
            "mutations": sorted(mutations),
        }, indent=2))
        print(f"Wrote discovered procedures to {args.dump}\n")

    ours = load_local_procedures()
    new_queries = sorted(queries - ours)
    new_mutations = sorted(mutations - ours)

    if new_queries:
        print(f"=== {len(new_queries)} new QUERY-type procedure(s) not in openapi.json (read-only — candidates to add) ===\n")
        if args.verify:
            print(f"Live-checking accessibility of {len(new_queries)} candidates against WarEra's API...\n")
            results = verify_candidates(new_queries)
            if results:
                if results["allowed"]:
                    print(f"--- {len(results['allowed'])} ALLOWED with our normal (non-premium) keys — add these ---\n")
                    for p in results["allowed"]:
                        print(f"  {p}")
                    print()
                if results["premium"]:
                    print(f"--- {len(results['premium'])} PREMIUM required — usable via the premium key pool, like work.getStats* ---\n")
                    for p in results["premium"]:
                        print(f"  {p}")
                    print()
                if results["blocked"]:
                    print(f"--- {len(results['blocked'])} BLOCKED for API tokens entirely (same class as user.getMe) — do not add ---\n")
                    for p in results["blocked"]:
                        print(f"  {p}")
                    print()
                if results["not_found"]:
                    print(f"--- {len(results['not_found'])} not real tRPC procedures (bundle-regex false positives) — ignore ---\n")
                    for p in results["not_found"]:
                        print(f"  {p}")
                    print()
                if results["rate_limited"]:
                    print(f"--- {len(results['rate_limited'])} INCONCLUSIVE — still rate-limited after retries, re-run --verify later ---\n")
                    for p in results["rate_limited"]:
                        print(f"  {p}")
                    print()
        else:
            for p in new_queries:
                print(f"  {p}")
            print(
                "\nThese are read-only (useQuery/useSuspenseQuery/useInfiniteQuery) so they're safe to "
                "live-test. Many will be WarEra's own internal admin/moderation calls our key can't use — "
                "re-run with --verify to check each one live, or check by hand with curl (403 'Premium "
                "required' / FORBIDDEN vs a real 200) before adding it to openapi.json.\n"
            )
    else:
        print("No new query-type procedures found.\n")

    if new_mutations:
        print(f"=== {len(new_mutations)} new MUTATION-type procedure(s) not in openapi.json (side-effecting — awareness only) ===\n")
        for p in new_mutations:
            print(f"  {p}")
        print(
            "\nThese DO things (spend gems, open cases, apply for jobs, ...). Do not add them to "
            "the live-test explorer as testable buttons.\n"
        )
    else:
        print("No new mutation-type procedures found.\n")


if __name__ == "__main__":
    main()
