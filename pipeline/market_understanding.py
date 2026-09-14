from __future__ import annotations

import logging

from pipeline.deepseek_client import DeepSeekClient
from pipeline.models import MarketBoundary, MarketUnderstanding
from prompts.market_understanding import build_messages

logger = logging.getLogger(__name__)

# The prompt asks DeepSeek for "40 to 60" queries, but that's a soft target --
# a detailed brief with a large segmentation matrix (e.g. product type x size x
# flavor x channel) can push the model to enumerate combinations instead of
# picking representative queries, blowing well past 60 (observed: 467 for the
# food thin wafers brief). A hard cap here prevents that from turning into
# hours of unnecessary discovery traffic regardless of what the model returns.
MAX_SEARCH_QUERIES = 70


def understand_market(
    client: DeepSeekClient, market_name: str, geography: str, category_prompt: str, brief: str = ""
) -> MarketUnderstanding:
    system, user = build_messages(market_name, geography, category_prompt, brief)
    data = client.chat_json(system, user)

    queries = [q for q in data.get("search_queries", []) if isinstance(q, str) and q.strip()]
    deduped_queries = list(dict.fromkeys(queries))
    if len(deduped_queries) > MAX_SEARCH_QUERIES:
        logger.warning(
            "DeepSeek returned %d search queries, capping to %d to keep discovery time bounded",
            len(deduped_queries), MAX_SEARCH_QUERIES,
        )
    capped_queries = deduped_queries[:MAX_SEARCH_QUERIES]

    return MarketUnderstanding(
        market_name=market_name,
        geography=geography,
        category_prompt=category_prompt,
        definition=data.get("definition", ""),
        value_chain=data.get("value_chain", []),
        ecosystem_functions=data.get("ecosystem_functions", []),
        boundary=MarketBoundary(
            in_scope=data.get("in_scope", []),
            out_of_scope=data.get("out_of_scope", []),
        ),
        search_queries=capped_queries,
        brief=brief,
    )
