# medwear-benchmark

Benchmark harness used to compare [MEDWEAR](https://github.com/SCAI-Lab/medwear) with [RADAR-base](https://radar-base.org) in *Connected but Late: Sample Age of Commercial BLE Wearables under Wi-Fi Coexistence for Real-Time Clinical Applications* (Supplementary Section "Platform Benchmark Against RADAR-base").

Both platforms run as deployed, one at a time, on a freshly created stack per job, and the same client drives both. Every test counts the samples sent, acknowledged, and stored (PostgreSQL and InfluxDB, or Kafka).

| Test | Script | What it measures |
|---|---|---|
| Latency | `bench/tests/latency.py` | 30 sequential requests per batch size (100-50,000 samples) |
| Load | `bench/tests/locustfile_bench.py` | 10-200 simulated devices, open loop (1 batch/s each) or closed loop (1-3 s think time), 600 s per level |
| Validation | `bench/tests/validation.py` | 15 malformed requests and a valid control |
| Idempotency | `bench/tests/idempotency.py` | one batch submitted five times with the same identifier |
| Durability | `bench/tests/durability.py` | restart, SIGKILL, or 10 s freeze of each container during writes, 5 repetitions per container and fault |

## Versions used in the paper

| | Version |
|---|---|
| MEDWEAR | commit `49be39f`, published as tag [`paper-2026-10`](https://github.com/SCAI-Lab/medwear/tree/paper-2026-10) (two-phase ingest, stream metadata registered once per session) |
| RADAR-base | RADAR-Gateway `cd0c6df` (release 0.9.4) with `bench/radar/RADAR-Gateway-cd0c6df.patch`, ManagementPortal 2.0.0, Confluent Platform 7.6.0 |
| Client | Python 3.9, requests 2.32.5, Locust 2.31.8 |
| Runtime | rootless Podman 5.8.2, podman-compose 1.6.0, Slurm 22.05, one exclusively allocated node (AMD EPYC 9555P) |

Hardware, software, and the full setup, including credentials, are described in [SETUP.md](SETUP.md).

## Layout

```
SETUP.md                        how the benchmark was set up and how to reproduce it
bench/
  campaign.sh, submit.sh        submit a campaign (randomized blocks) or one job
  campaigns/paper_round4.tsv    plan of the first campaign (both platforms; its RADAR-base jobs are used)
  campaigns/medwear_sessions_round5.tsv  plan of the MEDWEAR campaign used in the paper
  conditions/*.env              experiment variables per condition (no secrets)
  compose/                      bench-stack compose files for both platforms
  radar/                        patch applied to RADAR-Gateway
  slurm/job_*.sh                one Slurm job per test
  tests/                        test clients and platform adapters
  lib/                          counting, delivery checks, settings snapshot, profiling
  analysis/summarize.py         aggregates runs with the pre-registered exclusion rules
  analysis/paper_tables.py      the paper's latency, validation, and load tables from the run folders
  EXCLUSION_RULES.md            rules fixed before the campaign
results/campaigns/              manifests of the two campaigns
results/analysis/               per-run and aggregated metrics (output of summarize.py): radar_round4/ (the RADAR-base jobs of paper_round4), medwear_round5/
results/paper/                  latency, validation, and load tables of the paper (CSV + README)
```

## Campaigns of the paper

The paper reports RADAR-base from the first campaign and MEDWEAR from the second. Both use the same tests, repetitions, node, and harness.

- `paper_round4`: 35 jobs (16 RADAR-base, 19 MEDWEAR `a8a666b`) on 20-21 October 2026, seed 20261020, manifest `results/campaigns/20261020_093630_paper_round4.tsv`.
- `medwear_sessions_round5`: 19 MEDWEAR jobs on 23-24 October 2026, seed 20261023, manifest `results/campaigns/20261023_192526_medwear_sessions_round5.tsv`. MEDWEAR `49be39f` writes each batch in two phases (PostgreSQL row `pending`, InfluxDB write, then `complete`) and accepts batches that refer to a stream registered once with `POST /v1/data/session`. Its conditions (`bench/conditions/medwear_sessions_*.env`) differ from the first campaign's MEDWEAR conditions only in `MEDWEAR_SRC_REV` and `MEDWEAR_SESSIONS=1`, with which the client registers each simulated device's stream once, before the measured requests, and then sends session batches; the validation test always sends full batches. The delivery checks count only `complete` batches in PostgreSQL and record pending ones separately (`bench/lib/counts.py`). `bench/EXCLUSION_RULES.md` was not changed and still names `a8a666b`; its rules were applied unchanged.

Each manifest lists the job order and the hashes of the plan and of `bench/EXCLUSION_RULES.md` at the time of the run. For publication, comments and descriptive text in the scripts, condition files, and exclusion rules were edited to remove site-specific details (paths, a proxy address, internal references), so those files no longer match the recorded hashes. No experiment variable or rule was changed. The only code changes are outside the measurements: image preparation no longer defaults to our site's proxy (`bench/slurm/job_prepare_images.sh`), and the campaign launcher derives its working directory (`bench/launch_after_smoke.sh`).

## Results

The latency, validation, and load tables of the paper, with notes on delivery and fault recovery, are in [results/paper](results/paper/README.md). Per-run and aggregated metrics of every test, with the pre-registered exclusion rules applied (no run was excluded), are in `results/analysis/radar_round4/` (the 16 RADAR-base jobs of `paper_round4`) and `results/analysis/medwear_round5/` (`runs.csv`, `aggregate.csv`, `exclusions.csv`, `options.json`), as written by `bench/analysis/summarize.py`. The run folders themselves are not part of this repository; they contain only synthetic benchmark traffic, counts, and timings.

## Known limitations

The load generator runs on the same node as the platform under test, and all simulated devices share one client identity, so MEDWEAR's per-user rate limit applies to all of them together (hence the `medwear_sessions_ratelimit_open` condition). MEDWEAR is measured through its HTTPS proxy and RADAR-base over plain HTTP. See the paper's supplementary section for the full list.

## Citation

If you use this harness, please cite the paper: M. Perez-Serrano, H. Z. Özer, S. J. Paek, D. Paez-Granados, *Connected but Late: Sample Age of Commercial BLE Wearables under Wi-Fi Coexistence for Real-Time Clinical Applications*, submitted to IEEE Journal of Biomedical and Health Informatics, 2026 (see [CITATION.cff](CITATION.cff)).

## License

MIT, see [LICENSE](LICENSE).
