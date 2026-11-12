**Provider:** mock / `rule-based-sre-v1`. **Accuracy: 4/4**

| Scenario | Ground truth | Agent's root cause | Correct | Confidence | Tool calls | Tokens in / out | Latency | Cost (USD) | Claims removed by verify |
|---|---|---|---|---|---|---|---|---|---|
| bad_deploy | bad_deploy / checkout-api | bad_deploy / checkout-api@2.14.0 | yes | high | 5 | 7,043 / 695 | 0.2s | 0.0000 | 0 |
| db_pool | db_pool_exhaustion / postgres | db_pool_exhaustion / postgres | yes | high | 5 | 7,952 / 695 | 0.2s | 0.0000 | 0 |
| llm_timeout | dependency_timeout / gemini | dependency_timeout / gemini | yes | high | 5 | 6,974 / 704 | 0.1s | 0.0000 | 0 |
| cache_red_herring | cache_outage / redis | cache_outage / redis | yes | high | 5 | 7,230 / 764 | 0.2s | 0.0000 | 0 |
