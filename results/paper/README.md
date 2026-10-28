# Results reported in the paper

Per-test results of the campaign reported in the paper's supplementary material: 19 jobs, 23-24 October 2026, seed 20261023, manifest `../campaigns/20261023_192526_medwear_sessions_round5.tsv`, MEDWEAR commit `49be39f` (two-phase ingest, stream metadata registered once per session, `MEDWEAR_SESSIONS=1`). The CSV files were produced from the harness output of these jobs with

```
python3 bench/analysis/paper_tables.py --results results \
    --manifest results/campaigns/20261023_192526_medwear_sessions_round5.tsv --out results/paper
```

The benchmark sends synthetic ECG batches under generated identifiers only; no participant or health data were used. The raw run folders are not part of this repository; they are archived separately on Zenodo.

| File | Content |
|---|---|
| `latency.csv` | per hop and batch size: median over jobs of the per-job p50 and p95, with their range |
| `validation.csv` | per malformed request: HTTP status, whether anything was stored, and how many jobs agree |
| `load.csv` | per condition and level: share of requests acknowledged, acknowledged requests/s, share of HTTP 5xx, median latency of acknowledged requests (pre-registered ramp cut) |

## Latency by batch size

30 sequential requests per batch size and job, three jobs. Through the nginx proxy (HTTPS), as deployed. Medians over jobs of the per-job percentiles.

| Samples/batch | p50 (ms) | p95 (ms) |
|---:|---:|---:|
| 100 | 32.0 | 34.5 |
| 500 | 38.6 | 40.5 |
| 1,000 | 44.0 | 48.0 |
| 5,000 | 95.1 | 138.9 |
| 10,000 | 154.8 | 203.4 |
| 50,000 | 745.3 | 770.1 |

All 1,080 timed requests succeeded. Requests sent directly to the API, bypassing nginx and HTTPS (`hop=api_direct` in `latency.csv`), were at most 2.0 ms faster (medians).

## Responses to malformed requests

A valid control (V00) and 15 malformed requests, three jobs. The responses were identical in all three jobs.

| | Defect | Response |
|---|---|---|
| V01 | No token | 401 |
| V02 | Invalid token | 401 |
| V03 | Wrong content type | 200, **stored** |
| V04 | Malformed JSON | 400 |
| V05 | Empty body | 400 |
| V06 | Unknown schema | 200, **stored** |
| V07 | Missing timestamp | 400 |
| V08 | Missing sampling rate | 200, **stored** |
| V09 | Non-numeric sample | 400 |
| V10 | Missing subject | 400 |
| V11 | Missing project | 200, **stored**¹ |
| V12 | Unknown project | 200, **stored**¹ |
| V13 | Empty sample list | 400 |
| V14 | Time-reversed batch | 400 |
| V15 | Rate and count mismatch | 400 |

HTTP status; unless marked, nothing was stored. ¹Stored under the project of the authenticated user.

MEDWEAR rejected 10 of the 15 malformed requests and stored the other five. Validation always sends full batches (not session batches), since its probes alter fields that session batches do not carry.

## Requests served under load

600 s per level, three jobs per condition. Following the pre-registered rules (`bench/EXCLUSION_RULES.md`, applied by `bench/analysis/summarize.py`), requests started before the last simulated device had spawned are excluded. OK: share of requests acknowledged with HTTP 200 (open loop: of offered arrivals, including those dropped at the client's in-flight cap of 2,000). p50: latency of acknowledged requests. Medians over jobs; the OK share differed by at most 0.7 percentage points between jobs. "Limit raised": per-user limit raised from 300 to 10^6 batch requests per minute, since all simulated devices shared one identity.

| N | Limit raised OK (%) | p50 (s) | Deployed limit OK (%) | p50 (s) |
|---:|---:|---:|---:|---:|
| *Open loop (N batches/s offered)* | | | | |
| 10 | 100 | 0.14 | 50.1 | 0.14 |
| 50 | 100 | 0.54 | 9.4 | 0.59 |
| 100 | 37.9 | 30.9 | 4.6 | 4.5 |
| 200 | 17.2 | 24.5 | 1.2 | 39.1 |
| *Closed loop (N users, 1-3 s think time)* | | | | |
| 10 | – | – | 99.9 | 0.032 |
| 50 | – | – | 19.9 | 0.033 |
| 100 | – | – | 9.4 | 0.049 |
| 200 | – | – | 4.8 | 1.17 |

- With the limit raised, MEDWEAR sustained 35-38 batches/s (7,500-8,300 samples/s) above saturation; 9.3-10.2% of its arrivals at 100 devices and 6.6-6.7% at 200 failed with HTTP 500 when no database connection became free within 30 s (`load.csv`). With two-phase ingest, each request needs a second connection from the pool for its completion update.
- With the deployed limit, MEDWEAR served about 5 batches/s up to 100 devices, the limit of the one shared identity, and rejected the rest with HTTP 429; at 200 devices in open loop it served 2.4 batches/s, with a median wait of 39 s.
- Below saturation, the samples sent, acknowledged, and stored agreed. Above it, MEDWEAR stored every acknowledged sample; requests that failed between the InfluxDB write and the completion update left their batch `pending` in PostgreSQL (3,158-4,551 batches per stage with the limit raised, 301-356 at 200 devices with the deployed limit). Pending batches are not returned by queries, and a retry of the same batch completes them. Complete batches in PostgreSQL therefore held 4.6-7.0% fewer samples than InfluxDB (stored counts cover the whole stage, so the ramp cut does not apply).

## Delivery semantics and fault recovery

- Resubmission: in all three jobs, MEDWEAR stored five identical submissions once (256 samples), answering every submission with HTTP 200.
- Faults (restart, SIGKILL, 10 s freeze of each container, five repetitions): no acknowledged sample was missing in any of the 90 fault cases, and MEDWEAR resumed by itself after every fault. While PostgreSQL was down, it answered HTTP 403 instead of 5xx, because its project lookup failed.
