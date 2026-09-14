from __future__ import annotations

import asyncio
import logging
import socket

import httpx

from pipeline.models import ClassifiedCompany

logger = logging.getLogger(__name__)

VERIFY_CONCURRENCY = 20
DNS_TIMEOUT = 3.0
HTTP_TIMEOUT = 5.0


def _dns_resolves(domain: str) -> bool:
    try:
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        return False


async def _verify_one(client: httpx.AsyncClient, company: ClassifiedCompany, sem: asyncio.Semaphore) -> None:
    domain = company.website.strip()
    if not domain:
        return

    async with sem:
        loop = asyncio.get_running_loop()
        try:
            resolves = await asyncio.wait_for(
                loop.run_in_executor(None, _dns_resolves, domain), timeout=DNS_TIMEOUT + 1
            )
        except asyncio.TimeoutError:
            resolves = False

        if not resolves:
            logger.warning(
                "Domain %r for %r does not resolve (DNS NXDOMAIN or similar) -- "
                "likely a fabricated or mistyped website, clearing it",
                domain, company.company_name,
            )
            company.website = ""
            company.reason = (
                f"{company.reason} [Website domain '{domain}' failed DNS resolution during "
                f"verification and was removed -- the company itself was not re-checked.]"
            ).strip()
            return

        # DNS resolving isn't proof of a live site (parked domains resolve
        # too) -- a lightweight HTTP check catches those, but network
        # failures here are treated as inconclusive, not a rejection, since
        # many real corporate sites block bare HEAD requests or automated
        # user agents without actually being fake.
        try:
            resp = await client.head(
                f"https://{domain}", timeout=HTTP_TIMEOUT, follow_redirects=True
            )
            if resp.status_code >= 500:
                logger.info(
                    "Domain %r for %r resolved but returned server error %d -- keeping as-is "
                    "(inconclusive, not necessarily fake)",
                    domain, company.company_name, resp.status_code,
                )
        except (httpx.HTTPError, OSError):
            # DNS resolved but HTTP failed (timeout, TLS error, connection
            # refused) -- inconclusive on its own (some real sites are
            # slow, geo-blocked, or reject automated clients), so the
            # domain is kept rather than cleared, unlike the DNS-fail case.
            pass


async def verify_domains(companies: list[ClassifiedCompany]) -> None:
    """Check that every claimed website domain actually resolves via DNS
    before export, clearing it (and noting why) when it doesn't. This is a
    lightweight, no-API-cost check against a real, independent signal --
    unlike the AI Mode discovery step's own claim of confidence, DNS
    resolution can't be talked around. Confirmed necessary after a spot-check
    of a real run found domains like 'bremnerseals.com' and
    'spartanseals.com' that return NXDOMAIN, i.e. fabricated alongside a
    plausible-sounding company name. Mutates companies in place. Does NOT
    remove companies from the list -- an unverifiable website is a data
    quality flag on that field, not proof the company itself is fake, so
    the company stays in the export with an empty website rather than being
    silently dropped."""
    candidates = [c for c in companies if c.website]
    if not candidates:
        return

    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_verify_one(client, c, sem) for c in candidates))

    cleared = sum(1 for c in candidates if not c.website)
    if cleared:
        logger.info(
            "Domain verification cleared %d of %d claimed websites that failed DNS resolution",
            cleared, len(candidates),
        )
