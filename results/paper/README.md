# Results reported in the paper

Per-test results reported in the paper's Supplementary Section "Platform Benchmark Against RADAR-base", from two campaigns with the same tests, repetitions, and harness:

- RADAR-base: the 16 RADAR-base jobs of `../campaigns/20261020_093630_paper_round4.tsv` (20-21 October 2026, seed 20261020).
- MEDWEAR: the 19 jobs of `../campaigns/20261023_192526_medwear_sessions_round5.tsv` (23-24 October 2026, seed 20261023), MEDWEAR commit `49be39f` (two-phase ingest, stream metadata registered once per session, `MEDWEAR_SESSIONS=1`). The MEDWEAR jobs of the first campaign (commit `a8a666b`) are not used in these tables.

The CSV files were produced from the harness output of these jobs with

```
python3 bench/analysis/paper_tables.py --results results \
    --manifest results/campaigns/20261020_093630_paper_round4.tsv:radar \
    --manifest results/campaigns/20261023_192526_medwear_sessions_round5.tsv:medwear \
    --out results/paper
```

The benchmark sends synthetic ECG batches under generated identifiers only; no participant or health data were used. The raw run folders are not part of this repository.

| File | Content |
|---|---|
| `latency.csv` | per platform, hop, and batch size: median over jobs of the per-job p50 and p95, with their range |
| `validation.csv` | per malformed request and platform: HTTP status, whether anything was stored, and how many jobs agree |
| `load.csv` | per condition and level (MEDWEAR conditions `medwear_sessions_*`): share of requests acknowledged, acknowledged requests/s, share of HTTP 5xx, median latency of acknowledged requests (pre-registered ramp cut) |

## Latency by batch size

30 sequential requests per batch size and job, three jobs per platform. MEDWEAR through its nginx proxy (HTTPS), RADAR-base at its gateway (HTTP). Medians over jobs of the per-job percentiles.

| Samples/batch | MEDWEAR p50 (ms) | MEDWEAR p95 (ms) | RADAR-base p50 (ms) | RADAR-base p95 (ms) |
|---:|---:|---:|---:|---:|
| 100 | 32.0 | 34.5 | 24.6 | 27.3 |
| 500 | 38.6 | 40.5 | 56.8 | 62.3 |
| 1,000 | 44.0 | 48.0 | 97.8 | 101.2 |
| 5,000 | 95.1 | 138.9 | 438.2 | 443.6 |
| 10,000 | 154.8 | 203.4 | 865.7 | 869.2 |
| 50,000 | 745.3 | 770.1 | 4,284.9 | 4,303.6 |

All 1,620 timed requests succeeded. Requests sent directly to the MEDWEAR API, bypassing nginx and HTTPS (`hop=api_direct` in `latency.csv`), were at most 2.0 ms faster (medians).

## Responses to malformed requests

A valid control (V00) and 15 malformed requests, three jobs per platform. The responses were identical in all three jobs.

| | Defect | MEDWEAR | RADAR-base |
|---|---|---|---|
| V01 | No token | 401 | 401 |
| V02 | Invalid token | 401 | 401 |
| V03 | Wrong content type | 200, **stored** | 415 |
| V04 | Malformed JSON | 400 | 400 |
| V05 | Empty body | 400 | 400 |
| V06 | Unknown schema | 200, **stored** | 422 |
| V07 | Missing timestamp | 400 | 500 |
| V08 | Missing sampling rate | 200, **stored** | n/a |
| V09 | Non-numeric sample | 400 | 500 |
| V10 | Missing subject | 400 | 500 |
| V11 | Missing project | 200, **stored**¹ | 422 |
| V12 | Unknown project | 200, **stored**¹ | 404 |
| V13 | Empty sample list | 400 | 200, not stored |
| V14 | Time-reversed batch | 400 | n/a |
| V15 | Rate and count mismatch | 400 | n/a |

HTTP status; unless marked, nothing was stored. n/a: RADAR-base records carry per-sample timestamps, without a sampling rate or batch time range. ¹Stored under the project of the authenticated user.

