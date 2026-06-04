#!/usr/bin/env python3
"""Seed a PipesHub instance with everything the Go SDK example apps expect.

The Go SDK examples (in the separate ``pipeshub-sdk-go`` repo) currently look up
resources by **hardcoded name** rather than CRUD/dynamic discovery:

- KB named ``SDK-test``                         (conversation + semantic_search / knowledgebase)
- connector named ``ABC News RSS``              (conversation + semantic_search / connector)
- an LLM provider                               (all conversation examples)
- an embedding model                            (semantic search)
- a web-search provider                         (conversation / web_search)

This script provisions all of the above against a freshly-bootstrapped instance
(org + admin user already created, onboarding skipped — as the CI workflow does),
waits for the KB + connector content to finish indexing, then writes the
``examples/.env`` file the SDK examples read.

It deliberately **reuses** the existing integration-test helpers:
- ``local_auth.obtain_local_oauth_credentials`` for OAuth client creds
- ``PipeshubClient`` for all API calls
- ``ai_models_setup.setup_test_llm_model`` / ``list_configured_llm_models`` for the LLM
- ``sample_data.ensure_sample_data_files_root`` for KB content

Env vars:
    PIPESHUB_BASE_URL            default http://localhost:3000
    PIPESHUB_TEST_USER_EMAIL     required (admin login for local OAuth client mint)
    PIPESHUB_TEST_USER_PASSWORD  required
    TEST_OPENAI_API_KEY          required for LLM (+ embedding fallback); falls back to OPENAI_API_KEY
    TEST_OPENAI_EMBEDDING_MODEL  default text-embedding-3-small
    SDK_RSS_FEED_URL             default ABC News top-stories feed
    SDK_EXAMPLES_ENV_PATH        where to write the examples .env (required in CI)
    SDK_KB_NAME                  default "SDK-test"
    SDK_CONNECTOR_NAME           default "ABC News RSS"
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

# Make the integration-tests helpers importable regardless of CWD.
_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parent  # integration-tests/
for p in (_ROOT, _ROOT / "helper", _ROOT / "sample-data"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ai_models_setup import (  # noqa: E402
    list_configured_llm_models,
    setup_test_llm_model,
)
from local_auth import obtain_local_oauth_credentials  # noqa: E402
from pipeshub_client import PipeshubClient  # noqa: E402
from sample_data import ensure_sample_data_files_root  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [seed-sdk] %(message)s",
)
log = logging.getLogger("seed-sdk")

DEFAULT_RSS_FEED = "https://abcnews.go.com/abcnews/topstories"
KB_NAME = os.getenv("SDK_KB_NAME", "SDK-test")
CONNECTOR_NAME = os.getenv("SDK_CONNECTOR_NAME", "ABC News RSS")


def _api_key() -> Optional[str]:
    return os.getenv("TEST_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")


def _ensure_oauth_credentials(base_url: str) -> None:
    """Mint local OAuth client_credentials if not already provided (mirrors conftest)."""
    if os.getenv("CLIENT_ID") and os.getenv("CLIENT_SECRET"):
        return
    log.info("Minting local OAuth client credentials via %s", base_url)
    client_id, client_secret = obtain_local_oauth_credentials(base_url)
    os.environ["CLIENT_ID"] = client_id
    os.environ["CLIENT_SECRET"] = client_secret


def seed_llm(client: PipeshubClient) -> None:
    existing = list_configured_llm_models(client)
    if existing:
        log.info("LLM already configured (%d model(s)); skipping", len(existing))
        return
    seeded = setup_test_llm_model(client)
    log.info("Seeded LLM: provider=%s model=%s", seeded.provider, seeded.model_name)


def seed_embedding(client: PipeshubClient) -> None:
    existing = client.list_ai_models("embedding")
    if existing:
        log.info("Embedding model already configured (%d); skipping", len(existing))
        return
    api_key = _api_key()
    if not api_key:
        log.warning(
            "No embedding model configured and no OpenAI API key; relying on the "
            "instance default embedding. Semantic search may fail if none exists."
        )
        return
    model = os.getenv("TEST_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    client.add_embedding_model(
        provider="openAI", model=model, api_key=api_key, is_default=True
    )
    log.info("Seeded embedding model: provider=openAI model=%s", model)


def seed_web_search(client: PipeshubClient) -> None:
    existing = client.list_web_search_providers()
    if existing:
        log.info("Web-search provider already configured (%d); skipping", len(existing))
        return
    client.add_web_search_provider(provider="duckduckgo", configuration={}, is_default=True)
    log.info("Seeded web-search provider: duckduckgo")


def seed_kb(client: PipeshubClient) -> str:
    kb_id = client.find_knowledge_base_id(KB_NAME)
    if kb_id:
        log.info("KB %r already exists (id=%s)", KB_NAME, kb_id)
    else:
        kb_id = client.create_knowledge_base(KB_NAME)
        log.info("Created KB %r (id=%s)", KB_NAME, kb_id)

    # Already has indexed content? Don't re-upload on reruns.
    already = client.list_records(kb_id=kb_id, indexing_status="COMPLETED")
    if already:
        log.info("KB %r already has %d COMPLETED record(s)", KB_NAME, len(already))
        return kb_id

    files = _pick_sample_files(limit=3)
    log.info("Uploading %d file(s) into KB %r: %s", len(files), KB_NAME, [f.name for f in files])
    records = client.upload_files_to_kb(kb_id, files)
    log.info("Upload accepted %d record(s); waiting for indexing", len(records))
    client.wait_for_completed_records(
        kb_id=kb_id, expected_min=1, timeout=int(os.getenv("SDK_KB_INDEX_TIMEOUT", "420"))
    )
    return kb_id


def _pick_sample_files(limit: int) -> List[Path]:
    root = ensure_sample_data_files_root()
    files = [p for p in sorted(root.rglob("*")) if p.is_file()][:limit]
    if not files:
        raise RuntimeError(f"No sample files found under {root}")
    return files


def seed_rss_connector(client: PipeshubClient) -> str:
    feed_url = os.getenv("SDK_RSS_FEED_URL", DEFAULT_RSS_FEED)

    existing = client.list_connectors(search=CONNECTOR_NAME)
    connector_id = None
    for c in (existing.get("connectors") or existing.get("data") or []) if isinstance(existing, dict) else []:
        if isinstance(c, dict) and c.get("instanceName") == CONNECTOR_NAME:
            connector_id = c.get("connectorId") or c.get("_key")
            break

    if connector_id:
        log.info("Connector %r already exists (id=%s)", CONNECTOR_NAME, connector_id)
    else:
        instance = client.create_connector(
            connector_type="RSS",
            instance_name=CONNECTOR_NAME,
            scope="personal",
            config={
                "sync": {
                    "feed_urls": feed_url,
                    "max_articles_per_feed": int(os.getenv("SDK_RSS_MAX_ARTICLES", "10")),
                    "fetch_full_content": False,
                }
            },
            auth_type="NONE",
        )
        connector_id = instance.connector_id
        log.info("Created RSS connector %r (id=%s) feed=%s", CONNECTOR_NAME, connector_id, feed_url)

    already = client.list_records(connector_ids=[connector_id], indexing_status="COMPLETED")
    if already:
        log.info("Connector %r already has %d COMPLETED record(s)", CONNECTOR_NAME, len(already))
        return connector_id

    client.toggle_sync(connector_id, enable=True)
    log.info("Sync enabled for connector %s; waiting for indexing", connector_id)
    client.wait_for_completed_records(
        connector_id=connector_id,
        expected_min=1,
        timeout=int(os.getenv("SDK_RSS_INDEX_TIMEOUT", "600")),
    )
    return connector_id


def write_env_file(path: Path, base_url: str, kb_id: str, connector_id: str) -> None:
    email = os.getenv("PIPESHUB_TEST_USER_EMAIL", "")
    password = os.getenv("PIPESHUB_TEST_USER_PASSWORD", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"PIPESHUB_BASE_URL={base_url}",
                f"PIPESHUB_TEST_USER_EMAIL={email}",
                f"PIPESHUB_TEST_USER_PASSWORD={password}",
                f"KB_ID={kb_id}",
                f"CONNECTOR_ID={connector_id}",
                "",
            ]
        )
    )
    log.info("Wrote examples env file: %s", path)


def main() -> int:
    base_url = (os.getenv("PIPESHUB_BASE_URL") or "http://localhost:3000").rstrip("/")
    if not (os.getenv("PIPESHUB_TEST_USER_EMAIL") and os.getenv("PIPESHUB_TEST_USER_PASSWORD")):
        log.error("PIPESHUB_TEST_USER_EMAIL and PIPESHUB_TEST_USER_PASSWORD are required")
        return 2

    _ensure_oauth_credentials(base_url)
    client = PipeshubClient(base_url=base_url)

    seed_llm(client)
    seed_embedding(client)
    seed_web_search(client)
    kb_id = seed_kb(client)
    connector_id = seed_rss_connector(client)

    env_path = Path(
        os.getenv("SDK_EXAMPLES_ENV_PATH")
        or (_ROOT.parent.parent / "pipeshub-sdk-go" / "examples" / ".env")
    )
    write_env_file(env_path, base_url, kb_id, connector_id)
    log.info("✅ Seeding complete (KB=%s connector=%s)", kb_id, connector_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
