# medwear-benchmark

Benchmark harness for the [MEDWEAR](https://github.com/SCAI-Lab/medwear) ingest platform, used in *Connected but Late: Sample Age of Commercial BLE Wearables under Wi-Fi Coexistence for Real-Time Clinical Applications* (supplementary material).

MEDWEAR runs as deployed on a freshly created stack per job. Every test counts the samples sent, acknowledged, and stored (PostgreSQL and InfluxDB).

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
| MEDWEAR | commit `49be39f` (two-phase ingest, stream metadata registered once per session) |
| Client | Python 3.9, requests 2.32.5, Locust 2.31.8 |
| Runtime | rootless Podman 5.8.2, podman-compose 1.6.0, Slurm 22.05, one exclusively allocated node (AMD EPYC 9555P) |

Hardware, software, and the full setup, including credentials, are described in [SETUP.md](SETUP.md).

## Layout

```
SETUP.md                        how the benchmark was set up and how to reproduce it
bench/
  campaign.sh, submit.sh        submit a campaign (randomized blocks) or one job
  campaigns/medwear_sessions_round5.tsv  plan of the campaign
  conditions/*.env              experiment variables per condition (no secrets)
  compose/                      bench-stack compose file
  slurm/job_*.sh                one Slurm job per test
  tests/                        test clients and the MEDWEAR adapter
  lib/                          counting, delivery checks, settings snapshot, profiling
  analysis/summarize.py         aggregates runs with the pre-registered exclusion rules
  analysis/paper_tables.py      the paper's latency, validation, and load tables from the run folders
  EXCLUSION_RULES.md            rules fixed before the campaign
results/campaigns/              manifest of the campaign
results/analysis/               per-run and aggregated metrics (output of summarize.py)
results/paper/                  latency, validation, and load tables of the paper (CSV + README)
```

## Campaign of the paper

The campaign ran 19 jobs on 23-24 October 2026 with seed 20261023. Its manifest, `results/campaigns/20261023_192526_medwear_sessions_round5.tsv`, lists the job order and the hashes of the plan and of `bench/EXCLUSION_RULES.md` at the time of the run.

MEDWEAR `49be39f` writes each batch in two phases (PostgreSQL row `pending`, InfluxDB write, then `complete`) and accepts batches that refer to a stream registered once with `POST /v1/data/session`. With `MEDWEAR_SESSIONS=1` in the condition files, the client registers each simulated device's stream once, before the measured requests, and then sends session batches; the validation test always sends full batches, since its probes alter fields that session batches do not carry. The delivery checks count only `complete` batches in PostgreSQL and record pending ones separately (`bench/lib/counts.py`).

For publication, the harness was trimmed to MEDWEAR: the campaign ran with a harness that could also drive a second platform, and those code paths, files, and descriptions were removed. Comments and descriptive text in the scripts, condition files, campaign plan, and exclusion rules were also edited to remove site-specific details (paths, a proxy address, internal references). The published files therefore no longer match the hashes recorded in the manifest and in each run's `settings.json`. No experiment variable or rule was changed (see the note at the top of `bench/EXCLUSION_RULES.md`). Code changes outside the trimming: image preparation no longer defaults to our site's proxy (`bench/slurm/job_prepare_images.sh`), the campaign launcher derives its working directory (`bench/launch_after_smoke.sh`), and the Slurm job name is `medwear_bench`.

## Results

The latency, validation, and load tables of the paper, with notes on delivery and fault recovery, are in [results/paper](results/paper/README.md). Per-run and aggregated metrics of every test, with the pre-registered exclusion rules applied (no run was excluded), are in `results/analysis/` (`runs.csv`, `aggregate.csv`, `exclusions.csv`, `options.json`), as written by `bench/analysis/summarize.py`. The raw run folders, from which both are produced, are archived separately on Zenodo.

## Known limitations

The load generator runs on the same node as the platform under test, and all simulated devices share one client identity, so MEDWEAR's per-user rate limit applies to all of them together (hence the `medwear_sessions_ratelimit_open` condition). See the paper's supplementary material for the full list.

## Citation

If you use this harness, please cite the paper: M. Perez-Serrano, H. Z. Özer, S. J. Paek, D. Paez-Granados, *Connected but Late: Sample Age of Commercial BLE Wearables under Wi-Fi Coexistence for Real-Time Clinical Applications*, submitted to IEEE Journal of Biomedical and Health Informatics, 2026.

## License

MIT, see [LICENSE](LICENSE).