MEDWEAR rejected 10 of the 15 malformed requests and stored the other five. RADAR-base stored none of the 12 that apply to it, but answered three with HTTP 500 instead of a client error and acknowledged an empty batch without storing anything.

## Requests served under load

600 s per level, three jobs per condition. Following the pre-registered rules (`bench/EXCLUSION_RULES.md`, applied by `bench/analysis/summarize.py`), requests started before the last simulated device had spawned are excluded. OK: share of requests acknowledged with HTTP 200 (open loop: of offered arrivals, including those dropped at the client's in-flight cap of 2,000). p50: latency of acknowledged requests. Medians over jobs; the OK share differed by at most 0.7 percentage points between jobs. "Limit raised": MEDWEAR with its per-user limit raised from 300 to 10^6 batch requests per minute, since all simulated devices shared one identity.

| N | RADAR-base OK (%) | p50 (s) | MEDWEAR, limit raised OK (%) | p50 (s) | MEDWEAR, deployed limit OK (%) | p50 (s) |
|---:|---:|---:|---:|---:|---:|---:|
| *Open loop (N batches/s offered)* | | | | | | |
| 10 | 100 | 0.16 | 100 | 0.14 | 50.1 | 0.14 |
| 50 | 100 | 0.63 | 100 | 0.54 | 9.4 | 0.59 |
| 100 | 72.2 | 27.8 | 37.9 | 30.9 | 4.6 | 4.5 |
| 200 | 34.7 | 28.6 | 17.2 | 24.5 | 1.2 | 39.1 |
| *Closed loop (N users, 1-3 s think time)* | | | | | | |
| 10 | 100 | 0.028 | – | – | 99.9 | 0.032 |
| 50 | 100 | 0.031 | – | – | 19.9 | 0.033 |
| 100 | 100 | 0.090 | – | – | 9.4 | 0.049 |
| 200 | 100 | 2.11 | – | – | 4.8 | 1.17 |

Note: an earlier draft of the paper's table counted every request of the stage, including those of the ramp-up. That changed the OK share by at most 0.8 percentage points and none of the conclusions; the table above and the paper follow the pre-registered rule.

- Open loop, RADAR-base sustained about 71 batches/s (15,500 samples/s) above saturation and queued the rest; its accepted requests waited about 28 s, the excess arrivals were dropped at the client's in-flight cap, and no request failed at the server. MEDWEAR sustained 35-38 batches/s (7,500-8,300 samples/s); 9.3-10.2% of its arrivals at 100 devices and 6.6-6.7% at 200 failed with HTTP 500 when no database connection became free within 30 s (`load.csv`). With two-phase ingest, each request needs a second connection from the pool for its completion update.
- With the deployed limit, MEDWEAR served about 5 batches/s up to 100 devices, the limit of the one shared identity, and rejected the rest with HTTP 429; at 200 devices in open loop it served 2.4 batches/s, with a median wait of 39 s. Closed loop, RADAR-base served up to 53 batches/s without error.
- Below saturation, the samples sent, acknowledged, and stored agreed on both platforms. Above it, RADAR-base stored exactly what it acknowledged. MEDWEAR stored every acknowledged sample; requests that failed between the InfluxDB write and the completion update left their batch `pending` in PostgreSQL (3,158-4,551 batches per stage with the limit raised, 301-356 at 200 devices with the deployed limit). Pending batches are not returned by queries, and a retry of the same batch completes them. Complete batches in PostgreSQL therefore held 4.6-7.0% fewer samples than InfluxDB (stored counts cover the whole stage, so the ramp cut does not apply).

## Delivery semantics and fault recovery

- Resubmission: in all three jobs, MEDWEAR stored five identical submissions once (256 samples) and RADAR-base five times (1,280 records), both answering every submission with HTTP 200.
- Faults (restart, SIGKILL, 10 s freeze of each container, five repetitions): no acknowledged sample was missing in any of the 90 MEDWEAR and 75 RADAR-base fault cases. Both platforms resumed by themselves after every fault, except RADAR-base after a Kafka SIGKILL, where the broker did not return and the stack had to be restarted in dependency order. While PostgreSQL was down, MEDWEAR answered HTTP 403 instead of 5xx, because its project lookup failed.
