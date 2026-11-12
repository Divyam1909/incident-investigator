**Provider:** gemini / `gemini-3.1-flash-lite`. **Accuracy: 4/4**

| Scenario | Ground truth | Agent's root cause | Correct | Confidence | Tool calls | Tokens in / out | Latency | Cost (USD) | Claims removed by verify |
|---|---|---|---|---|---|---|---|---|---|
| bad_deploy | bad_deploy / checkout-api | bad_deploy / checkout-api@2.14.0 | yes | high | 3 | 9,301 / 984 | 8.8s | 0.0053 | 0 |
| db_pool | db_pool_exhaustion / postgres | db_pool_exhaustion / postgres | yes | high | 5 | 15,456 / 1,048 | 18.5s | 0.0073 | 0 |
| llm_timeout | dependency_timeout / gemini | dependency_timeout / gemini | yes | high | 4 | 11,738 / 980 | 34.9s | 0.0060 | 0 |
| cache_red_herring | cache_outage / redis | cache_outage / redis | yes | high | 4 | 12,676 / 1,178 | 36.9s | 0.0067 | 0 |
