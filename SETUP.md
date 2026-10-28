# Setup

How the benchmark was set up for the paper campaign, and how to reproduce it. MEDWEAR runs in rootless Podman inside Slurm jobs, one job at a time, on a stack created fresh for each job (and for each load level).

## Hardware and software used

| | |
|---|---|
| Node | 1 × AMD EPYC 9555P (64 cores, 128 threads), 124 GiB RAM, local ext4 storage |
| Allocation per job | whole node (`--exclusive`), all 128 threads, 32 GB memory limit |
| OS | Rocky Linux 9.5, kernel 5.14, cgroup v2 |
| Scheduler | Slurm 22.05 |
| Containers | rootless Podman 5.8.2 (crun, overlay), podman-compose 1.6.0 |
| Client | Python 3.9.21, requests 2.32.5, Locust 2.31.8, on the same node, over 127.0.0.1 |
| MEDWEAR | commit `49be39f`; PostgreSQL 15 (alpine), InfluxDB 2.7, Keycloak 23.0, MinIO, nginx; one uvicorn worker, SQLAlchemy pool 5 + 10 |

MEDWEAR is reached through its nginx proxy over HTTPS, as deployed.

## Directory layout

The scripts take the repository root as `BENCH` (the parent of `bench/`) and write to `results/` below it. Slurm jobs receive `BENCH` from `bench/submit.sh`. The MEDWEAR source goes below `bench/`:

```
<repo>/
  bench/                         this harness
  bench/medwear_src/49be39f/     MEDWEAR source (step 3)
  results/                       run output
```

## Steps

1. **Client tools.** Create a Python 3.9 virtual environment with `requests==2.32.5` and `locust==2.31.8`, install podman-compose 1.6.0, then copy `bench/site.env.example` to `bench/site.env` and set the paths to `python`, `locust`, and `podman-compose`. `site.env` holds tool paths only.

2. **Container configuration.** The jobs use `bench/lib/containers-bench.conf`, and image preparation uses `bench/lib/registries-bench.conf` (fully qualified image names, no short-name prompts in batch jobs). Rootless Podman must place containers in the job's cgroup, which every job checks (`cgroups_start.tsv`, `inside_job=yes`).

3. **MEDWEAR source.** Export the benchmarked commit into `bench/medwear_src/<rev>/`, where `<rev>` is `MEDWEAR_SRC_REV` in the condition files:
   ```bash
   mkdir -p bench/medwear_src
   git -C /path/to/medwear archive --prefix=49be39f/ 49be39f \
       medwear_med_api docker/medwear-api.Dockerfile docker/nginx docker/medwear-realm.json frontend \
     | tar -x -C bench/medwear_src
   ```

4. **Credentials.** No credentials are stored in this repository.
   `bench/make_bench_env.sh` writes `bench/.env.medwear-bench` with random passwords and tokens for PostgreSQL, InfluxDB, MinIO, and Keycloak (mode 600, never printed). `bench/.env.medwear-bench.example` lists its keys. Each job then creates a Keycloak user `bench-loader` with the `device` role and a random password in the fresh realm, and fetches tokens for it. All simulated devices share this one identity, so MEDWEAR's per-user rate limit applies to them together.
   Run snapshots (`run.env`) redact every variable whose name looks like a secret.

5. **Images.** Pull and build all images once, before any job (jobs never pull, so digests stay fixed and are recorded per run):
   ```bash
   bench/prepare_images.sh                      # on the node
   bench/submit.sh prepare_images conditions/smoke_medwear_sessions.env   # or as a Slurm job
   ```
   Behind an HTTP proxy, set `HTTPS_PROXY`/`HTTP_PROXY` for `prepare_images.sh`, or `BENCH_PROXY` for the Slurm job.

6. **Smoke run.** One short job checks the whole chain:
   ```bash
   bench/submit.sh smoke conditions/smoke_medwear_sessions.env
   ```
   The summary at the end of `results/slurm/medwear_bench_<jobid>.out` (also `summary.txt` in the run folder) should report every container inside the job, a health-check median below 100 ms, `sent=acked=stored` for every delivery check, and all acceptance checks passed.

7. **Campaign.** `bench/EXCLUSION_RULES.md` must be marked `RULES FINAL: yes` before the campaign starts. Then:
   ```bash
   CAMPAIGN_SEED=20261023 bench/campaign.sh campaigns/medwear_sessions_round5.tsv
   ```
   submits every condition once per block, shuffled with the seed, chained so that one job runs at a time; `bench/launch_after_smoke.sh <smoke job ids>` starts it only if the smoke run passed. The manifest (job ids, order, seed, hashes) goes to `results/campaigns/`.

8. **Analysis.**
   ```bash
   python3 bench/analysis/summarize.py --results results --out results/analysis/medwear_sessions_round5
   ```
   applies the exclusion rules and writes `runs.csv`, `aggregate.csv`, and `exclusions.csv`. The paper's tables come from `bench/analysis/paper_tables.py`; see [results/paper](results/paper/README.md) for the command.

## Per-job checks

Every job records the node state at start and end, the Slurm allocation, image digests, platform settings read inside the containers (`settings.json`), CPU throttling, and per-container CPU and memory at 1 Hz. A job stops before any test if a container runs outside its cgroup, if another stack or bench job is running, if a bench port is taken, or if the health-check median exceeds `PREFLIGHT_MAX_MS`.
