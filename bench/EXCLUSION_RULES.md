# Pre-registration: conditions, metrics, exclusion and analysis rules

Write this BEFORE the campaign starts. `bench/campaign.sh` refuses to submit
until the marker line at the end says `yes`, and records this file's hash in
the campaign manifest. `bench/analysis/summarize.py` implements the rules
below; any deviation from it is reported as a deviation.

## A. What is compared

1. **Each platform as deployed.** MEDWEAR: commit `a8a666b` (the version used
   in the end-to-end tests), built from a git archive of that commit; default
   database pool (5 + 10 overflow), one uvicorn worker, default per-user batch
   rate limit (300/min); HTTPS through nginx (TLS terminated in nginx, then
   HTTP to the API). RADAR: plain HTTP directly
   to the gateway, no proxy. The latency test also measures MEDWEAR directly at
   the API, so the cost of the nginx+TLS step is reported separately.
2. **Same data per request**: identical sample counts and signal
   (ECG-like 256 samples @ 256 Hz, PPG-like 100 samples @ 100 Hz; latency
   sizes 100–50,000). Each platform's own request format is kept: MEDWEAR's
   Open mHealth body carries per-sample signal quality and a declared rate;
   RADAR's Avro records carry a timestamp per sample. Bytes per request are
   reported.
3. **Conditions** (`bench/conditions/`):
   - PRIMARY comparison: `medwear_default_open`, `radar_default_open`:
     defaults on both platforms (MEDWEAR DB pool 5, 1 uvicorn worker),
     open-loop load (N devices × 1 batch/s), N = 10, 50, 100, 200.
   - SECONDARY, reported separately: `medwear_ratelimit_open`: MEDWEAR
     `a8a666b` with one parameter changed, `BATCH_RATE_LIMIT=1000000/minute`,
     because all simulated devices share one bench user and the default
     per-user limit (300/min = 5 batches/s) would otherwise cap the load;
     reported as "MEDWEAR a8a666b with BATCH_RATE_LIMIT changed".
     `*_default_closed`: closed loop.
   - The durability writer is paced at 2 batches/s and latency requests at
     2 requests/s on both platforms (sequential requests from one user must
     not run into MEDWEAR's per-user rate limit). Every batch covers the
     seconds that end at the moment it is sent (data already recorded).
   - Open-loop load: arrivals beyond 2,000 requests in flight are not sent
     (`MAX_INFLIGHT`); they are counted (`client_dropped`).
   - Resources per job: the whole node (`--exclusive`, all 128 cores; memory
     limit 32 GB). Every job verifies that its allocation is exclusive; the
     node state is recorded per run (`node_state_start/end.txt`). Runs from
     the abandoned shared-node campaign of 2026-10-20 09:24 are never pooled
     with these (different harness hash, rule C7).
4. **No production or study systems.** No number in the paper comes from CLAID
   production or any database holding participant data.
   All results come from the isolated bench stacks.

## B. Primary metrics (claims rest on these; everything else is secondary)

- Latency per batch size: p50 and p95 of successful requests, per hop.
- Load per level: failure rate (all classes) and p95 of successful requests;
  ok-samples/s.
- Durability: acked − stored (samples) per case; self-recovery yes/no.
- Validation: outcome per probe (rejected / accepted, stored / not stored).

## C. Run-level exclusion rules

1. **Setup failure.** Final run-index status `failed` with stage `init`,
   `stack_up`, `stack_down`, `cgroup_check`, `preflight` or `settings_snapshot`,
   and no test request sent. Excluded and re-submitted.
2. **Interrupted job.** Ended by SIGTERM (time limit or `scancel`), or no
   final run-index row. Excluded and re-submitted.
3. **Contaminated stack.** `foreign_containers_*.txt` in the run folder (own
   non-bench containers were running), or the other platform's containers at
   the end. Excluded and re-submitted.
4. **Unverifiable delivery.** A count snapshot used by a delivery line has
   errors: that phase's delivery is not reported; its latency and request
   classes are.
5. **Never excluded:** failed or timed-out requests, 4xx/5xx responses,
   durability cases that did not self-recover, sent ≠ acked ≠ stored, high
   co-located load. These are results.
6. **Resubmission limit.** At most two re-submissions per excluded run; a
   condition with fewer than 3 valid runs after that is reported with its n.
7. **Harness changes.** Runs with a different harness or API-constraints hash
   are never pooled.
8. **No canonical run.** Every run not excluded by 1–3 is included. Choosing
   one run to represent a condition is not allowed. Replacements for excluded
   runs are the next runs in campaign order, never chosen afterwards. A run
   that looks wrong for a reason not covered here stays in; any extra
   exclusion is a reported deviation with results shown with and without it.

## D. Analysis rules

1. **Unit and aggregation.** Each run is summarised on its own (its p50, p95,
   failure rate, throughput); runs are combined per condition as the
   median with min–max, n stated.
2. **Warm-up (latency).** 5 requests per size and hop are excluded from the
   percentiles but counted in sent/acked/stored.
3. **Ramp-up (load).** Requests started before all simulated devices were
   running are excluded from load metrics (`summarize.py --ramp-cut spawn`).
4. **Censoring (load).** Requests in flight at the end of a stage are allowed
   to complete (up to LOCUST_TIMEOUT) and counted; the number in flight is
   reported per stage. A request slower than the client timeout is a failure
   (`client_timeout`), not a success.
5. **Success** = HTTP 2xx within the client timeout. 409 is not a success.
   In open-loop load, offered load = sent + dropped arrivals, and dropped
   arrivals count as failures (`failure_rate`); the rate over sent requests
   only is reported alongside (`failure_rate_sent_only`).
   Failure classes are reported separately (client timeout, connection error,
   401, 403, server timeout 504/408, other 5xx, other 4xx).
6. **Repetitions.** n = 3 per condition, randomised blocks (seed recorded).
   If the across-run coefficient of variation of a primary metric exceeds
   20 %, 2 more runs of that condition are added.
7. **Client saturation.** A load stage whose load generator averaged > 90 %
   CPU is flagged `client_bound` and reported as such (not excluded).

RULES FINAL: yes
